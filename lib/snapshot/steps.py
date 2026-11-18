"""enter, resume and leave: what the gate steps decide, in order (specification section 7).

A gated build runs its gate steps in the post-command of steps that carry the
plugin's gate options (``lib/cli.py``):

- enter (option ``plan``), after the step's command generated the pipeline:
  reuse what exists, trigger the builds of dependencies that lack a variant,
  or upload the pipeline with one build of every snapshot dependency written
  into it;
- resume (option ``resume``), after the triggered builds ended: build, or fail
  with the cause;
- leave (option ``leave``), after every other step of the build: record the
  variants whose step did not pass.

Each step is its decisions in the specification's order, under its numbers;
the first decision that ends the step ends it. A decision is a function of
what the edge returned (``Edge``): the store, ``buildkite-agent``, git,
Buildkite's API, the clock and the log. The steps keep no state but these
marks on their own build, which a retried step and leave read back:

    snapshot-mode                  reused, built or failed
    snapshot-promotes              {variant: step key} of the steps that promote
    snapshot-uploaded-<step key>   this build's id, once the step has uploaded

"Fail" is: write the failure record the decision names, when the build has a
key; show why on the build page; set the mode ``failed``; end with
``ArtifactsError``, which the command line turns into exit status 1. An error
of the edge fails the step the same way and writes no record: a record saves a
waiting build from compiling in vain, and an error of the store, of git or of
an API says nothing of the build of a key.

Leave reads no pipeline document, and post-checkout installs PyYAML only on
the steps of enter and resume. So the modules that read documents
(``declaration``, ``document``, ``planner``, ``buildkite``) are imported where
enter and resume use them, never at the top of this module.
"""

from __future__ import annotations

import functools
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from botocore.exceptions import BotoCoreError, ClientError

from .. import layout, pointer
from ..clients import aws_clients, resolve_object_auth
from ..config import env_or_ssm, load_store_config
from ..errors import ArtifactsError
from ..forms import BRANCH_PREFIX, is_build_id, is_https_url, is_slug, is_variant
from ..log import log
from ..objects import Store, destination_store
from ..options import command_exit_status, job_build_number, job_variable
from ..pointer import Build, Pointer
from .agent import Agent, AgentError, MetaDataAbsent, RealAgent
from .annotation import (
    PLAN_REFUSED,
    STEP_ERROR,
    caused_annotation,
    failure_annotation,
    left_no_record,
    plan_annotation,
    refusal_annotation,
    reused_annotation,
)
from .canonical import canonical_json
from .git import Checkout, Git, GitError, RealGit
from .plan import Node, Plan, PlanError, decode_plan, encode_plan, planned_node
from .records import (
    GENERATOR_FAILED,
    NOT_DECLARED,
    PASSED,
    RULE,
    Cause,
    Origin,
    Record,
    RecordError,
    caused_record,
    chain_failure,
    decode_record,
    encode_record,
    is_step_key,
    leave_record,
    reason_record,
    shared_failure,
    shared_record,
)
from .shown import short_commit, shown_text, shown_value

# The marks on the build, and the meta-data a step reads.
MODE = "snapshot-mode"
REUSED, BUILT, FAILED = "reused", "built", "failed"
PROMOTES = "snapshot-promotes"
UPLOADED = "snapshot-uploaded-"
PLAN = "snapshot-plan"
ARRIVED = "snapshot-arrived"

# One annotation per context: a retried step replaces its own.
PLAN_CONTEXT = "snapshot-plan"
STEP_CONTEXT = "snapshot"
LEAVE_CONTEXT = "snapshot-leave"

TEMPLATE_DIRECTORY = ".buildkite"

TOKEN_VARIABLE = "ARTIFACTS_BUILDKITE_TOKEN"
TOKEN_SSM_PARAM = "/services/buildkite/credentials/artifacts/buildkite-api-token"

