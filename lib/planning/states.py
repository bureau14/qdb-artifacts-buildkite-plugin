"""What becomes of each project of a plan, as the plan's page says it (``states``).

A project is reused when every folder asked of it, and every folder found at its key, was made
before the root build began, and built when one of them was made since. Else its build failed
under this plan, or it builds, and ``reason`` says why its key is not made.

A state is read from the store each time the page is drawn (``Held``) and is stored nowhere.
The build that draws the page knows the folders asked of itself and of the projects it takes.
Of a project further down it knows only the folders found at the key, so such a project reads
as reused while a build of its key still runs, or was cancelled, after it made some folders.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from ..forms import epoch_of
from ..job import Build
from ..layout import Folder
from ..manifest import Manifest, is_made
from .plan import Plan, is_forced
from .records import Record, failure_in_plan, most_recent

REUSED, BUILT, BUILDS, FAILED = "reused", "built", "builds", "failed"

# Why a key is not made: SNAPSHOT_REBUILD gave it to this plan alone; no key of the project
# holds its commit; a project it takes is not reused; every project it takes is reused, but its
# commit was never built with them; a build of the key failed under another plan; a build of
# the key left folders unmade and no record.
FORCED, NEW_COMMIT, NEW_BELOW, NEW_SET = "forced", "new_commit", "new_below", "new_set"
FAILED_BEFORE, UNFINISHED = "failed_before", "unfinished"

REASONS = {
    FORCED: "rebuild forced",
    NEW_COMMIT: "new commit",
    NEW_SET: "not built with these before",
    UNFINISHED: "earlier build unfinished",
}


@dataclass(frozen=True)
class Held:
    """What the store holds of a project at its planned key: the manifest of every folder asked
    of it and of every folder found there, None where no upload wrote one; its failure records;
    and whether another key of its commit is there."""

    manifests: Mapping[Folder, Manifest | None]
    records: tuple[Record, ...]
    built_before: bool


@dataclass(frozen=True)
class State:
    """``reason`` is set for ``BUILDS``. ``builds`` holds the builds that made a reused or
    built project, or the one that failed. ``below`` names the projects of ``NEW_BELOW``."""

    kind: str
    reason: str = ""
    builds: tuple[Build, ...] = ()
    below: tuple[str, ...] = ()


def makers(made: Iterable[Manifest]) -> tuple[Build, ...]:
    """The builds that promoted ``made``, each once, by number."""
    return tuple(sorted({m.promoted.build for m in made}, key=lambda b: (b.number, b.id)))


def unmade(plan: Plan, project: str, held: Held, below: Mapping[str, State]) -> State:
    """Why ``project`` builds: the first reason that holds, in the order of the constants,
    with what the store holds at the key before what the plan says of the projects below."""
    if is_forced(plan, project):
        return State(BUILDS, FORCED)
    earlier = most_recent(held.records)
    if earlier is not None:
        return State(BUILDS, FAILED_BEFORE, (earlier.build,))
    if any(m is not None for m in held.manifests.values()):
        return State(BUILDS, UNFINISHED)
    if not held.built_before:
        return State(BUILDS, NEW_COMMIT)
    new = tuple(d for d, s in below.items() if s.kind != REUSED)
    return State(BUILDS, NEW_BELOW, below=new) if new else State(BUILDS, NEW_SET)


def state_of(
    plan: Plan, project: str, held: Held, began: float, below: Mapping[str, State]
) -> State:
    """``began`` is when the root build began, in epoch seconds; ``below`` the state of each
    project that ``project`` takes."""
    made = [m for m in held.manifests.values() if is_made(m)]
    if made and len(made) == len(held.manifests):
        since = any(epoch_of(m.promoted.at) >= began for m in made)
        return State(BUILT if since else REUSED, builds=makers(made))
    failed = failure_in_plan(held.records, plan.id)
    if failed is not None:
        return State(FAILED, builds=(failed.build,))
    return unmade(plan, project, held, below)


def states(plan: Plan, project: str, held: Mapping[str, Held], began: float) -> dict[str, State]:
    """The state of every project of the tree of ``project``."""
    found: dict[str, State] = {}

    def state(p: str) -> State:
        if p not in found:
            below = {d: state(d) for d in plan.projects[p].deps}
            found[p] = state_of(plan, p, held[p], began, below)
        return found[p]

    state(project)
    return found


def stays_off_latest(plan: Plan, project: str, s: State) -> bool:
    """Whether this plan makes ``project`` and its build does not become the latest
    successful build of its branch. A reused project is not made here, so nothing of it could
    become the latest."""
    return s.kind != REUSED and not plan.projects[project].writes_latest_file


def why(s: State) -> str:
    """The reason of a project that builds, in a few words."""
    if s.reason == NEW_BELOW:
        more = len(s.below) - 1
        return f"new {s.below[0]}" + (f" and {more} more" if more else "")
    if s.reason == FAILED_BEFORE:
        return f"build {s.builds[0].number} failed"
    return REASONS[s.reason]


def said(s: State) -> str:
    """A state in one line, as a box of the picture says it."""
    if s.kind == BUILDS:
        return f"builds: {why(s)}"
    numbers = ", ".join(str(b.number) for b in s.builds)
    return f"{s.kind}, build{'' if len(s.builds) == 1 else 's'} {numbers}"
