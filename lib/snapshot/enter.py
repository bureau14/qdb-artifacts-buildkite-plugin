"""enter and resume: what the plan step decides, in order (specification section 7).

A gated build runs them in the post-command of the steps that carry the
plugin's options ``plan`` (enter) and ``resume``, after the step's command
generated the pipeline:

- enter: reuse what exists, trigger the builds of dependencies that lack a
  variant, or upload the pipeline with one build of every snapshot dependency
  written into it;
- resume, after the triggered builds ended: build, or fail with the cause.

Each is its decisions in the specification's order, under its numbers; the
first decision that ends the step ends it. A decision is a function of what
the plan step's edge (``PlanEdge``) returned. A decision that fails the step
raises a ``failing.Refusal``.
"""

from __future__ import annotations

import functools
import json
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from ..clients import aws_clients
from ..config import env_or_ssm
from ..errors import ArtifactsError
from ..forms import is_variant
from ..log import log
from ..options import job_variable
from ..pointer import Pointer
from .agent import RealAgent
from .annotation import (
    PLAN_REFUSED,
    caused_annotation,
    failure_annotation,
    left_no_record,
    plan_annotation,
    refusal_annotation,
    reused_annotation,
)
from .buildkite import BuildkiteError, PipelineFacts, fetch_pipeline
from .canonical import canonical_json
from .declaration import (
    Reading,
    is_template_path,
    pipeline_downloads,
    read_declaration,
    rule_violations,
    violation_message,
)
from .document import (
    DocumentError,
    Rewrite,
    dump_document,
    dump_written,
    load_document,
    needed_variants,
    planned,
    promote_keys,
    promotions,
    resume_pipeline,
)
from .edge import (
    BUILT,
    MODE,
    PLAN,
    PROMOTES,
    REUSED,
    Edge,
    arrival,
    chain_plan,
    chain_records,
    held_only,
    opened_store,
    origin_of,
    own_node,
    pointers_at,
    stored_records,
)
from .failing import EDGE_ERRORS, Refusal, gated, survive
from .git import Git, RealGit
from .job import Job, is_chain_node, job_of, own_ref
from .plan import Node, Plan, PlanError, encode_plan
from .planner import Pipelines, Root, make_plan, rebuild_setting
from .records import (
    GENERATOR_FAILED,
    NOT_DECLARED,
    RULE,
    Cause,
    Origin,
    Record,
    caused_record,
    chain_failure,
    reason_record,
    shared_failure,
    shared_record,
)
from .shown import short_commit, shown_text, shown_value

# One annotation per context: a retried step replaces its own.
PLAN_CONTEXT = "snapshot-plan"
STEP_CONTEXT = "snapshot"

TEMPLATE_DIRECTORY = ".buildkite"


@dataclass(frozen=True)
class PlanEdge(Edge):
    """The edge of enter and resume: every gate step's, and what only the plan step reaches.

    ``git``: other repositories. ``pipelines``: Buildkite's facts of a
    pipeline slug. ``plugin_commit``: the commit of the plugin's own checkout,
    which the plan step writes into what it uploads.
    """

    git: Git
    pipelines: Pipelines
    plugin_commit: Callable[[], str]


# ---------------------------------------------------------------------------
# Failing before anything is built
# ---------------------------------------------------------------------------


def reason_refusal(
    job: Job, reason: str, message: str, origin: Origin | None, now: float
) -> Refusal:
    """A refusal before anything is built, with the record of ``reason`` when there is an origin."""
    record = None if origin is None else reason_record(origin, reason, now)
    return Refusal(message, refusal_annotation(reason, job.project, job.build, message), record)


def chain_origin(job: Job, edge: Edge) -> Origin | None:
    """What a record of this build says of it, for a chain node with a key; None for a root.

    A root knows no key before it made its plan (step 3).
    """
    if not is_chain_node(job):
        return None
    p = chain_plan(job, edge)
    node = own_node(job, edge, p)
    return None if node.key is None else origin_of(job, p, node.key)


