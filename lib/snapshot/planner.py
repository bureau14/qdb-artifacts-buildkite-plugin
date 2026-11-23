"""The plan of a root build (``make_plan``): the place of every project of its tree, who of
them writes its branch's latest files, and their keys.

The tree is the root build's project and, recursively, the snapshot dependencies of every
project in it. The root build is taken at its own commit. A dependency is taken from the ref
a template of the tree pins it to, else from the first of ``default_refs`` its repository has.
Pins come from every project of the tree, and decide which commits are read and so which pins
there are: the tree is walked again with the pins it found until they are the pins it was
walked with (``settled``).

Where one project is taken from, and whether it can be part of a snapshot there, is
``places``.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from ..forms import branch_of, branch_ref, tag_ref
from .buildkite import PipelineFacts
from .lines import is_release_line
from .places import (
    Ask,
    Dependent,
    Member,
    Pin,
    Place,
    Refused,
    asking,
    default_refs,
    first_ref,
    resolve,
)
from .plan import (
    REBUILD_ALL,
    REBUILD_NONE,
    REBUILD_ONE,
    Latest,
    Plan,
    PlanError,
    Planned,
    Root,
    below,
    dependencies_first,
    keyed,
)
from .templates import Declaration, snapshot_dependencies


@dataclass(frozen=True)
class RootBuild:
    """The build that plans. ``name`` is the name it runs under, ``refs`` git's answer for
    ``name_refs`` of it in its repository, ``commit`` the commit checked out, ``rebuild`` what
    ``rebuild_setting`` gives."""

    project: str
    name: str
    commit: str
    refs: Mapping[str, str]
    build_id: str
    build_number: int
    rebuild: str = REBUILD_NONE


def name_refs(name: str) -> tuple[str, str]:
    """The refs a root build's name may be, in the order they count."""
    return branch_ref(name), tag_ref(name)


def named_ref(name: str, refs: Mapping[str, str]) -> str:
    """The first of ``name_refs`` that is in ``refs``; a name that is neither is planned as
    the branch it names."""
    return next((r for r in name_refs(name) if r in refs), branch_ref(name))


def rebuild_setting(value: str | None) -> str:
    """``SNAPSHOT_REBUILD`` as a setting: unset none, ``1`` one, ``all`` all; else an error."""
    settings = {None: REBUILD_NONE, "1": REBUILD_ONE, "all": REBUILD_ALL}
    if value not in settings:
        raise PlanError(f"SNAPSHOT_REBUILD={value!r}: expected 1 or all")
    return settings[value]


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def walk(
    root: str, member: Member, pins: Mapping[str, Pin], ask: Ask
) -> dict[str, Member | Refused]:
    """Every project of the tree of ``root``, which ``member`` is, breadth first, each
    resolved once, for the first project that reaches it. A refused one is not descended."""
    found: dict[str, Member | Refused] = {root: member}
    queue = [root]
    while queue:
        by = queue.pop(0)
        for project, dep in snapshot_dependencies(found[by].declaration).items():
            if project in found:
                continue
            dependent = Dependent(by, dep.path)
            found[project] = resolve(project, dependent, pins.get(project), member.place.ref, ask)
            if isinstance(found[project], Member):
                queue.append(project)
    return found


def pins_of(found: Mapping[str, Member | Refused]) -> dict[str, Pin]:
    """The pin of each project that a member of ``found`` pins. Two different pins of one
    project fail the plan; one ref written in two places is one pin."""
    pins: dict[str, list[Pin]] = {}
    for by, m in sorted(found.items()):
        for e in m.declaration.entries if isinstance(m, Member) else ():
            if e.pin is not None:
                pins.setdefault(e.project, []).append(
                    Pin(e.project, e.pin, by, m.place.commit, e.path)
                )
    for project, written in sorted(pins.items()):
        if len({p.ref for p in written}) > 1:
            shown = "; ".join(map(str, written))
            raise PlanError(f"{project} has different pins in this plan: {shown}")
    return {project: written[0] for project, written in pins.items()}


