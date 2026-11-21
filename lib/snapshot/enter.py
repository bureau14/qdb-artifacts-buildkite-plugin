"""enter and resume: what the plan step decides, in order (specification section 7).

A gated build runs them in the post-command of the steps that carry the
plugin's options ``plan`` (enter) and ``resume``, after the step's command
generated the pipeline:

- enter: reuse what is made, trigger the builds of dependencies that lack a
  step, or upload the pipeline with the key of every step a download takes
  written into it;
- resume, after the triggered builds ended: build, or fail with the cause.

What a build makes is a set of folders of its key, each a platform and a step
id; a folder is made when its manifest says promoted. Each step is its
decisions in the specification's order, under its numbers; the first decision
that ends the step ends it. A decision is a function of what the plan step's
edge (``PlanEdge``) returned. A decision that fails the step raises a
``failing.Refusal``.
"""

from __future__ import annotations

import functools
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NoReturn

from ..clients import aws_clients
from ..config import env_or_ssm
from ..errors import ArtifactsError
from ..key import canonical_json
from ..layout import Folder
from ..log import log
from ..manifest import Manifest
from .agent import RealAgent
from .annotation import (
    PLAN_REFUSED,
    caused_annotation,
    failure_annotation,
    left_no_record,
    plan_annotation,
    refusal_annotation,
    refused_line,
    reused_annotation,
    shown_folders,
)
from .buildkite import PipelineFacts, fetch_pipeline
from .configuration import entry_steps
from .declaration import (
    SNAPSHOT,
    Declaration,
    TemplateError,
    declared_path,
    is_template_path,
    pipeline_downloads,
    read_declaration,
    rule_violations,
    violation_message,
)
from .document import (
    DocumentError,
    Rewrite,
    Taken,
    dump_document,
    dump_written,
    entry_taking,
    load_document,
    needed_folders,
    planned,
    promote_entries,
    promotions,
    read_needs,
    resume_pipeline,
    stepless_downloads,
    unwritten_promotions,
)
from .edge import (
    BUILT,
    MODE,
    PLAN,
    PROMOTES,
    REUSED,
    Edge,
    chain_plan,
    chain_records,
    create_own_key_json,
    made_only,
    manifests_at,
    opened_store,
    root_arrival,
    stored_records,
)
from .failing import Refusal, gated
from .git import Git, RealGit, branch_ref
from .job import Job, is_chain_node, job_of
from .plan import Plan, PlanError, encode_plan, short_commit
from .planner import Pipelines, Root, make_plan, rebuild_setting
from .records import (
    GENERATOR_FAILED,
    NOT_DECLARED,
    RULE,
    Cause,
    Origin,
    Record,
    RefusedFolder,
    caused_record,
    chain_failure,
    reason_record,
    refused_record,
    shared_failure,
    shared_record,
)

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


def create_chain_key_json(job: Job, edge: Edge) -> Origin | None:
    """For a chain node, which has its plan from the start, ``create_own_key_json`` before the
    record of a refusal in step 1 or 2; the origin of that record. None for a root: it knows
    no key before it made its plan (step 3), so it records nothing there."""
    if not is_chain_node(job):
        return None
    return create_own_key_json(job, edge, chain_plan(job, edge))


# ---------------------------------------------------------------------------
# Steps 1 to 5, of enter and of resume
# ---------------------------------------------------------------------------


def check_command(job: Job, edge: Edge) -> None:
    """1. The command failed: record ``generator_failed`` when a chain node; fail."""
    if job.exit_status != 0:
        message = (
            f"the command exited {job.exit_status}: it generates the pipeline into "
            f"{job.pipeline}, which this step plans"
        )
        origin = create_chain_key_json(job, edge)
        raise reason_refusal(job, GENERATOR_FAILED, message, origin, edge.clock())


@dataclass(frozen=True)
class Generated:
    """The generated pipeline as loaded, and the declaration of the build's own commit."""

    doc: Any
    declaration: Declaration