# ---------------------------------------------------------------------------
# Steps 1 to 5, of enter and of resume
# ---------------------------------------------------------------------------


def check_command(job: Job, edge: Edge) -> None:
    """1. The command failed: record ``generator_failed`` when a chain node; fail."""
    if job.exit_status != 0:
        message = (
            f"the command exited {job.exit_status}: it generates the pipeline into "
            f"{shown_text(job.pipeline or '')}, which this step plans"
        )
        origin = chain_origin(job, edge)
        raise reason_refusal(job, GENERATOR_FAILED, message, origin, edge.clock())


@dataclass(frozen=True)
class Generated:
    """The generated pipeline as loaded, and the reading of the build's own commit's templates."""

    doc: Any
    reading: Reading


def generated_pipeline(job: Job, edge: Edge) -> Generated:
    """2. Check the rule of section 2; failing, record ``rule`` when a chain node; fail.

    A file the command did not leave, or one that does not load, is the
    generator's failure. A template of the build's own commit that does not
    parse fails the build that reads it for itself, as the rule does.
    """
    path = Path(job.pipeline or "")
    try:
        doc = load_document(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, DocumentError) as exc:
        message = f"the command passed, and {shown_text(str(path))}: {shown_text(str(exc))}"
        raise reason_refusal(
            job, GENERATOR_FAILED, message, chain_origin(job, edge), edge.clock()
        ) from exc
    files = edge.checkout.checkout_files(TEMPLATE_DIRECTORY, is_template_path)
    reading = read_declaration(files, job.project)
    unparsed = [f"{shown_text(e.path)}: {e.detail}" for e in reading.errors]
    downloads = pipeline_downloads(doc)
    violations = rule_violations(downloads, reading.declaration, job.project, own_ref(job))
    problems = unparsed + [violation_message(v) for v in violations]
    if problems:
        message = "\n".join(problems)
        raise reason_refusal(job, RULE, message, chain_origin(job, edge), edge.clock())
    return Generated(doc, reading)


def root_plan(job: Job, edge: PlanEdge, generated: Generated) -> Plan:
    """3, a root: make the plan (sections 3 and 4), write it, annotate the build with it.

    A plan that cannot be made fails the build with its message. An annotation
    the agent refuses is logged: the plan is written, and the build is sound
    without its picture.
    """
    try:
        root = Root(
            project=job.project,
            branch=job.branch,
            commit=edge.checkout.head_commit(),
            build_id=job.build.id,
            build_number=job.build.number,
            build_url=job.build.url,
            rebuild=rebuild_setting(job.rebuild),
        )
        p = make_plan(root, generated.reading, edge.git, edge.pipelines, edge.clock())
        text = encode_plan(p)
    except PlanError as exc:
        annotation = refusal_annotation(PLAN_REFUSED, job.project, job.build, str(exc))
        raise Refusal(str(exc), annotation) from exc
    edge.agent.meta_data_set(PLAN, text)
    annotation = plan_annotation(p, job.project)
    survive(
        edge, lambda: edge.agent.annotate(annotation, "info", PLAN_CONTEXT), "annotate the plan"
    )
    return p


def plan_of(job: Job, edge: PlanEdge, generated: Generated) -> tuple[Plan, Node]:
    """3. A chain node loads the plan and checks its commit; a root makes it."""
    p = chain_plan(job, edge) if is_chain_node(job) else root_plan(job, edge, generated)
    return p, own_node(job, edge, p)


def promoted(job: Job, edge: Edge, generated: Generated, origin: Origin) -> frozenset[str]:
    """5. ``D``, the variants this pipeline promotes; a variant promoted twice fails, as the rule.

    Two steps that promote one variant break a rule the plan step holds a
    pipeline that has a snapshot to, so the record's reason is ``rule``.
    """
    try:
        return frozenset(promotions(generated.doc, job.project))
    except DocumentError as exc:
        raise reason_refusal(job, RULE, str(exc), origin, edge.clock()) from exc