# The plugin's own checkout: lib/snapshot/steps.py is two levels below it.
PLUGIN_ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# The job
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Job:
    """What a gate step knows of its job: its option and the variables the agent sets.

    ``kind`` is ``plan``, ``resume`` or ``leave``; ``pipeline`` the path of the
    generated pipeline that the option of ``plan`` and ``resume`` names.
    ``plan_id`` is ``SNAPSHOT_PLAN`` (the root build's uuid) of a chain node,
    None for a root; ``needs`` and ``rebuild`` are ``SNAPSHOT_NEEDS`` and
    ``SNAPSHOT_REBUILD`` as given.
    """

    kind: str
    pipeline: str | None
    step_key: str
    project: str
    build: Build
    branch: str
    command: str | None
    exit_status: int
    plan_id: str | None
    needs: str | None
    rebuild: str | None


def is_chain_node(job: Job) -> bool:
    """A chain node has ``SNAPSHOT_PLAN`` in its environment, also an empty one."""
    return job.plan_id is not None


def job_build(option: str) -> Build:
    """This job's build, of the form a pointer and a record hold."""
    b = Build(
        id=job_variable("BUILDKITE_BUILD_ID", option),
        number=job_build_number(option),
        url=job_variable("BUILDKITE_BUILD_URL", option),
    )
    if not is_build_id(b.id):
        raise ArtifactsError(f"BUILDKITE_BUILD_ID {shown_value(b.id)} is not a build uuid")
    if not is_https_url(b.url):
        raise ArtifactsError(f"BUILDKITE_BUILD_URL {shown_value(b.url)} is not an https URL")
    return b


def job_of(kind: str, pipeline: str | None) -> Job:
    """The job of a gate step of ``kind``, from the variables of its environment."""
    project = job_variable("BUILDKITE_PIPELINE_SLUG", kind)
    if not is_slug(project):
        raise ArtifactsError(f"BUILDKITE_PIPELINE_SLUG {shown_value(project)} is not a slug")
    return Job(
        kind=kind,
        pipeline=pipeline,
        step_key=job_variable("BUILDKITE_STEP_KEY", kind),
        project=project,
        build=job_build(kind),
        branch=os.environ.get("BUILDKITE_BRANCH", ""),
        command=os.environ.get("BUILDKITE_COMMAND"),
        exit_status=command_exit_status(),
        plan_id=os.environ.get("SNAPSHOT_PLAN"),
        needs=os.environ.get("SNAPSHOT_NEEDS"),
        rebuild=os.environ.get("SNAPSHOT_REBUILD"),
    )


def own_ref(job: Job) -> str:
    """The build's own ref, as the generator writes it from ``BUILDKITE_BRANCH``."""
    return BRANCH_PREFIX + job.branch


# ---------------------------------------------------------------------------
# The edge
# ---------------------------------------------------------------------------

# Buildkite's facts of a pipeline slug (``planner.Pipelines``).
Pipelines = Callable[[str], Any]


@dataclass(frozen=True)
class Edge:
    """Everything a gate step reads or changes, each behind the interface of its module.

    ``store`` and ``prefix``: the destination's store, opened as a job opens
    it (``objects.destination_store``). ``agent``: ``buildkite-agent``.
    ``git``: other repositories; ``checkout``: the build's own. ``pipelines``:
    Buildkite's facts of a pipeline slug. ``plugin_commit``: the commit of the
    plugin's own checkout. ``clock``: epoch seconds. ``log``: one line to the
    job log.
    """

    store: Store
    prefix: str
    agent: Agent
    git: Git
    checkout: Checkout
    pipelines: Pipelines
    plugin_commit: Callable[[], str]
    clock: Callable[[], float]
    log: Callable[[str], None]


def buildkite_token(ssm: Any) -> str:
    """The token of Buildkite's API: ``ARTIFACTS_BUILDKITE_TOKEN``, else its SSM parameter.

    It goes to Buildkite's API only, never into a message.
    """
    token = env_or_ssm(ssm, TOKEN_VARIABLE, TOKEN_SSM_PARAM)
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
        return job_variable("BUILDKITE_ORGANIZATION_SLUG", "plan"), buildkite_token(ssm)

    def facts(slug: str) -> Any:
        from .buildkite import fetch_pipeline

        organisation, token = credentials()
        return fetch_pipeline(slug, organisation, token)

    return facts


def plugin_checkout_commit() -> str:
    """``git rev-parse HEAD`` of the plugin's own checkout: the plugin every job of the build runs."""
    return RealGit(cwd=str(PLUGIN_ROOT)).head_commit()