def settled(root: str, member: Member, ask: Ask) -> dict[str, Member]:
    """The tree of ``root``, walked with the pins it holds. Pins that never settle, and a
    project refused in the settled tree, fail the plan."""

    def refs(pins: Mapping[str, Pin]) -> dict[str, str]:
        return {project: pin.ref for project, pin in sorted(pins.items())}

    pins = pins_of({root: member})
    tried: list[dict[str, str]] = []
    while True:
        found = walk(root, member, pins, ask)
        held = pins_of(found)
        if refs(held) == refs(pins):
            break
        tried.append(refs(pins))
        if refs(held) in tried:
            shown = " -> ".join(str(r) for r in (*tried, refs(held)))
            raise PlanError(f"the pins of this plan never settle, each set moves another: {shown}")
        pins = held
    refused = [str(m) for _, m in sorted(found.items()) if isinstance(m, Refused)]
    if refused:
        raise PlanError("\n".join(refused))
    return found


# ---------------------------------------------------------------------------
# Who writes its branch's latest files
# ---------------------------------------------------------------------------


def own_line(at: Place, root: Place, ask: Ask) -> str | None:
    """The release line of the build a person would start of the project at ``at``, as far as
    this plan knows it: its branch when that is a line; on the root build's branch, the root build's line, in another repository only when it has a branch of that name; else None, and
    only its own branch counts."""
    branch = branch_of(at.ref)
    if branch is None or is_release_line(branch):
        return branch
    if at.ref != root.ref:
        return None
    line = ask.line()
    known = at.repository == root.repository or ask.commit(at.repository, branch_ref(line))
    return line if known else None


def writes_latest_file(
    p: str, tree: Mapping[str, Member], under: frozenset[str], root: Place, ask: Ask
) -> bool:
    """Whether a build of ``p`` started for a dependency writes its branch's latest files:
    when the build a person would start on its branch at its commit takes every project
    ``under`` it at the commit this plan takes it at. That build knows the pins of ``p`` and of
    the projects under it, and takes the rest from ``default_refs`` of its own ref."""
    at = tree[p].place
    pins = pins_of({q: tree[q] for q in (p, *under)})

    def own_commit(q: str) -> str | None:
        repository = tree[q].place.repository
        if q in pins:
            return ask.commit(repository, pins[q].ref)
        found = first_ref(ask, repository, default_refs(at.ref, lambda: own_line(at, root, ask)))
        return None if found is None else found[1]

    return all(own_commit(q) == tree[q].place.commit for q in sorted(under))


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def make_plan(
    root: RootBuild,
    own: Declaration,
    latest: tuple[Latest, ...],
    git,
    pipelines: Callable[[str], PipelineFacts],
) -> Plan:
    """The plan of ``root``. ``own`` is the declaration of its own commit, ``latest`` its
    latest successful downloads with the keys read for them. Raises ``PlanError`` when the
    tree cannot be planned; errors of git, of Buildkite and of templates pass through."""
    pipeline = functools.cache(pipelines)
    at = Place(pipeline(root.project).repository, named_ref(root.name, root.refs), root.commit)
    named = {ref: root.refs.get(ref) for ref in name_refs(root.name)}
    ask = asking(git, pipeline, at, root.name, named)
    tree = settled(root.project, Member(at, own), ask)
    deps = {p: tuple(sorted(snapshot_dependencies(m.declaration))) for p, m in tree.items()}
    # Before ``below``: a cycle fails the plan here.
    order = dependencies_first(deps)

    def planned(p: str) -> Planned:
        is_root = p == root.project
        place = tree[p].place
        return Planned(
            ref=place.ref,
            commit=place.commit,
            key=None,
            writes_latest_file=is_root or writes_latest_file(p, tree, below(deps, p), at, ask),
            deps=deps[p],
            steps=tree[p].declaration.steps,
            latest=latest if is_root else (),
        )

    return keyed(
        Plan(
            id=root.build_id,
            context=root.name,
            rebuild=root.rebuild,
            root=Root(root.project, root.build_number),
            projects={p: planned(p) for p in order},
        )
    )