def asked(job: Job, d: frozenset[str]) -> frozenset[str]:
    """5. ``N``: ``SNAPSHOT_NEEDS`` for a chain node, else ``D``."""
    if not is_chain_node(job):
        return d
    try:
        needs = json.loads(job.needs or "")
    except ValueError:
        needs = None
    if not isinstance(needs, list) or not all(is_variant(v) for v in needs):
        raise ArtifactsError(
            f"SNAPSHOT_NEEDS {shown_value(job.needs)} is not a JSON list of variants: the "
            "trigger step of a chain node hands down what its consumer downloads"
        )
    return frozenset(needs)


def check_declared(
    job: Job, edge: Edge, n: frozenset[str], d: frozenset[str], origin: Origin
) -> None:
    """5. A variant of ``N`` that is not in ``D``: record ``not_declared``; fail. Nothing compiles."""
    undeclared = sorted(n - d)
    if undeclared:
        message = (
            f"asked for {', '.join(undeclared)}, which this pipeline does not promote; it "
            f"promotes {', '.join(sorted(d)) or 'nothing'}"
        )
        raise reason_refusal(job, NOT_DECLARED, message, origin, edge.clock())


# ---------------------------------------------------------------------------
# Steps 6 and 7, of enter
# ---------------------------------------------------------------------------


def is_reused(d: frozenset[str], n: frozenset[str], m: frozenset[str]) -> bool:
    """6. ``D`` is not empty and no variant of ``N`` is in ``M``, the variants without a pointer."""
    return bool(d) and not (n & m)


def reuse(
    job: Job, edge: Edge, p: Plan, held: Mapping[str, Pointer | None], n: frozenset[str]
) -> None:
    """6. Mode ``reused``, and the builds that hold the variants asked for on the build page.

    An annotation the agent refuses is logged: the build is reused all the same.
    """
    builds = {v: held[v].build for v in n if held[v] is not None}
    edge.agent.meta_data_set(MODE, REUSED)
    annotation = reused_annotation(p, job.project, builds)
    survive(
        edge,
        lambda: edge.agent.annotate(annotation, "success", STEP_CONTEXT),
        "annotate the builds that hold the variants",
    )
    edge.log(f"{job.project} is reused: every variant asked for is held")


def check_shared(job: Job, edge: Edge, p: Plan, origin: Origin, wanted: frozenset[str]) -> None:
    """7. A build of ``K`` failed on every wanted variant while this one waited behind it: fail.

    The own record copies those entries and names that build under
    ``shared_with``. An empty outcome or any other state is not a failure.
    """
    if not wanted:
        return
    records = stored_records(edge, origin.project, origin.key)
    source = shared_failure(records, job.build.id, arrival(edge), wanted) if records else None
    if source is None:
        return
    own = shared_record(origin, source, wanted, edge.clock())
    message = (
        f"build {source.build.number} of the same snapshot failed on "
        f"{', '.join(sorted(wanted))} while this build waited behind it: {source.build.url}"
    )
    raise Refusal(message, failure_annotation(p, own, {}), own)


# ---------------------------------------------------------------------------
# Steps 8 to 11
# ---------------------------------------------------------------------------


def dependency_key(p: Plan, project: str) -> str:
    """The key of a snapshot dependency of this build, as the plan fixed it."""
    node = p.nodes.get(project)
    if node is None or node.key is None:
        raise PlanError(f"the pipeline downloads {project}, which plan {p.id} holds no key of")
    return node.key


def dependency_pointers(
    edge: Edge, p: Plan, needs: Mapping[str, frozenset[str]]
) -> dict[tuple[str, str], Pointer | None]:
    """8. The pointer of each variant of each dependency at its key; None for a missing one."""
    return {
        (project, v): found
        for project, variants in sorted(needs.items())
        for v, found in pointers_at(edge, project, dependency_key(p, project), variants).items()
    }


def missing_variants(
    held: Mapping[tuple[str, str], Pointer | None],
) -> dict[str, tuple[str, ...]]:
    """8. The variants of each dependency that have no pointer at its key, sorted."""
    missing: dict[str, list[str]] = {}
    for (project, v), found in sorted(held.items()):
        if found is None:
            missing.setdefault(project, []).append(v)
    return {project: tuple(vs) for project, vs in missing.items()}