def real_edge() -> Edge:
    """The edge of a job: its store, its agent, git in its checkout, Buildkite's API."""
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    store, prefix = destination_store(cfg, resolve_object_auth(ssm, cfg, "object-read-write"))
    git = RealGit(log=log)
    return Edge(
        store=store,
        prefix=prefix,
        agent=RealAgent(),
        git=git,
        checkout=git,
        pipelines=buildkite_pipelines(ssm),
        plugin_commit=plugin_checkout_commit,
        clock=time.time,
        log=log,
    )


# ---------------------------------------------------------------------------
# Reading through the edge
# ---------------------------------------------------------------------------


def meta_data(edge: Edge, key: str, build: str | None = None) -> str | None:
    """The value of ``key`` on this build, or on ``build``; None when it is not set."""
    try:
        return edge.agent.meta_data_get(key, build)
    except MetaDataAbsent:
        return None


def pointers_at(
    edge: Edge, project: str, key: str, variants: Iterable[str]
) -> dict[str, Pointer | None]:
    """The pointer of each variant of ``project`` at ``key``; None where no build holds it."""
    return {v: pointer.find(edge.store, edge.prefix, project, key, v) for v in sorted(variants)}


def held_only(held: Mapping[Any, Pointer | None]) -> dict[Any, Pointer]:
    """The pointers of ``held`` that a build holds."""
    return {k: found for k, found in held.items() if found is not None}


def decoded_record(edge: Edge, where: str, project: str, key: str) -> Record | None:
    """The record stored at ``where``; None, with a line in the log, when it is none of the key."""
    stored = edge.store.get(where)
    if stored is None:
        return None
    try:
        r = decode_record(stored.body.decode("utf-8"))
    except (RecordError, UnicodeDecodeError) as exc:
        edge.log(f"skipped s3://{edge.store.bucket}/{where}: {shown_text(str(exc))}")
        return None
    if (r.project, r.key) != (project, key):
        edge.log(f"skipped s3://{edge.store.bucket}/{where}: a record of {r.project} at {r.key}")
        return None
    return r


def stored_records(edge: Edge, project: str, key: str) -> list[Record]:
    """Every failure record of ``project`` at ``key`` that decodes."""
    listed = edge.store.list(layout.failure_records(edge.prefix, project, key))
    found = (decoded_record(edge, where, project, key) for where, _ in listed)
    return [r for r in found if r is not None]


def create_record(edge: Edge, r: Record) -> None:
    """Write ``r`` once: a record already at its place is left as it is."""
    where = layout.failure_record(edge.prefix, r.project, r.key, r.build.id)
    if edge.store.create(where, encode_record(r).encode("utf-8"), "application/json"):
        edge.log(f"wrote the failure record s3://{edge.store.bucket}/{where}")
    else:
        edge.log(f"s3://{edge.store.bucket}/{where} exists; left as it is")


def chain_records(edge: Edge, plan: Plan, causes: Iterable[Cause]) -> dict[str, Record]:
    """The record of this chain of every key the causes name, and the keys their records name."""
    found: dict[str, Record] = {}
    asked: set[str] = set()
    pending = list(causes)
    while pending:
        c = pending.pop()
        if c.key in asked:
            continue
        asked.add(c.key)
        r = chain_failure(stored_records(edge, c.project, c.key), plan.id)
        if r is not None:
            found[c.key] = r
            pending.extend(r.caused_by)
    return found


def arrival(edge: Edge) -> int:
    """When this build arrived at the gate: ``snapshot-arrived``, epoch seconds the runner set."""
    value = meta_data(edge, ARRIVED)
    if value is None or not re.fullmatch(r"[0-9]+", value):
        raise ArtifactsError(
            f"{ARRIVED} is {shown_value(value)}, not epoch seconds: the runner sets it when it "
            "prints the gate"
        )
    return int(value)


# ---------------------------------------------------------------------------
# The plan of a build
# ---------------------------------------------------------------------------


