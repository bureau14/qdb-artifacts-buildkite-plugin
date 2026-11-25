"""The plan step and its second run: what a build does, decided in order.

The plan step runs after its command put the generated pipeline in the checkout. First the
build learns what it is (``own_build``):

1. The command failed: fail, without a record.
2. The generated pipeline breaks the rule of its templates: fail.
3. A root build asks git whether its name is a branch or a tag, reads the keys of its latest
   successful downloads, makes the plan and stores it. A build started for a dependency has
   its plan from the start. The page shows the plan.
4. ``key.json`` of the build's key is created. A step id no template writes, a download that
   would take nothing, or a folder asked for that the pipeline does not promote: fail.

Then it decides:

5. The latest files of the folders made at the key name that key, when git names the build's
   commit as its ref's. The pipeline promotes something and every folder asked for is made:
   nothing is built; done.
6. Another build of the key failed on the folders wanted since the root build began: fail.
7. A dependency that lacks a folder the kept steps take failed earlier under this plan: fail.
8. Some dependency lacks a folder: upload a trigger step for each, and the step of the second
   run; done.
9. Upload the pipeline with every key written into it.

The second run, once the started builds ended, repeats 1 to 4 with the stored plan. A
dependency that still lacks a folder fails it, with the cause; else it goes on at 9. A
failure of 1, 2, 4, 6 or 7 is recorded when the build has a key. A failed step sets the outcome
``failed``, which tells the last step that nothing was built.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import NoReturn

import yaml

from ..errors import ArtifactsError
from ..job import Job, is_started_for_dependency
from ..layout import Folder
from ..promote import write_latest_file
from .annotation import (
    PLAN_STEP_ERROR,
    failure_annotation,
    left_no_record,
    refused_line,
    reused_annotation,
    shown_folders,
    which_build,
)
from .buildkite import https_repository
from .edge import (
    BUILT,
    FAILED,
    OUTCOME,
    PROMOTES,
    REUSED,
    Edge,
    made_only,
    manifests_at,
    promotes_value,
    records_of_plan,
    root_build_began,
    stored_plan,
    stored_records,
)
from .failing import Refusal, run_step
from .own_build import OwnBuild, own_build
from .pipeline import DocumentError, dependencies, promotions, taken_folders
from .plan import Plan
from .records import (
    NOT_DECLARED,
    Cause,
    Record,
    failure_in_plan,
    record,
    shared_failure,
    shared_record,
)
from .rewrite import Rewrite, needed_folders, planned, replaced
from .templates import declared_path
from .triggers import second_run_pipeline

# One annotation per context: a retried step replaces its own.
STEP_CONTEXT = "snapshot"


# ---------------------------------------------------------------------------
# What is made at the build's own key
# ---------------------------------------------------------------------------


def point_latest_files(job: Job, edge: Edge, own: OwnBuild) -> None:
    """Point the latest file of every folder made at the build's key at it, when git names
    the build's commit as its ref's: so a branch cut at built commits gets its latest files at
    the first build on it, and an older build that runs again does not move them back. A build
    started for a dependency asks git now, not the plan: its ref may have moved since the
    plan was made."""
    if not own.made or not job.writes_latest_files:
        return
    key, ref = own.origin.key, own.plan.projects[job.project].ref
    named = own.named
    if named is None:
        named = edge.git.commits_of(https_repository(job.repository), [ref])
    newest = named.get(ref)
    if newest != edge.checkout.head_commit():
        edge.log(
            f"git names {newest or 'no commit'} for {job.branch}, not this build's commit: no "
            f"latest file moves to {key}"
        )
        return
    for f in sorted(own.made):
        write_latest_file(edge.store, job.project, job.branch, f, key)
    edge.log(f"the latest files of {job.branch} name {key} for {shown_folders(sorted(own.made))}")


def reuse(job: Job, edge: Edge, own: OwnBuild) -> None:
    """Nothing is built: the page names the build that made each folder asked for."""
    builds = {f: own.made[f].promoted.build for f in own.needs}
    edge.agent.meta_data_set(OUTCOME, REUSED)
    edge.agent.annotate(reused_annotation(job.project, builds), "success", STEP_CONTEXT)
    edge.log(f"{job.project} is reused: every step asked for is made")


def check_shared(job: Job, edge: Edge, own: OwnBuild, wanted: frozenset[Folder]) -> None:
    """Another build of the key hard failed on every wanted folder since the root build
    began: fail, compiling nothing. The own record copies its entries and names that build."""
    origin = own.origin
    records = stored_records(edge, origin.project, origin.key) if wanted else []
    if not records:
        return
    source = shared_failure(records, job.build.id, root_build_began(job, edge), wanted)
    if source is None:
        return
    copied = shared_record(origin, source, wanted, edge.clock())
    message = (
        f"build {source.build.number} of the same snapshot failed on "
        f"{shown_folders(sorted(wanted))} after the root build of this plan started: "
        f"{source.build.url}"
    )
    raise Refusal(message, failure_annotation(own.plan, copied, {}), copied)


# ---------------------------------------------------------------------------
# The dependencies
# ---------------------------------------------------------------------------


def dependency_needs(job: Job, own: OwnBuild) -> dict[str, frozenset[Folder]]:
    """What the steps this build still runs take of each dependency."""
    step_ids = {project: n.steps for project, n in own.plan.projects.items()}
    return needed_folders(own.generated.steps, job.project, own.made, step_ids)


def lacking(edge: Edge, p: Plan, needs: Mapping[str, frozenset[Folder]]) -> list[Cause]:
    """Each dependency of ``needs`` with the folders that are not made at its planned key."""
    found = []
    for project, folders in sorted(needs.items()):
        key = p.projects[project].key
        manifests = manifests_at(edge, project, key, folders)
        missing = tuple(f for f in manifests if f not in made_only(manifests))
        if missing:
            found.append(Cause(project, key, missing))
    return found


def with_asking_entry(job: Job, own: OwnBuild, c: Cause, records: Mapping[str, Record]) -> Cause:
    """``c``, when the build of its dependency under this plan refused folders this build
    asked for, with the template of the entry that asked and the steps that entry names: the
    entry a person changes. A refusal of what another dependent asked for leaves ``c`` as it
    is."""
    r = records.get(c.key)
    refused = frozenset(x.folder for x in r.refused) if r and r.reason == NOT_DECLARED else ()
    wanted = frozenset(c.missing) & frozenset(refused)
    if not wanted:
        return c
    step_ids = own.plan.projects[c.project].steps
    entry = next(
        e
        for _, project, e in dependencies(own.generated.steps, job.project, latest=False)
        if project == c.project and taken_folders(e, step_ids) & wanted
    )
    template = declared_path(own.generated.declaration, c.project, latest=False)
    return replace(c, template=template, takes=entry.step or None)


def cause_message(p: Plan, c: Cause, records: Mapping[str, Record]) -> str:
    """One log line of a build that a dependency left without folders."""
    if c.template is not None:
        return refused_line(p, c, records[c.key])
    lacks = f"{which_build(p, c)} has no {shown_folders(c.missing)}"
    if c.key in records:
        return f"{lacks}: its build {records[c.key].build.number} failed under this plan"
    return f"{lacks}: {left_no_record(p, c)}"


def fail_with_causes(job: Job, edge: Edge, own: OwnBuild, causes: list[Cause]) -> NoReturn:
    """Fail a build that its dependencies left without folders, each named as a cause."""
    records = records_of_plan(edge, own.plan, causes)
    named = tuple(with_asking_entry(job, own, c, records) for c in causes)
    message = "\n".join(cause_message(own.plan, c, records) for c in named)
    written = record(own.origin, edge.clock(), caused_by=named)
    raise Refusal(message, failure_annotation(own.plan, written, records), written)


def start_builds(
    job: Job, edge: Edge, p: Plan, wanted: Mapping[str, frozenset[Folder]], path: str
) -> None:
    """Upload a trigger step per dependency of ``wanted``, each handing down every folder
    this pipeline takes of it, and the step of the second run."""
    document = second_run_pipeline(p, wanted, job.command, edge.plugin_commit(), path)
    # JSON quotes every string: the agent reads YAML 1.2, which reads as numbers some strings
    # YAML writes plain, such as a commit of digits and one "e", or a branch named 1e3.
    edge.agent.pipeline_upload(json.dumps(document, sort_keys=True))
    edge.log(f"started {', '.join(sorted(wanted))}; the second run goes on when they end")


# ---------------------------------------------------------------------------
# The upload
# ---------------------------------------------------------------------------


def upload_pipeline(job: Job, edge: Edge, own: OwnBuild) -> None:
    """Upload the generated pipeline with every key written into it. The meta-data the last
    step reads is set before the upload, so no uploaded step runs in a build without it."""
    p, steps = own.plan, own.generated.steps
    planned_own = p.projects[job.project]
    dependencies_of_own = {d: p.projects[d] for d in planned_own.deps}
    latest = {r.download: r.keys for r in planned_own.latest}
    rewrite = Rewrite(
        job.project, own.origin.key, edge.plugin_commit(), dependencies_of_own, latest, own.made
    )
    gone = replaced(steps, own.made)
    built = {f: s for f, s in promotions(steps).items() if s.position not in gone}
    keyless = sorted(f for f, s in built.items() if s.key is None)
    if keyless:
        raise DocumentError(f"the step that promotes {keyless[0]} has no key")
    document = yaml.safe_dump(planned(own.generated.doc, steps, rewrite))
    edge.agent.meta_data_set(PROMOTES, promotes_value({f: s.key for f, s in built.items()}))
    edge.agent.meta_data_set(OUTCOME, BUILT)
    edge.agent.pipeline_upload(document)
    edge.log(
        f"{job.project} builds: uploaded the pipeline with the keys of {len(planned_own.deps)} "
        f"planned projects and {len(planned_own.latest)} latest successful downloads"
    )


# ---------------------------------------------------------------------------
# The two runs
# ---------------------------------------------------------------------------


def first_run(job: Job, edge: Edge, path: str) -> None:
    stored = stored_plan(job, edge) if is_started_for_dependency(job) else None
    own = own_build(job, edge, path, stored)
    point_latest_files(job, edge, own)
    wanted = own.needs - frozenset(own.made)
    if own.promotes and not wanted:
        reuse(job, edge, own)
        return
    check_shared(job, edge, own, wanted)
    needs = dependency_needs(job, own)
    causes = lacking(edge, own.plan, needs)
    failed = [
        c for c in causes if failure_in_plan(stored_records(edge, c.project, c.key), own.plan.id)
    ]
    if failed:
        fail_with_causes(job, edge, own, failed)
    if causes:
        start_builds(job, edge, own.plan, {c.project: needs[c.project] for c in causes}, path)
        return
    upload_pipeline(job, edge, own)


def second_run(job: Job, edge: Edge, path: str) -> None:
    own = own_build(job, edge, path, stored_plan(job, edge))
    causes = lacking(edge, own.plan, dependency_needs(job, own))
    if causes:
        fail_with_causes(job, edge, own, causes)
    upload_pipeline(job, edge, own)


def run(job: Job, edge: Edge, decisions: Callable[[Job, Edge, str], None], path: str) -> int:
    try:
        return run_step(
            job, edge, lambda j, e: decisions(j, e, path), STEP_CONTEXT, PLAN_STEP_ERROR
        )
    except ArtifactsError:
        edge.agent.meta_data_set(OUTCOME, FAILED)
        raise


def plan_step(job: Job, edge: Edge, path: str) -> int:
    """The plan step of a build whose command put the generated pipeline at ``path``."""
    return run(job, edge, first_run, path)


def plan_step_again(job: Job, edge: Edge, path: str) -> int:
    """The plan step's second run, after the builds it started ended."""
    return run(job, edge, second_run, path)