def cause_message(p: Plan, c: Cause, records: Mapping[str, Record]) -> str:
    """One line of the message of a build that a dependency left without variants."""
    lacks = (
        f"{c.project} @ {short_commit(p.nodes[c.project].commit)} ({c.key}) has no "
        f"{', '.join(c.variants)}"
    )
    if c.key in records:
        return f"{lacks}: its build {records[c.key].build.number} failed in this chain"
    return f"{lacks}: {left_no_record(p, c)}"


def fail_with_causes(
    job: Job, edge: Edge, p: Plan, causes: list[Cause], origin: Origin | None
) -> NoReturn:
    """Fail a build that its dependencies left without variants, each named as a cause."""
    records = chain_records(edge, p, causes)
    message = "\n".join(cause_message(p, c, records) for c in causes)
    record = None if origin is None else caused_record(origin, causes, edge.clock())
    annotation = caused_annotation(p, job.project, job.build, causes, records)
    raise Refusal(message, annotation, record)


def failed_in_chain(
    job: Job,
    edge: Edge,
    p: Plan,
    missing: Mapping[str, tuple[str, ...]],
    origin: Origin | None,
) -> None:
    """9. A dependency with a missing variant failed earlier in this chain: fail, triggering nothing.

    Its failure record of ``key(P)`` carries this plan. The own record names it
    under ``caused_by``. Within one chain a key is built at most once.
    """
    lacking = [Cause(project, dependency_key(p, project), vs) for project, vs in missing.items()]
    causes = [c for c in lacking if chain_failure(stored_records(edge, c.project, c.key), p.id)]
    if causes:
        fail_with_causes(job, edge, p, causes, origin)


def trigger(
    job: Job,
    edge: PlanEdge,
    p: Plan,
    needs: Mapping[str, frozenset[str]],
    missing: Mapping[str, tuple[str, ...]],
) -> None:
    """10. One trigger step per dependency with a missing variant, and the resume step.

    Each trigger step hands down every variant this pipeline's entries name
    for its dependency. The resume step runs this step's command again.
    """
    if not job.command:
        raise ArtifactsError("BUILDKITE_COMMAND is not set, and the resume step runs it again")
    wanted = {project: needs[project] for project in missing}
    document = resume_pipeline(p, wanted, job.command, edge.plugin_commit(), job.pipeline or "")
    edge.agent.pipeline_upload(dump_written(document))
    edge.log(f"triggered {', '.join(sorted(missing))}; the resume step goes on when they end")


def build_pipeline(
    job: Job,
    edge: PlanEdge,
    generated: Generated,
    key: str | None,
    held: Mapping[tuple[str, str], Pointer | None],
    existing: Mapping[str, Pointer],
) -> None:
    """11. Rewrite the generated pipeline (section 8), mark the build ``built``, upload it.

    With a key, ``snapshot-promotes`` names the step of each variant that is
    built: a replaced step promotes nothing, so leave asks for no outcome of it.
    The marks leave reads are set before the upload, so that no uploaded step
    runs in a build without them; an upload the agent refuses then fails the
    step, which sets the mode ``failed``.
    """
    dependencies = held_only(held)
    rewrite = Rewrite(job.project, key, edge.plugin_commit(), dependencies, existing)
    out = planned(generated.doc, rewrite)
    document = dump_document(out)
    if key is not None:
        promotes = canonical_json(promote_keys(out, job.project)).decode("utf-8")
        edge.agent.meta_data_set(PROMOTES, promotes)
    edge.agent.meta_data_set(MODE, BUILT)
    edge.agent.pipeline_upload(document)
    edge.log(f"{job.project} builds: uploaded the pipeline with {len(dependencies)} builds in it")


# ---------------------------------------------------------------------------
# Enter and resume
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """What steps 1 to 5 found: the pipeline, the plan, the own key and origin, ``D`` and ``N``.

    Without a key (step 4) there is no origin, and ``D`` and ``N`` are empty.
    """

    generated: Generated
    plan: Plan
    key: str | None
    origin: Origin | None
    d: frozenset[str]
    n: frozenset[str]


