"""What a build learns of itself before the plan step decides (``own_build``).

The generated pipeline, which its command put in the checkout, held to the rule of its
templates; its plan, which a root build makes; its key; what its pipeline promotes, what it
was asked for, and what of that is made. Each fails the build when it cannot be had, with a
failure record once the build has a key.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..forms import branch_ref, is_tag
from ..job import Job, is_started_for_dependency
from ..layout import Folder
from ..manifest import Manifest
from .annotation import PLAN_REFUSED, PLAN_STEP_ERROR, plan_annotation, shown_folders
from .buildkite import https_repository
from .edge import (
    PLAN,
    Edge,
    create_own_key_json,
    held_of_tree,
    made_only,
    manifests_at,
    root_build_began,
)
from .failing import Refusal, refusal
from .latest import latest_download, read_latest
from .picture import PICTURE, tree_picture
from .pipeline import DocumentError, Step, dependencies, left_out, promotions, steps_of
from .plan import Plan, PlanError, encode_plan
from .planner import RootBuild, given_refs, make_plan, name_refs, named_ref, rebuild_setting
from .records import NOT_DECLARED, RULE, Origin, RefusedFolder
from .rewrite import needed_folders
from .rule import rule_violations, stepless_downloads, unwritten_promotions
from .states import states
from .templates import Declaration, TemplateError, is_template_path, read_declaration
from .triggers import read_needs

# The plan has its own annotation, which a retried step replaces.
PLAN_CONTEXT = "snapshot-plan"


@dataclass(frozen=True)
class Generated:
    """The generated pipeline, its steps, and the declaration of the build's own commit."""

    doc: Any
    steps: tuple[Step, ...]
    declaration: Declaration


@dataclass(frozen=True)
class OwnBuild:
    """``origin`` holds the build's key. ``promotes`` is what its pipeline promotes, ``needs``
    what it was asked for: ``SNAPSHOT_NEEDS`` in a build started for a dependency, else all it
    promotes. ``made`` is the manifest of each folder it promotes that is made at its key.
    ``takes`` is what the steps it still runs take of each dependency. ``named`` is git's
    answer for the refs a root build's name may be, None for a build that had its plan from
    the start."""

    generated: Generated
    plan: Plan
    origin: Origin
    promotes: frozenset[Folder]
    needs: frozenset[Folder]
    made: Mapping[Folder, Manifest]
    takes: Mapping[str, frozenset[Folder]]
    named: Mapping[str, str] | None


def check_command(job: Job, edge: Edge, path: str) -> None:
    """Fail the step when its command failed, without a record: a pipeline that could not be
    fetched says nothing of the build of a key."""
    if job.exit_status != 0:
        message = (
            f"the command exited {job.exit_status}: it puts the generated pipeline at {path}, "
            "which this step plans"
        )
        raise refusal(job, edge, PLAN_STEP_ERROR, message)


def generated_pipeline(job: Job, edge: Edge, path: str, origin: Origin | None) -> Generated:
    """The pipeline at ``path``, which must keep the rule of the templates of its commit."""
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    files = edge.checkout.checkout_files(".buildkite", is_template_path)
    try:
        declaration = read_declaration(files, job.project)
        steps = steps_of(doc)
    except (TemplateError, DocumentError) as exc:
        raise refusal(job, edge, RULE, str(exc), origin) from exc
    # The build's own ref, as the generator writes it from BUILDKITE_BRANCH.
    violations = rule_violations(steps, declaration, job.project, branch_ref(job.branch))
    if violations:
        raise refusal(job, edge, RULE, "\n".join(map(str, violations)), origin)
    return Generated(doc, tuple(steps), declaration)


def shown_name(name: str, repository: str, refs: Mapping[str, str]) -> str:
    """What git said of the refs a root build's name may be, as the plan step logs it."""
    ref = named_ref(name, refs)
    if ref not in refs:
        return (
            f"{name} is neither a branch nor a tag of {repository}: it is planned as a branch, "
            "and writes no latest file of a step it finds made"
        )
    if is_tag(ref):
        return (
            f"{name} is a tag of {repository}, of commit {refs[ref]}: every dependency without a "
            "pin is taken from that tag"
        )
    return f"{name} is a branch of {repository}, at {refs[ref]}"


def named_refs(job: Job, edge: Edge) -> Mapping[str, str]:
    """Git's answer for the refs a root build's name may be, which it asks once."""
    repository = https_repository(job.repository)
    refs = edge.git.commits_of(repository, name_refs(job.branch))
    edge.log(shown_name(job.branch, repository, refs))
    return refs


