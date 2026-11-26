"""The plan of a root build: the commit and key of every project of its tree, fixed once.

The root build makes it (``planner.make_plan``) and keeps it as its meta-data
``snapshot-plan``; every build it starts reads it back from there, so no build computes a key
or reads a dependency's templates again. The projects are the root build's and, recursively,
the snapshot dependencies of each.
"""

from __future__ import annotations

import functools
import graphlib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from ..codec import decoded, encoded
from ..forms import short_commit
from ..key import KeyJson, key_of

REBUILD_NONE, REBUILD_ONE, REBUILD_ALL = "none", "one", "all"


class PlanError(RuntimeError):
    """The plan cannot be made, or does not fit the build that reads it."""


@dataclass(frozen=True)
class Root:
    project: str
    build_number: int


@dataclass(frozen=True)
class LatestDownload:
    """A download of the root build that takes the latest successful build of another
    project: the ref its walk starts at, whether it tries that ref alone, and the step ids it
    names, None for every step of the newest key."""

    project: str
    variant: str
    ref: str
    strict: bool
    step: tuple[str, ...] | None


@dataclass(frozen=True)
class Latest:
    """A latest successful download and the key the plan step read for each step it takes."""

    download: LatestDownload
    keys: Mapping[str, str]


@dataclass(frozen=True)
class Planned:
    """A project of the plan. ``ref`` is the full ref its commit was taken from, whose name a
    build of it runs under; ``deps`` its direct snapshot dependencies; ``steps`` the step ids
    its templates promote; ``latest`` its latest successful downloads, which only the root
    build has; ``given`` what a person gave for it in ``SNAPSHOT_REFS``, which decided its ref
    and commit. ``key`` is None only while ``make_plan`` has not keyed it."""

    ref: str
    commit: str
    key: str | None
    writes_latest_file: bool
    deps: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    latest: tuple[Latest, ...] = ()
    given: str | None = None


@dataclass(frozen=True)
class Plan:
    """``id`` is the root build's uuid, ``context`` the name it runs under, ``rebuild`` one of
    ``REBUILD_*``."""

    id: str
    context: str
    rebuild: str
    root: Root
    projects: Mapping[str, Planned]


def encode_plan(p: Plan) -> str:
    return encoded(p).decode("utf-8")


def decode_plan(text: str) -> Plan:
    return decoded(Plan, text)


def dependencies_first(deps: Mapping[str, Iterable[str]]) -> list[str]:
    """The projects of ``deps``, which names the dependencies of each, every one after its
    dependencies. A cycle is a ``PlanError``."""
    graph = {p: sorted(deps[p]) for p in sorted(deps)}
    try:
        return list(graphlib.TopologicalSorter(graph).static_order())
    except graphlib.CycleError as exc:
        raise PlanError(f"dependency cycle: {' -> '.join(reversed(exc.args[1]))}") from exc


def below(deps: Mapping[str, Iterable[str]], p: str) -> frozenset[str]:
    """Every project ``p`` reaches through ``deps``, which holds no cycle."""
    return frozenset(deps[p]).union(*(below(deps, d) for d in deps[p]))


def tree_of(p: Plan, project: str) -> list[str]:
    """The projects of the tree of ``project``, depth first, each once."""
    found: dict[str, None] = {}

    def visit(q: str) -> None:
        if q not in found:
            found[q] = None
            for d in p.projects[q].deps:
                visit(d)

    visit(project)
    return list(found)


def dependencies_of(p: Plan) -> dict[str, tuple[str, ...]]:
    return {project: n.deps for project, n in p.projects.items()}


def is_forced(p: Plan, project: str) -> bool:
    """Whether ``SNAPSHOT_REBUILD`` forces ``project``: the root build's under ``one``, every
    one under ``all``."""
    return p.rebuild == REBUILD_ALL or (p.rebuild == REBUILD_ONE and project == p.root.project)


def key_json_of(p: Plan, project: str) -> KeyJson:
    """What the key of ``project`` stands for: its commit, the key of each snapshot dependency
    and each key read for a latest successful download, and the plan's id when
    ``SNAPSHOT_REBUILD`` forces the project."""
    n = p.projects[project]
    planned = ((d, p.projects[d].key) for d in n.deps)
    read = ((r.download.project, k) for r in n.latest for k in r.keys.values())
    return KeyJson(project, n.commit, (*planned, *read), p.id if is_forced(p, project) else None)


def keyed(p: Plan) -> Plan:
    """``p`` with the key of every project, dependencies first."""

    def with_key(q: Plan, project: str) -> Plan:
        n = replace(q.projects[project], key=key_of(key_json_of(q, project)))
        return replace(q, projects={**q.projects, project: n})

    return functools.reduce(with_key, dependencies_first(dependencies_of(p)), p)


def planned_project(p: Plan, project: str, commit: str) -> Planned:
    """``project`` of the plan, for a build of it at ``commit``; ``PlanError`` when the plan
    has no such project or plans it at another commit."""
    n = p.projects.get(project)
    if n is None:
        raise PlanError(f"plan {p.id} has no project {project}")
    if n.commit != commit:
        raise PlanError(f"plan {p.id} plans {project} at {n.commit}, this build is at {commit}")
    return n


def trigger_label(p: Plan, project: str) -> str:
    """The label of the step that starts the build of ``project``, which a failure of that
    build names so that a person finds the step."""
    return f":link: {project} @ {short_commit(p.projects[project].commit)}"