def chain_plan(job: Job, edge: Edge) -> Plan:
    """The plan of a chain node's chain, read from the meta-data of its root build."""
    if not job.plan_id:
        raise ArtifactsError("SNAPSHOT_PLAN is empty: a chain node names the build of its plan")
    text = meta_data(edge, PLAN, job.plan_id)
    if text is None:
        raise ArtifactsError(f"build {shown_value(job.plan_id)} holds no {PLAN}")
    p = decode_plan(text)
    if p.id != job.plan_id:
        raise PlanError(f"build {job.plan_id} holds the plan {p.id}")
    return p


def own_plan(job: Job, edge: Edge) -> Plan | None:
    """The plan a root wrote on its own build; None before it wrote one."""
    text = meta_data(edge, PLAN)
    if text is None:
        return None
    p = decode_plan(text)
    if p.id != job.build.id or p.root.project != job.project:
        raise PlanError(f"this build holds the plan {p.id} of {p.root.project}")
    return p


def own_node(job: Job, edge: Edge, p: Plan) -> Node:
    """This build's node of the plan, which must plan it at the commit it checked out."""
    return planned_node(p, job.project, edge.checkout.head_commit())


def origin_of(job: Job, p: Plan, key: str) -> Origin:
    return Origin(job.project, key, p.id, job.build)


# ---------------------------------------------------------------------------
# Failing
# ---------------------------------------------------------------------------