def generated_pipeline(job: Job, edge: Edge) -> Generated:
    """2. Check the rule of section 2; failing, record ``rule`` when a chain node; fail.

    A template of the build's own commit that does not parse fails the build
    that reads it for itself, as the rule does (``TemplateError``).
    """
    doc = load_document(Path(job.pipeline or "").read_text(encoding="utf-8"))
    files = edge.checkout.checkout_files(TEMPLATE_DIRECTORY, is_template_path)
    try:
        declaration = read_declaration(files, job.project)
    except TemplateError as exc:
        origin = create_chain_key_json(job, edge)
        raise reason_refusal(job, RULE, str(exc), origin, edge.clock()) from exc
    downloads = pipeline_downloads(doc)
    # The build's own ref, as the generator writes it from BUILDKITE_BRANCH.
    violations = rule_violations(downloads, declaration, job.project, branch_ref(job.branch))
    if violations:
        message = "\n".join(violation_message(v) for v in violations)
        origin = create_chain_key_json(job, edge)
        raise reason_refusal(job, RULE, message, origin, edge.clock())
    return Generated(doc, declaration)


def root_plan(job: Job, edge: PlanEdge, generated: Generated) -> Plan:
    """3, a root: make the plan (sections 3 and 4), write it, annotate the build with it.

    A plan that cannot be made fails the build with its message.
    """
    try:
        root = Root(
            project=job.project,
            branch=job.branch,
            commit=edge.checkout.head_commit(),
            build_id=job.build.id,
            build_number=job.build.number,
            rebuild=rebuild_setting(job.rebuild),
        )
        p = make_plan(root, generated.declaration, edge.git, edge.pipelines)
    except PlanError as exc:
        annotation = refusal_annotation(PLAN_REFUSED, job.project, job.build, str(exc))
        raise Refusal(str(exc), annotation) from exc
    edge.agent.meta_data_set(PLAN, encode_plan(p))
    edge.agent.annotate(plan_annotation(p, job.project), "info", PLAN_CONTEXT)
    return p


# How step 3 comes to the plan: in enter (``entered_plan``) or in resume (``resumed_plan``).
PlanOf = Callable[[Job, PlanEdge, Generated], Plan]


def entered_plan(job: Job, edge: PlanEdge, generated: Generated) -> Plan:
    """3, enter. A chain node loads the plan of its chain; a root makes it."""
    return chain_plan(job, edge) if is_chain_node(job) else root_plan(job, edge, generated)


def resumed_plan(job: Job, edge: PlanEdge, generated: Generated) -> Plan:
    """3, resume. Every build loads the plan of its chain, a root its own: the plan its enter
    made, which names the commits the triggered builds built."""
    return chain_plan(job, edge)


def promoted(job: Job, edge: Edge, generated: Generated, origin: Origin) -> frozenset[Folder]:
    """5. ``D``, the folders this pipeline promotes; a folder promoted twice fails, as the rule.

    Two steps that promote one folder break a rule the plan step holds a
    pipeline that has a snapshot to, so the record's reason is ``rule``.
    """
    try:
        return frozenset(promotions(generated.doc))
    except DocumentError as exc:
        raise reason_refusal(job, RULE, str(exc), origin, edge.clock()) from exc


def check_step_ids(job: Job, edge: Edge, generated: Generated, p: Plan, origin: Origin) -> None:
    """5. Every step id this pipeline promotes is one a template of its commit writes, which
    the builds that depend on it read; and every dependency a download takes without naming
    a step has step ids. Else record ``rule``; fail."""
    broken = [
        *unwritten_promotions(generated.doc, job.project, generated.declaration.steps),
        *stepless_downloads(generated.doc, job.project, p),
    ]
    if broken:
        raise reason_refusal(job, RULE, "\n".join(broken), origin, edge.clock())