def first_steps(job: Job, edge: PlanEdge) -> Prepared:
    """Steps 1 to 5, the same in enter and resume."""
    check_command(job, edge)
    generated = generated_pipeline(job, edge)
    p, node = plan_of(job, edge, generated)
    if node.key is None:
        # 4. Only a root can be here: its declaration holds a latest successful dependency.
        edge.log(f"{job.project} has no snapshot: it is built, and never reused")
        return Prepared(generated, p, None, None, frozenset(), frozenset())
    origin = origin_of(job, p, node.key)
    d = promoted(job, edge, generated, origin)
    n = asked(job, d)
    check_declared(job, edge, n, d, origin)
    return Prepared(generated, p, node.key, origin, d, n)


def own_pointers(job: Job, edge: Edge, prepared: Prepared) -> dict[str, Pointer | None]:
    """The pointer of each variant of ``D`` at ``K``; none without a key."""
    if prepared.key is None:
        return {}
    return pointers_at(edge, job.project, prepared.key, prepared.d)


def enter_decisions(job: Job, edge: PlanEdge) -> None:
    prepared = first_steps(job, edge)
    held = own_pointers(job, edge, prepared)
    if prepared.origin is not None:
        m = frozenset(held) - frozenset(held_only(held))
        if is_reused(prepared.d, prepared.n, m):
            reuse(job, edge, prepared.plan, held, prepared.n)
            return
        check_shared(job, edge, prepared.plan, prepared.origin, prepared.n & m)
    existing = held_only(held)
    needs = needed_variants(prepared.generated.doc, job.project, existing)
    dependencies = dependency_pointers(edge, prepared.plan, needs)
    missing = missing_variants(dependencies)
    if missing:
        failed_in_chain(job, edge, prepared.plan, missing, prepared.origin)
        trigger(job, edge, prepared.plan, needs, missing)
        return
    build_pipeline(job, edge, prepared.generated, prepared.key, dependencies, existing)


def resume_decisions(job: Job, edge: PlanEdge) -> None:
    prepared = first_steps(job, edge)
    existing = held_only(own_pointers(job, edge, prepared))
    needs = needed_variants(prepared.generated.doc, job.project, existing)
    dependencies = dependency_pointers(edge, prepared.plan, needs)
    missing = missing_variants(dependencies)
    if missing:
        causes = [
            Cause(project, dependency_key(prepared.plan, project), vs)
            for project, vs in missing.items()
        ]
        fail_with_causes(job, edge, prepared.plan, causes, prepared.origin)
    build_pipeline(job, edge, prepared.generated, prepared.key, dependencies, existing)


# The errors of enter and resume: the edge's, and those of the modules that read documents.
ENTER_ERRORS = (*EDGE_ERRORS, DocumentError, BuildkiteError)


def enter(job: Job, edge: PlanEdge) -> int:
    """The plan step: decide what this build does.

    1. The command failed: record ``generator_failed`` when a chain node; fail.
    2. The generated pipeline breaks the rule of section 2: record ``rule``
       when a chain node; fail.
    3. A chain node loads the plan and checks its commit; a root makes the
       plan, writes it and annotates the build with it. A plan that cannot be
       made fails the build with its message.
    4. The own project has no snapshot: go to 8 with no key.
    5. ``D`` is what this pipeline promotes, a variant promoted twice failing;
       ``N`` is ``SNAPSHOT_NEEDS`` for a chain node, else ``D``. A variant of
       ``N`` not in ``D``: record ``not_declared``; fail.
    6. ``D`` is not empty and every variant of ``N`` has a pointer at ``K``:
       mode reused; done.
    7. A build of ``K`` failed on the wanted variants while this one waited:
       record its failure as shared; fail.
    8. For each direct snapshot dependency, the variants the kept steps name,
       and which of them have no pointer at its key.
    9. A dependency with a missing variant failed earlier in this chain:
       record the cause; fail. Nothing is triggered.
    10. Some are missing: upload a trigger step per such dependency and the
        resume step; done.
    11. Else mode built, and upload the rewritten pipeline; done.
    """
    return gated(job, edge, enter_decisions, ENTER_ERRORS, STEP_CONTEXT, mark=True)