class Refusal(Exception):
    """A decision that fails the step: its message, its annotation, and its record, if any."""

    def __init__(self, message: str, annotation: str, record: Record | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.annotation = annotation
        self.record = record


# What the edge raises when it fails, and what the planning package raises for
# a plan read from elsewhere. Enter and resume add the errors of the modules
# that read documents.
EDGE_ERRORS = (ArtifactsError, AgentError, GitError, PlanError, ClientError, BotoCoreError)


def error_refusal(job: Job, error: Exception) -> Refusal:
    """The refusal of a step that an error of the edge ended: no record."""
    message = str(error) or type(error).__name__
    shown = "\n".join(shown_text(line) for line in message.splitlines())
    return Refusal(message, refusal_annotation(STEP_ERROR, job.project, job.build, shown))


def reason_refusal(
    job: Job, reason: str, message: str, origin: Origin | None, now: float
) -> Refusal:
    """A refusal before anything is built, with the record of ``reason`` when there is an origin."""
    record = None if origin is None else reason_record(origin, reason, now)
    return Refusal(message, refusal_annotation(reason, job.project, job.build, message), record)


def chain_origin(job: Job, edge: Edge) -> Origin | None:
    """What a record of this build says of it, for a chain node with a key; None for a root.

    A root knows no key before it made its plan (step 4).
    """
    if not is_chain_node(job):
        return None
    p = chain_plan(job, edge)
    node = own_node(job, edge, p)
    return None if node.key is None else origin_of(job, p, node.key)


def survive(edge: Edge, effect: Callable[[], Any], what: str) -> str:
    """Run one effect of a failing step; a further failure is logged and said, never raised."""
    try:
        effect()
        return ""
    except EDGE_ERRORS as exc:
        edge.log(f"could not {what}: {shown_text(str(exc))}")
        return f"; could not {what}: {shown_text(str(exc))}"


def fail(edge: Edge, refusal: Refusal, context: str, mark: bool) -> NoReturn:
    """Write the refusal's record, annotate, set the mode when ``mark``, and end the step."""
    said = ""
    if refusal.record is not None:
        r = refusal.record
        said += survive(edge, lambda: create_record(edge, r), "write the failure record")
    said += survive(
        edge, lambda: edge.agent.annotate(refusal.annotation, "error", context), "annotate"
    )
    if mark:
        said += survive(edge, lambda: edge.agent.meta_data_set(MODE, FAILED), "set the mode")
    raise ArtifactsError(refusal.message + said)


def gated(
    job: Job,
    edge: Edge,
    decisions: Callable[[Job, Edge], None],
    errors: tuple[type[BaseException], ...],
    context: str,
    mark: bool,
) -> int:
    """Run a step's decisions; a refusal, or an error of ``errors``, fails the step."""
    try:
        decisions(job, edge)
    except Refusal as refusal:
        fail(edge, refusal, context, mark)
    except errors as exc:
        fail(edge, error_refusal(job, exc), context, mark)
    return 0


# ---------------------------------------------------------------------------
# Steps 1 to 6, of enter and of resume
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


def uploaded_before(job: Job, edge: Edge) -> bool:
    """2. The step's marker holds this build's id: a retry after the upload."""
    return meta_data(edge, UPLOADED + job.step_key) == job.build.id


@dataclass(frozen=True)
class Generated:
    """The generated pipeline as loaded, and the reading of the build's own commit's templates."""

    doc: Any
    reading: Any


def generated_pipeline(job: Job, edge: Edge) -> Generated:
    """3. Check the rule of section 2; failing, record ``rule`` when a chain node; fail.

    A file the command did not leave, or one that does not load, is the
    generator's failure. A template of the build's own commit that does not
    parse fails the build that reads it for itself, as the rule does.
    """
    from .declaration import (
        is_template_path,
        pipeline_downloads,
        read_declaration,
        rule_violations,
        violation_message,
    )
    from .document import DocumentError, load_document

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


def root_plan(job: Job, edge: Edge, generated: Generated) -> Plan:
    """4, a root: make the plan (sections 3 and 4), write it, annotate the build with it.

    A retried step loads the plan it wrote. A plan that cannot be made fails
    the build with its message.
    """
    from .planner import Root, make_plan, rebuild_setting

    p = own_plan(job, edge)
    if p is None:
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
    edge.agent.annotate(plan_annotation(p, job.project), "info", PLAN_CONTEXT)
    return p


def plan_of(job: Job, edge: Edge, generated: Generated) -> tuple[Plan, Node]:
    """4. A chain node loads the plan and checks its commit; a root makes it."""
    p = chain_plan(job, edge) if is_chain_node(job) else root_plan(job, edge, generated)
    return p, own_node(job, edge, p)


def promoted_variants(job: Job, edge: Edge, generated: Generated, origin: Origin) -> frozenset[str]:
    """6. ``D``, the variants this pipeline promotes; a variant promoted twice fails, as the rule.

    Two steps that promote one variant break a rule the plan step holds a
    pipeline that has a snapshot to, so the record's reason is ``rule``.
    """
    from .document import DocumentError, promotions

    try:
        return frozenset(promotions(generated.doc, job.project))
    except DocumentError as exc:
        raise reason_refusal(job, RULE, str(exc), origin, edge.clock()) from exc


def asked(job: Job, promoted: frozenset[str]) -> frozenset[str]:
    """6. ``N``: ``SNAPSHOT_NEEDS`` for a chain node, else ``D``."""
    if not is_chain_node(job):
        return promoted
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
    job: Job, edge: Edge, needs: frozenset[str], promoted: frozenset[str], origin: Origin
) -> None:
    """6. A variant of ``N`` that is not in ``D``: record ``not_declared``; fail. Nothing compiles."""
    undeclared = sorted(needs - promoted)
    if undeclared:
        message = (
            f"asked for {', '.join(undeclared)}, which this pipeline does not promote; it "
            f"promotes {', '.join(sorted(promoted)) or 'nothing'}"
        )
        raise reason_refusal(job, NOT_DECLARED, message, origin, edge.clock())


# ---------------------------------------------------------------------------
# Steps 7 and 8, of enter
# ---------------------------------------------------------------------------


def is_reused(promoted: frozenset[str], needs: frozenset[str], absent: frozenset[str]) -> bool:
    """7. ``D`` is not empty and no variant of ``N`` is in ``M``, the variants without a pointer."""
    return bool(promoted) and not (needs & absent)


def reuse(
    job: Job, edge: Edge, p: Plan, held: Mapping[str, Pointer | None], needs: frozenset[str]
) -> None:
    """7. Mode ``reused``, and the builds that hold the variants asked for on the build page."""
    builds = {v: held[v].build for v in needs if held[v] is not None}
    edge.agent.meta_data_set(MODE, REUSED)
    edge.agent.annotate(reused_annotation(p, job.project, builds), "success", STEP_CONTEXT)
    edge.log(f"{job.project} is reused: every variant asked for is held")