def made_plan(job: Job, edge: Edge, generated: Generated, named: Mapping[str, str]) -> Plan:
    """The plan a root build makes, stored as its meta-data. A latest successful download
    that finds no latest file, and a plan that cannot be made, fail the build."""
    try:
        root = RootBuild(
            project=job.project,
            name=job.branch,
            commit=edge.checkout.head_commit(),
            refs=named,
            build_id=job.build.id,
            build_number=job.build.number,
            rebuild=rebuild_setting(job.rebuild),
            given=given_refs(job.refs),
        )
        downloads = [
            latest_download(e, project)
            for _, project, e in dependencies(generated.steps, job.project, latest=True)
        ]
        at_tag = is_tag(named_ref(job.branch, named))
        latest = read_latest(edge.store, downloads, generated.declaration, at_tag)
        p = make_plan(root, generated.declaration, latest, edge.git, edge.pipelines)
    except PlanError as exc:
        raise refusal(job, edge, PLAN_REFUSED, str(exc)) from exc
    edge.agent.meta_data_set(PLAN, encode_plan(p))
    return p


def promoted_and_needed(
    job: Job, edge: Edge, generated: Generated, p: Plan, origin: Origin
) -> tuple[frozenset[Folder], frozenset[Folder]]:
    """What the pipeline promotes, and what the build was asked for. A step id no template
    writes, a download that would take nothing, and a folder asked for that the pipeline does
    not promote fail the build."""
    try:
        promotes = frozenset(promotions(generated.steps))
    except DocumentError as exc:
        raise refusal(job, edge, RULE, str(exc), origin) from exc
    broken = [
        *unwritten_promotions(generated.steps, job.project, generated.declaration.steps),
        *stepless_downloads(generated.steps, job.project, p),
    ]
    if broken:
        raise refusal(job, edge, RULE, "\n".join(broken), origin)
    needs = read_needs(job.needs) if is_started_for_dependency(job) else promotes
    outside = tuple(
        RefusedFolder(f, tuple(sorted(g.step for g in promotes if g.platform == f.platform)))
        for f in sorted(needs - promotes)
    )
    if outside:
        message = (
            f"asked for {shown_folders(x.folder for x in outside)}, which this pipeline does "
            f"not promote; it promotes {shown_folders(sorted(promotes)) or 'nothing'}"
        )
        raise refusal(job, edge, NOT_DECLARED, message, origin, refused=outside)
    return promotes, needs


def show_plan(
    job: Job,
    edge: Edge,
    p: Plan,
    outside: tuple[tuple[str, str, str], ...],
    asked: Mapping[str, frozenset[Folder]],
) -> None:
    """The plan on the build's page, each project in the state the store gives it now.
    ``asked`` holds the folders this build is asked for and those it takes of each dependency,
    as far as it knows them. The picture of the tree is an artifact of the build, which the
    page shows; it is uploaded first, so the page never names a picture that is not there."""
    held = held_of_tree(edge, p, job.project, asked)
    found = states(p, job.project, held, root_build_began(job, edge))
    picture = tree_picture(p, job.project, found)
    if picture is not None:
        edge.agent.artifact_upload(PICTURE, picture)
    edge.agent.annotate(plan_annotation(p, job.project, outside, found), "info", PLAN_CONTEXT)


def own_build(job: Job, edge: Edge, path: str, stored: Plan | None) -> OwnBuild:
    """What the build whose command put the generated pipeline at ``path`` knows of itself.
    ``stored`` is its plan when it had one from the start, as a build started for a dependency
    and every second run have: such a build has its key before its pipeline is judged, so a
    broken rule is recorded too. The page shows the plan, also of a build that is asked for
    what it cannot give, which knows of no folder that is asked for."""
    check_command(job, edge, path)
    origin = None if stored is None else create_own_key_json(job, edge, stored)
    generated = generated_pipeline(job, edge, path, origin)
    named = named_refs(job, edge) if stored is None else None
    p = made_plan(job, edge, generated, named) if stored is None else stored
    origin = origin or create_own_key_json(job, edge, p)
    outside = left_out(generated.steps, job.project, branch_ref(job.branch))
    try:
        promotes, needs = promoted_and_needed(job, edge, generated, p, origin)
    except Refusal:
        show_plan(job, edge, p, outside, {})
        raise
    made = made_only(manifests_at(edge, job.project, origin.key, promotes))
    step_ids = {project: n.steps for project, n in p.projects.items()}
    takes = needed_folders(generated.steps, job.project, made, step_ids)
    show_plan(job, edge, p, outside, {job.project: needs, **takes})
    return OwnBuild(generated, p, origin, promotes, needs, made, takes, named)
