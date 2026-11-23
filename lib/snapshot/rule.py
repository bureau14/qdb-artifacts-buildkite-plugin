"""The rule that keeps a declaration true: what a generated pipeline may download.

Every entry of another project must have its project declared by a committed template, with
the same kind. A snapshot entry also needs a git ref that is the build's own or a pin declared
for that project; a latest successful entry only needs to name one. Every upload and promote
block needs a step id. The declaration may hold more than the pipeline uses.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from ..forms import is_tag
from ..options import Entry, step_id_problems
from ..resolve import no_git_ref
from .pipeline import Step, dependencies, entries_of, other_project, promotions
from .plan import Plan, short_commit
from .templates import Declaration, declared_path, declared_pins, kind

# A download at a tag's ref comes from Buildkite's tag field, and the plan step reads the
# build's name alone.
TAG_FIELD_REMEDY = (
    ". Start a build at a tag with the tag's name in the branch field and Buildkite's tag "
    "field empty."
)


@dataclass(frozen=True)
class Violation:
    step: str
    reason: str

    def __str__(self) -> str:
        return f"step {self.step}: {self.reason}"


def ref_problem(e: Entry, project: str, pins: frozenset[str], own_ref: str) -> str | None:
    if e.git_ref == own_ref or e.git_ref in pins:
        return None
    shown = "no git ref" if e.git_ref is None else repr(e.git_ref)
    declared = f" (declared: {', '.join(sorted(pins))})" if pins else ""
    remedy = TAG_FIELD_REMEDY if e.git_ref and is_tag(e.git_ref) else ""
    return (
        f"downloads {project} at {shown}, which is neither the build's own ref "
        f"{own_ref} nor a pin declared for {project}{declared}{remedy}"
    )


def entry_problem(e: Entry, declaration: Declaration, own: str, own_ref: str) -> str | None:
    """Why one download entry breaks the rule; None when it does not."""
    project = other_project(e, own)
    if project is None:
        return None
    if not any(d.project == project and d.latest == e.latest for d in declaration.entries):
        return (
            f"downloads from {project} as a {kind(e.latest)} dependency, which no committed "
            f"template declares as a {kind(e.latest)} dependency"
        )
    if not e.latest:
        return ref_problem(e, project, declared_pins(declaration, project), own_ref)
    if e.git_ref:
        return None
    template = declared_path(declaration, project, latest=True)
    return no_git_ref(project, f"its entry ({project}, {e.variant}) in {template}")


def unwritten_promotions(steps: Sequence[Step], own: str, written: Collection[str]) -> list[str]:
    """Why each folder the pipeline promotes whose step id is not in ``written``, the step ids
    the declaration of the build's commit holds, breaks the rule."""
    return [
        f"Step {s.name} promotes {f}, and no template of {own} writes step_id: {f.step}. The "
        f"builds that depend on {own} read its steps from those templates, so make the step "
        f"from a template that writes step_id: {f.step}."
        for f, s in sorted(promotions(steps).items(), key=lambda item: item[0])
        if f.step not in written
    ]


def stepless_downloads(steps: Sequence[Step], own: str, plan: Plan) -> list[str]:
    """Why each snapshot entry that names no step breaks the plan, when the templates of its
    dependency promote no step id at the planned commit: it would take nothing."""
    found = (
        f"Step {s.name} downloads {project} without naming a step, and no template of {project} "
        f"at {short_commit(plan.projects[project].commit)} writes step_id beside a promote, so "
        f"{project} has no step ids at that commit. Bring to {plan.projects[project].ref} of "
        f"{project} the commit that writes them, or remove the download if {project} promotes "
        "nothing."
        for s, project, e in dependencies(steps, own, latest=False)
        if not e.step and not plan.projects[project].steps
    )
    return list(dict.fromkeys(found))


def rule_violations(
    steps: Sequence[Step], declaration: Declaration, own: str, own_ref: str
) -> list[Violation]:
    """The parts of the generated pipeline ``steps`` that the declaration of its commit does
    not allow, each once, in order. ``own_ref`` is the build's own git ref as the generator
    writes it."""
    found = (
        Violation(s.name, problem)
        for s in steps
        for o in s.options
        for problem in (
            *step_id_problems(o),
            *(entry_problem(e, declaration, own, own_ref) for e in entries_of(o)),
        )
        if problem is not None
    )
    return list(dict.fromkeys(found))