def check_shared(job: Job, edge: Edge, p: Plan, origin: Origin, wanted: frozenset[str]) -> None:
    """8. A build of ``K`` failed on every wanted variant while this one waited behind it: fail.

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
# Steps 9 to 12
# ---------------------------------------------------------------------------


def dependency_key(p: Plan, project: str) -> str:
    """The key of a snapshot dependency of this build, as the plan fixed it."""
    node = p.nodes.get(project)
    if node is None or node.key is None:
        raise PlanError(f"the pipeline downloads {project}, which plan {p.id} holds no key of")
    return node.key


def needed(
    job: Job, generated: Generated, existing: Mapping[str, Pointer]
) -> dict[str, frozenset[str]]:
    """9. For each direct snapshot dependency, the variants the steps that step 12 keeps name.

    ``existing`` holds the pointer of each own variant of ``D`` at ``K``: a step
    that completion replaces downloads nothing, so it needs nothing.
    """
    from .document import needed_variants

    return needed_variants(generated.doc, job.project, existing)


def dependency_pointers(
    edge: Edge, p: Plan, needs: Mapping[str, frozenset[str]]
) -> dict[tuple[str, str], Pointer | None]:
    """9. The pointer of each variant of each dependency at its key; None for a missing one."""
    return {
        (project, v): found
        for project, variants in sorted(needs.items())
        for v, found in pointers_at(edge, project, dependency_key(p, project), variants).items()
    }


def missing_variants(
    held: Mapping[tuple[str, str], Pointer | None],
) -> dict[str, tuple[str, ...]]:
    """9. The variants of each dependency that have no pointer at its key, sorted."""
    missing: dict[str, list[str]] = {}
    for (project, v), found in sorted(held.items()):
        if found is None:
            missing.setdefault(project, []).append(v)
    return {project: tuple(vs) for project, vs in missing.items()}


def failed_in_chain(
    job: Job,
    edge: Edge,
    p: Plan,
    missing: Mapping[str, tuple[str, ...]],
    origin: Origin | None,
) -> None:
    """10. A dependency with a missing variant failed earlier in this chain: fail, triggering nothing.

    Its failure record of ``key(P)`` carries this plan. The own record names it
    under ``caused_by``. Within one chain a key is built at most once.
    """
    lacking = [Cause(project, dependency_key(p, project), vs) for project, vs in missing.items()]
    causes = [c for c in lacking if chain_failure(stored_records(edge, c.project, c.key), p.id)]
    if causes:
        fail_with_causes(job, edge, p, causes, origin)


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


def mark_uploaded(job: Job, edge: Edge) -> None:
    edge.agent.meta_data_set(UPLOADED + job.step_key, job.build.id)


def trigger(
    job: Job,
    edge: Edge,
    p: Plan,
    needs: Mapping[str, frozenset[str]],
    missing: Mapping[str, tuple[str, ...]],
) -> None:
    """11. One trigger step per dependency with a missing variant, and the resume step.

    Each trigger step hands down every variant this pipeline's entries name
    for its dependency. The resume step runs this step's command again.
    """
    from .document import dump_written, resume_pipeline

    if not job.command:
        raise ArtifactsError("BUILDKITE_COMMAND is not set, and the resume step runs it again")
    wanted = {project: needs[project] for project in missing}
    document = resume_pipeline(p, wanted, job.command, edge.plugin_commit(), job.pipeline or "")
    edge.agent.pipeline_upload(dump_written(document))
    mark_uploaded(job, edge)
    edge.log(f"triggered {', '.join(sorted(missing))}; the resume step goes on when they end")


def build_pipeline(
    job: Job,
    edge: Edge,
    generated: Generated,
    key: str | None,
    held: Mapping[tuple[str, str], Pointer | None],
    existing: Mapping[str, Pointer],
) -> None:
    """12. Rewrite the generated pipeline (section 8), upload it, mark the build ``built``.

    With a key, ``snapshot-promotes`` names the step of each variant that is
    built: a replaced step promotes nothing, so leave asks for no outcome of it.
    """
    from .document import Rewrite, dump_document, planned, promote_keys

    dependencies = held_only(held)
    rewrite = Rewrite(job.project, key, edge.plugin_commit(), dependencies, existing)
    out = planned(generated.doc, rewrite)
    edge.agent.pipeline_upload(dump_document(out))
    if key is not None:
        promotes = canonical_json(promote_keys(out, job.project)).decode("utf-8")
        edge.agent.meta_data_set(PROMOTES, promotes)
    mark_uploaded(job, edge)
    edge.agent.meta_data_set(MODE, BUILT)
    edge.log(f"{job.project} builds: uploaded the pipeline with {len(dependencies)} builds in it")


# ---------------------------------------------------------------------------
# Enter and resume
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Prepared:
    """What steps 1 to 6 found: the pipeline, the plan, the own key and origin, ``D`` and ``N``.

    Without a key (step 5) there is no origin, and ``D`` and ``N`` are empty.
    """

    generated: Generated
    plan: Plan
    key: str | None
    origin: Origin | None
    promoted: frozenset[str]
    needs: frozenset[str]


def first_steps(job: Job, edge: Edge) -> Prepared | None:
    """Steps 1 to 6, the same in enter and resume; None when the step uploaded before (2)."""
    check_command(job, edge)
    if uploaded_before(job, edge):
        edge.log(f"{job.step_key} uploaded in this build before; nothing more to do")
        return None
    generated = generated_pipeline(job, edge)
    p, node = plan_of(job, edge, generated)
    if node.key is None:
        # 5. Only a root can be here: its declaration holds a latest successful dependency.
        edge.log(f"{job.project} has no snapshot: it is built, and never reused")
        return Prepared(generated, p, None, None, frozenset(), frozenset())
    origin = origin_of(job, p, node.key)
    d = promoted_variants(job, edge, generated, origin)
    n = asked(job, d)
    check_declared(job, edge, n, d, origin)
    return Prepared(generated, p, node.key, origin, d, n)


def own_pointers(job: Job, edge: Edge, prepared: Prepared) -> dict[str, Pointer | None]:
    """The pointer of each variant of ``D`` at ``K``; none without a key."""
    if prepared.key is None:
        return {}
    return pointers_at(edge, job.project, prepared.key, prepared.promoted)


def enter_decisions(job: Job, edge: Edge) -> None:
    prepared = first_steps(job, edge)
    if prepared is None:
        return
    held = own_pointers(job, edge, prepared)
    if prepared.origin is not None:
        absent = frozenset(held) - frozenset(held_only(held))
        if is_reused(prepared.promoted, prepared.needs, absent):
            reuse(job, edge, prepared.plan, held, prepared.needs)
            return
        check_shared(job, edge, prepared.plan, prepared.origin, prepared.needs & absent)
    existing = held_only(held)
    needs = needed(job, prepared.generated, existing)
    dependencies = dependency_pointers(edge, prepared.plan, needs)
    missing = missing_variants(dependencies)
    if missing:
        failed_in_chain(job, edge, prepared.plan, missing, prepared.origin)
        trigger(job, edge, prepared.plan, needs, missing)
        return
    build_pipeline(job, edge, prepared.generated, prepared.key, dependencies, existing)


def resume_decisions(job: Job, edge: Edge) -> None:
    prepared = first_steps(job, edge)
    if prepared is None:
        return
    existing = held_only(own_pointers(job, edge, prepared))
    needs = needed(job, prepared.generated, existing)
    dependencies = dependency_pointers(edge, prepared.plan, needs)
    missing = missing_variants(dependencies)
    if missing:
        causes = [
            Cause(project, dependency_key(prepared.plan, project), vs)
            for project, vs in missing.items()
        ]
        fail_with_causes(job, edge, prepared.plan, causes, prepared.origin)
    build_pipeline(job, edge, prepared.generated, prepared.key, dependencies, existing)


def document_errors() -> tuple[type[BaseException], ...]:
    """The errors of enter and resume: the edge's, and those of the modules that read documents."""
    from .buildkite import BuildkiteError
    from .document import DocumentError

    return (*EDGE_ERRORS, DocumentError, BuildkiteError)