def asked(job: Job, d: frozenset[Folder]) -> frozenset[Folder]:
    """5. ``N``: ``SNAPSHOT_NEEDS`` for a chain node, else ``D``."""
    return read_needs(job.needs) if is_chain_node(job) else d


def refused_folders(n: frozenset[Folder], d: frozenset[Folder]) -> list[RefusedFolder]:
    """The folders of ``N`` that are not in ``D``, each with the step ids ``D`` holds of its
    platform."""
    return [
        RefusedFolder(f, tuple(sorted(g.step for g in d if g.platform == f.platform)))
        for f in sorted(n - d)
    ]


def check_declared(
    job: Job, edge: Edge, n: frozenset[Folder], d: frozenset[Folder], origin: Origin
) -> None:
    """5. A folder of ``N`` that is not in ``D``: record ``not_declared`` with the folders
    refused; fail. Nothing compiles."""
    refused = refused_folders(n, d)
    if refused:
        message = (
            f"asked for {shown_folders(x.folder for x in refused)}, which this pipeline does "
            f"not promote; it promotes {shown_folders(sorted(d)) or 'nothing'}"
        )
        record = refused_record(origin, refused, edge.clock())
        annotation = refusal_annotation(NOT_DECLARED, job.project, job.build, message)
        raise Refusal(message, annotation, record)


# ---------------------------------------------------------------------------
# Steps 6 and 7, of enter
# ---------------------------------------------------------------------------


def is_reused(d: frozenset[Folder], n: frozenset[Folder], m: frozenset[Folder]) -> bool:
    """6. ``D`` is not empty and no folder of ``N`` is in ``M``, the folders not made."""
    return bool(d) and not (n & m)


def reuse(
    job: Job, edge: Edge, p: Plan, made: Mapping[Folder, Manifest], n: frozenset[Folder]
) -> None:
    """6. Mode ``reused``, and the builds that made the folders asked for on the build page."""
    builds = {f: made[f].promoted.build for f in n}
    edge.agent.meta_data_set(MODE, REUSED)
    edge.agent.annotate(reused_annotation(p, job.project, builds), "success", STEP_CONTEXT)
    edge.log(f"{job.project} is reused: every step asked for is made")


def check_shared(job: Job, edge: Edge, p: Plan, origin: Origin, wanted: frozenset[Folder]) -> None:
    """7. Another build of ``K`` hard failed on every wanted folder after the root build of
    this chain arrived at the gate: fail, compiling nothing.

    If it fails, it fails everywhere: the failure counts for every build of a
    chain whose root started before it was recorded, also one that reaches
    ``K`` later. A new root build builds ``K`` again. The own record copies
    those entries and names that build under ``shared_with``; such a copy is
    never shared itself (``shared_failure``). An empty outcome or any other
    state is not a failure.
    """
    if not wanted:
        return
    records = stored_records(edge, origin.project, origin.key)
    if not records:
        return
    source = shared_failure(records, job.build.id, root_arrival(job, edge), wanted)
    if source is None:
        return
    own = shared_record(origin, source, wanted, edge.clock())
    message = (
        f"build {source.build.number} of the same snapshot failed on "
        f"{shown_folders(sorted(wanted))} after the root build of this chain started: "
        f"{source.build.url}"
    )
    raise Refusal(message, failure_annotation(p, own, {}), own)


# ---------------------------------------------------------------------------
# Steps 8 to 11
# ---------------------------------------------------------------------------


def dependency_key(p: Plan, project: str) -> str:
    """The key of a snapshot dependency of this build, as the plan fixed it."""
    return p.projects[project].key


def steps_of(p: Plan) -> dict[str, tuple[str, ...]]:
    """The step ids of every project of the plan, which an entry that names no step takes."""
    return {project: n.steps for project, n in p.projects.items()}


def dependency_manifests(
    edge: Edge, p: Plan, needs: Mapping[str, frozenset[Folder]]
) -> dict[tuple[str, Folder], Manifest | None]:
    """8. The manifest of each folder of each dependency at its key; None where none is."""
    return {
        (project, f): found
        for project, folders in sorted(needs.items())
        for f, found in manifests_at(edge, project, dependency_key(p, project), folders).items()
    }