def resume(job: Job, edge: PlanEdge) -> int:
    """After the triggered builds: build, or fail with the cause.

    Steps 1 to 5 of enter; then, for each dependency variant still missing, a
    failure record of its key that carries this plan is its cause, and
    without one the build of that dependency left no record. Resume never
    triggers. With nothing missing it goes on at step 11.
    """
    return gated(job, edge, resume_decisions, ENTER_ERRORS, STEP_CONTEXT, mark=True)


# ---------------------------------------------------------------------------
# On an agent
# ---------------------------------------------------------------------------

TOKEN_VARIABLE = "ARTIFACTS_BUILDKITE_TOKEN"
TOKEN_SSM_PARAM = "/services/buildkite/credentials/artifacts/buildkite-api-token"

# The plugin's own checkout: lib/snapshot/enter.py is two levels below it.
PLUGIN_ROOT = Path(__file__).resolve().parents[2]


# What an HTTP header can carry of a token: printable ASCII without white space.
_SENDABLE_TOKEN = re.compile(r"[!-~]+")


def buildkite_token(ssm: Any) -> str:
    """The token of Buildkite's API: ``ARTIFACTS_BUILDKITE_TOKEN``, else its SSM parameter.

    The white space around it is dropped: a parameter written from a file ends
    with a newline. A token that is then not printable ASCII without white
    space cannot be sent, and is refused by where it was read. It goes to
    Buildkite's API only, never into a message.
    """
    variable = os.environ.get(TOKEN_VARIABLE)
    where = TOKEN_VARIABLE if variable else f"the SSM parameter {TOKEN_SSM_PARAM}"
    token = (env_or_ssm(ssm, TOKEN_VARIABLE, TOKEN_SSM_PARAM) or "").strip()
    if not token:
        raise ArtifactsError(
            f"no Buildkite API token: set {TOKEN_VARIABLE} or the SSM parameter {TOKEN_SSM_PARAM}"
        )
    if not _SENDABLE_TOKEN.fullmatch(token):
        raise ArtifactsError(
            f"the Buildkite API token in {where} is not printable ASCII without white space, "
            "so it cannot be sent"
        )
    return token


def buildkite_pipelines(ssm: Any) -> Pipelines:
    """Buildkite's facts of a pipeline slug.

    The organisation and the token are read at the first call, which only the
    plan of a root makes: a chain node never needs them.
    """

    @functools.cache
    def credentials() -> tuple[str, str]:
        return job_variable("BUILDKITE_ORGANIZATION_SLUG", "plan"), buildkite_token(ssm)

    def facts(slug: str) -> PipelineFacts:
        organisation, token = credentials()
        return fetch_pipeline(slug, organisation, token)

    return facts


def plugin_checkout_commit() -> str:
    """``git rev-parse HEAD`` of the plugin's own checkout: the plugin every job of the build runs."""
    return RealGit(cwd=str(PLUGIN_ROOT)).head_commit()


def real_edge() -> PlanEdge:
    """The edge of a plan or resume step on an agent: its store, agent and checkout, git in
    the checkout, Buildkite's API, the plugin's own commit."""
    _, ssm = aws_clients()
    store, prefix = opened_store(ssm)
    git = RealGit(log=log)
    return PlanEdge(
        store=store,
        prefix=prefix,
        agent=RealAgent(),
        checkout=git,
        clock=time.time,
        log=log,
        git=git,
        pipelines=buildkite_pipelines(ssm),
        plugin_commit=plugin_checkout_commit,
    )


def run(kind: str, pipeline: str) -> int:
    """The gate command of a step that carries ``plan`` or ``resume``, on an agent."""
    step = {"plan": enter, "resume": resume}[kind]
    return step(job_of(kind, pipeline), real_edge())