def enter(job: Job, edge: Edge) -> int:
    """The plan step: decide what this build does.

    1. The command failed: record ``generator_failed`` when a chain node; fail.
    2. The step's marker holds this build's id (a retry after the upload): done.
    3. The generated pipeline breaks the rule of section 2: record ``rule``
       when a chain node; fail.
    4. A chain node loads the plan and checks its commit; a root makes the
       plan, writes it and annotates the build with it. A plan that cannot be
       made fails the build with its message.
    5. The own project has no snapshot: go to 9 with no key.
    6. ``D`` is what this pipeline promotes, a variant promoted twice failing;
       ``N`` is ``SNAPSHOT_NEEDS`` for a chain node, else ``D``. A variant of
       ``N`` not in ``D``: record ``not_declared``; fail.
    7. ``D`` is not empty and every variant of ``N`` has a pointer at ``K``:
       mode reused; done.
    8. A build of ``K`` failed on the wanted variants while this one waited:
       record its failure as shared; fail.
    9. For each direct snapshot dependency, the variants the kept steps name,
       and which of them have no pointer at its key.
    10. A dependency with a missing variant failed earlier in this chain:
        record the cause; fail. Nothing is triggered.
    11. Some are missing: upload a trigger step per such dependency and the
        resume step; done.
    12. Else upload the rewritten pipeline; mode built; done.
    """
    return gated(job, edge, enter_decisions, document_errors(), STEP_CONTEXT, mark=True)