def missing_folders(
    found: Mapping[tuple[str, Folder], Manifest | None],
) -> dict[str, tuple[Folder, ...]]:
    """8. The folders of each dependency that are not made at its key, sorted."""
    made = made_only(found)
    missing: dict[str, list[Folder]] = {}
    for project, f in sorted(found):
        if (project, f) not in made:
            missing.setdefault(project, []).append(f)
    return {project: tuple(folders) for project, folders in missing.items()}


def cause_message(p: Plan, c: Cause, records: Mapping[str, Record]) -> str:
    """One line of the message of a build that a dependency left without steps; for a
    dependency that refused them, what leads to the remedy."""
    if c.template is not None:
        return refused_line(p, c, records[c.key])
    lacks = (
        f"{c.project} @ {short_commit(p.projects[c.project].commit)} ({c.key}) has no "
        f"{shown_folders(c.missing)}"
    )
    if c.key in records:
        return f"{lacks}: its build {records[c.key].build.number} failed in this chain"
    return f"{lacks}: {left_no_record(p, c)}"


def with_asking_entry(
    job: Job, p: Plan, generated: Generated, c: Cause, records: Mapping[str, Record]
) -> Cause:
    """``c``, when the build of its dependency in this chain refused steps it does not make,
    with the template of the entry that asked for them and the steps that entry names (None
    for every step its dependency's templates name): the entry a person changes."""
    r = records.get(c.key)
    if r is None or r.reason != NOT_DECLARED:
        return c
    steps = p.projects[c.project].steps
    refused = [x.folder for x in r.refused]
    entry = entry_taking(generated.doc, job.project, c.project, steps, refused)
    template = declared_path(generated.declaration, c.project, SNAPSHOT)
    return replace(c, template=template, takes=entry_steps(entry) or None)


def fail_with_causes(
    job: Job,
    edge: Edge,
    p: Plan,
    generated: Generated,
    causes: list[Cause],
    origin: Origin | None,
) -> NoReturn:
    """Fail a build that its dependencies left without steps, each named as a cause."""
    records = chain_records(edge, p, causes)
    named = [with_asking_entry(job, p, generated, c, records) for c in causes]
    message = "\n".join(cause_message(p, c, records) for c in named)
    record = None if origin is None else caused_record(origin, named, edge.clock())
    annotation = caused_annotation(p, job.project, job.build, named, records)
    raise Refusal(message, annotation, record)


def failed_in_chain(
    job: Job,
    edge: Edge,
    p: Plan,
    generated: Generated,
    missing: Mapping[str, tuple[Folder, ...]],
    origin: Origin | None,
) -> None:
    """9. A dependency with a missing folder failed earlier in this chain: fail, triggering
    nothing.

    Its failure record of ``key(P)`` carries this plan. The own record names it
    under ``caused_by``. Within one chain a key is built at most once.
    """
    lacking = [Cause(project, dependency_key(p, project), fs) for project, fs in missing.items()]
    causes = [c for c in lacking if chain_failure(stored_records(edge, c.project, c.key), p.id)]
    if causes:
        fail_with_causes(job, edge, p, generated, causes, origin)


def trigger(
    job: Job,
    edge: PlanEdge,
    p: Plan,
    needs: Mapping[str, frozenset[Folder]],
    missing: Mapping[str, tuple[Folder, ...]],
) -> None:
    """10. One trigger step per dependency with a missing folder, and the resume step.

    Each trigger step hands down every folder this pipeline's entries take of
    its dependency. The resume step runs this step's command again.
    """
    wanted = {project: needs[project] for project in missing}
    document = resume_pipeline(p, wanted, job.command, edge.plugin_commit(), job.pipeline or "")
    edge.agent.pipeline_upload(dump_written(document))
    edge.log(f"triggered {', '.join(sorted(missing))}; the resume step goes on when they end")