def resume(job: Job, edge: Edge) -> int:
    """After the triggered builds: build, or fail with the cause.

    Steps 1 to 6 of enter, with its own marker; then, for each dependency
    variant still missing, a failure record of its key that carries this plan
    is its cause, and without one the build of that dependency left no record.
    Resume never triggers. With nothing missing it goes on at step 12.
    """
    return gated(job, edge, resume_decisions, document_errors(), STEP_CONTEXT, mark=True)


# ---------------------------------------------------------------------------
# Leave
# ---------------------------------------------------------------------------


def step_promotes(edge: Edge) -> dict[str, str]:
    """``snapshot-promotes``: the step key of each variant the build makes."""
    text = meta_data(edge, PROMOTES)
    try:
        promotes = json.loads(text) if text is not None else None
    except ValueError:
        promotes = None
    if not isinstance(promotes, dict) or not all(
        is_variant(v) and isinstance(s, str) and is_step_key(s) for v, s in promotes.items()
    ):
        raise ArtifactsError(
            f"{PROMOTES} is {shown_value(text)}, not the steps of the variants a built build makes"
        )
    return promotes


def leave_plan(job: Job, edge: Edge) -> Plan:
    """The plan of this build's chain: its root's, or its own as a root."""
    if is_chain_node(job):
        return chain_plan(job, edge)
    p = own_plan(job, edge)
    if p is None:
        raise ArtifactsError(f"the mode is {BUILT}, and this root build holds no {PLAN}")
    return p


def leave_decisions(job: Job, edge: Edge) -> None:
    mode = meta_data(edge, MODE)
    if mode != BUILT:
        edge.log(f"the mode is {shown_value(mode)}: nothing to record")
        return
    p = leave_plan(job, edge)
    node = own_node(job, edge, p)
    if node.key is None:
        edge.log(f"{job.project} has no snapshot: nothing to record")
        return
    promotes = step_promotes(edge)
    outcomes = {s: edge.agent.step_outcome(s) for s in sorted(set(promotes.values()))}
    unpassed = [v for v, s in promotes.items() if outcomes[s] != PASSED]
    held = pointers_at(edge, job.project, node.key, unpassed)
    present = [v for v, found in held.items() if found is not None]
    record = leave_record(origin_of(job, p, node.key), promotes, outcomes, present, edge.clock())
    if record is None:
        edge.log(f"every variant of {job.project} at {node.key} passed or exists")
        return
    create_record(edge, record)
    edge.agent.annotate(failure_annotation(p, record, {}), "error", LEAVE_CONTEXT)


def leave(job: Job, edge: Edge) -> int:
    """After every other step of the build: record what failed.

    When the mode is built and the build has a key: the variants of
    ``snapshot-promotes`` whose step did not pass (its outcome empty or not
    ``passed``) and that have no pointer at ``K`` go into one failure record,
    written once, and an annotation. A job retried later writes its pointer
    itself; the pointer decides, never the record. Leave sets no mode: its
    command failed the step already when no plan step set one.
    """
    return gated(job, edge, leave_decisions, EDGE_ERRORS, LEAVE_CONTEXT, mark=False)