def build_pipeline(
    job: Job,
    edge: PlanEdge,
    p: Plan,
    generated: Generated,
    key: str | None,
    made: Mapping[Folder, Manifest],
) -> None:
    """11. Rewrite the generated pipeline (section 8), mark the build ``built``, upload it.

    With a key, ``snapshot-promotes`` names the step of each folder that is
    built: a replaced step promotes nothing, so leave asks for no outcome of
    it. The marks leave reads are set before the upload, so that no uploaded
    step runs in a build without them; an upload the agent refuses then fails
    the step, which sets the mode ``failed``.
    """
    deps = p.projects[job.project].deps
    taken = {d: Taken(dependency_key(p, d), p.projects[d].steps) for d in deps}
    out = planned(generated.doc, Rewrite(job.project, key, edge.plugin_commit(), taken, made))
    document = dump_document(out)
    if key is not None:
        promotes = canonical_json(promote_entries(out)).decode("utf-8")
        edge.agent.meta_data_set(PROMOTES, promotes)
    edge.agent.meta_data_set(MODE, BUILT)
    edge.agent.pipeline_upload(document)
    edge.log(f"{job.project} builds: uploaded the pipeline with the keys of {len(deps)} projects")


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
    d: frozenset[Folder]
    n: frozenset[Folder]


def first_steps(job: Job, edge: PlanEdge, plan_of: PlanOf) -> Prepared:
    """Steps 1 to 5, the same in enter and resume but for how step 3 comes to the plan.

    Step 3 also checks that the plan plans this build at the commit it checked
    out, and creates ``key.json`` of its key before any record or upload.
    """
    check_command(job, edge)
    generated = generated_pipeline(job, edge)
    p = plan_of(job, edge, generated)
    origin = create_own_key_json(job, edge, p)
    if origin is None:
        # 4. Only a root can be here: its declaration holds a latest successful dependency.
        edge.log(f"{job.project} has no snapshot: it is built, and never reused")
        return Prepared(generated, p, None, None, frozenset(), frozenset())
    d = promoted(job, edge, generated, origin)
    check_step_ids(job, edge, generated, p, origin)
    n = asked(job, d)
    check_declared(job, edge, n, d, origin)
    return Prepared(generated, p, origin.key, origin, d, n)


def own_manifests(job: Job, edge: Edge, prepared: Prepared) -> dict[Folder, Manifest | None]:
    """The manifest of each folder of ``D`` at ``K``; none without a key."""
    if prepared.key is None:
        return {}
    return manifests_at(edge, job.project, prepared.key, prepared.d)


def enter_decisions(job: Job, edge: PlanEdge) -> None:
    prepared = first_steps(job, edge, entered_plan)
    found = own_manifests(job, edge, prepared)
    made = made_only(found)
    if prepared.origin is not None:
        m = frozenset(found) - frozenset(made)
        if is_reused(prepared.d, prepared.n, m):
            reuse(job, edge, prepared.plan, made, prepared.n)
            return
        check_shared(job, edge, prepared.plan, prepared.origin, prepared.n & m)
    needs = needed_folders(prepared.generated.doc, job.project, made, steps_of(prepared.plan))
    missing = missing_folders(dependency_manifests(edge, prepared.plan, needs))
    if missing:
        failed_in_chain(job, edge, prepared.plan, prepared.generated, missing, prepared.origin)
        trigger(job, edge, prepared.plan, needs, missing)
        return
    build_pipeline(job, edge, prepared.plan, prepared.generated, prepared.key, made)


def resume_decisions(job: Job, edge: PlanEdge) -> None:
    prepared = first_steps(job, edge, resumed_plan)
    made = made_only(own_manifests(job, edge, prepared))
    needs = needed_folders(prepared.generated.doc, job.project, made, steps_of(prepared.plan))
    missing = missing_folders(dependency_manifests(edge, prepared.plan, needs))
    if missing:
        causes = [
            Cause(project, dependency_key(prepared.plan, project), folders)
            for project, folders in missing.items()
        ]
        fail_with_causes(job, edge, prepared.plan, prepared.generated, causes, prepared.origin)
    build_pipeline(job, edge, prepared.plan, prepared.generated, prepared.key, made)


def enter(job: Job, edge: PlanEdge) -> int:
    """The plan step: decide what this build does.

    1. The command failed: record ``generator_failed`` when a chain node; fail.
    2. The generated pipeline breaks the rule of section 2: record ``rule``
       when a chain node; fail.
    3. A chain node loads the plan and checks its commit; a root makes the
       plan, writes it and annotates the build with it. A plan that cannot be
       made fails the build with its message. Then ``key.json`` of the own key.
    4. The own project has no snapshot: go to 8 with no key.
    5. ``D`` is what this pipeline promotes, a folder promoted twice failing;
       a step id no template of the commit writes, or a download without a
       step of a dependency that has no step ids: record ``rule``; fail.
       ``N`` is ``SNAPSHOT_NEEDS`` for a chain node, else ``D``. A folder of
       ``N`` not in ``D``: record ``not_declared`` with what it refuses; fail.
    6. ``D`` is not empty and every folder of ``N`` is made at ``K``: mode
       reused; done.
    7. Another build of ``K`` failed on the wanted folders after the chain's
       root arrived at the gate: record its failure as shared; fail.
    8. For each direct snapshot dependency, the folders the kept steps take,
       and which of them are not made at its key.
    9. A dependency with a missing folder failed earlier in this chain:
       record the cause; fail. Nothing is triggered.
    10. Some are missing: upload a trigger step per such dependency and the
        resume step; done.
    11. Else mode built, and upload the rewritten pipeline; done.
    """
    return gated(job, edge, enter_decisions, STEP_CONTEXT, mark=True)


def resume(job: Job, edge: PlanEdge) -> int:
    """After the triggered builds: build, or fail with the cause.

    Steps 1 to 5 of enter, except that in step 3 a root, too, loads the plan:
    the one its enter made. Then, for each dependency folder still missing,
    a failure record of its key that carries this plan is its cause, and
    without one the build of that dependency left no record. Resume never
    triggers. With nothing missing it goes on at step 11.
    """
    return gated(job, edge, resume_decisions, STEP_CONTEXT, mark=True)


# ---------------------------------------------------------------------------
# On an agent
# ---------------------------------------------------------------------------

TOKEN_VARIABLE = "ARTIFACTS_BUILDKITE_TOKEN"
TOKEN_SSM_PARAM = "/services/buildkite/credentials/artifacts/buildkite-api-token"

# The plugin's own checkout: lib/snapshot/enter.py is two levels below it.
PLUGIN_ROOT = Path(__file__).resolve().parents[2]


def buildkite_token(ssm: Any) -> str:
    """The token of Buildkite's API: ``ARTIFACTS_BUILDKITE_TOKEN``, else its SSM parameter.

    The white space around it is dropped: a parameter written from a file ends
    with a newline. It goes to Buildkite's API only, never into a message.
    """
    token = (env_or_ssm(ssm, TOKEN_VARIABLE, TOKEN_SSM_PARAM) or "").strip()
    if not token:
        raise ArtifactsError(
            f"no Buildkite API token: set {TOKEN_VARIABLE} or the SSM parameter {TOKEN_SSM_PARAM}"
        )
    return token


def buildkite_pipelines(ssm: Any) -> Pipelines:
    """Buildkite's facts of a pipeline slug.

    The organisation and the token are read at the first call, which only the
    plan of a root makes: a chain node never needs them.
    """

    @functools.cache
    def credentials() -> tuple[str, str]:
        return os.environ["BUILDKITE_ORGANIZATION_SLUG"], buildkite_token(ssm)

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
    return step(job_of(pipeline), real_edge())
