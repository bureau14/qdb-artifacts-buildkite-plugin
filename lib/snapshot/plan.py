"""The plan of a chain, as data and as the JSON document kept in the first build's meta-data.

The first build of a chain makes the plan once (``planner.make_plan``) and
keeps it as its build meta-data ``snapshot-plan``; every build of the chain
reads it back from there. ``encode_plan`` writes canonical JSON; ``decode_plan``
reads it. ``planned_project`` is the check a build of the chain makes against
it; ``dependencies_first`` orders a tree for the steps that start builds.

The projects are the snapshot tree: the first build's project and,
recursively, the snapshot dependencies of every project. Every project has a
key: that of what ``key_json_of`` reads from the plan (``keyed``), so every
build of the chain creates its ``key.json`` from the plan alone and computes
no key. The first build's downloads that take the latest successful build of
another project are read once, by its plan step, and the keys read are part
of the plan and of the first build's key, so its second run and its every job
take the same keys.

The document holds what the builds of the chain read::

    {"context": "<first build's name, a branch's or a tag's>",
     "first": {"build_number": <n>, "project": "<slug>"},
     "id": "<first build's uuid>",
     "projects": {"<project>": {"deps": ["<project>", ...], "key": "<key>",
                                "latest": [{"keys": {"<step id>": "<key>"},
                                            "project": "<slug>",
                                            "ref": "refs/heads/<branch>",
                                            "step": ["<step id>", ...] or null,
                                            "strict": true or false,
                                            "variant": "<platform>"}],
                                "ref": "refs/heads/<branch>" or "refs/tags/<tag>",
                                "steps": ["<step id>", ...],
                                "writes_latest_file": true or false}},
     "rebuild": "none" or "one" or "all"}

``steps`` are the step ids the project's templates promote at its commit, so
that no build of the chain reads a dependency's templates again. A project's
commit is its key's (``commit_of``).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from ..key import KeyJson, canonical_json, commit_of, key_of

# How many hex digits of a commit a label or a message shows.
SHORT_COMMIT = 12

REBUILD_NONE = "none"
REBUILD_ONE = "one"
REBUILD_ALL = "all"


class PlanError(RuntimeError):
    """The plan cannot be made, or does not fit the build that reads it."""


@dataclass(frozen=True)
class First:
    """The first build of a chain, as the plan names it; its uuid is the plan's id."""

    project: str
    build_number: int


@dataclass(frozen=True)
class LatestDownload:
    """A download of the first build that takes the latest successful build of another
    project: the project, the platform, the ref its walk starts at, whether it tries that
    ref alone, and the step ids it names, None for every step of the newest key."""

    project: str
    variant: str
    ref: str
    strict: bool
    step: tuple[str, ...] | None


@dataclass(frozen=True)
class Latest:
    """A latest successful download and the key of each step it takes, as the plan step read
    them through the latest files."""

    download: LatestDownload
    keys: Mapping[str, str]


@dataclass(frozen=True)
class Planned:
    """A project of the chain: every build of the chain uses its build at ``key``.

    ``ref`` is the full ref its commit was taken from (``refs/heads/<branch>``
    or ``refs/tags/<tag>``), whose name a build of it runs under;
    ``writes_latest_file`` whether that build writes its branch's latest
    files, true for the first build; ``deps`` its direct snapshot dependencies
    (projects of the same plan), ``steps`` the step ids its templates promote,
    ``latest`` its downloads that take the latest successful build, which only
    the first build has. ``key`` is None only while ``make_plan`` has not
    keyed it.
    """

    ref: str
    commit: str
    key: str | None
    writes_latest_file: bool
    deps: tuple[str, ...] = ()
    steps: tuple[str, ...] = ()
    latest: tuple[Latest, ...] = ()


@dataclass(frozen=True)
class Plan:
    """What the first build fixed for its chain; ``id`` is the first build's uuid."""

    id: str
    context: str
    rebuild: str
    first: First
    projects: Mapping[str, Planned]


@dataclass(frozen=True)
class LeftOut:
    """A download of a build's own project at another ref, which the build's key leaves out:
    the step that downloads, the platform it takes, and the ref whose latest build its job
    reads when it runs."""

    step: str
    platform: str
    ref: str


# ---------------------------------------------------------------------------
# Writing and reading
# ---------------------------------------------------------------------------


def latest_document(r: Latest) -> dict[str, Any]:
    d = r.download
    return {
        "keys": dict(r.keys),
        "project": d.project,
        "ref": d.ref,
        "step": None if d.step is None else list(d.step),
        "strict": d.strict,
        "variant": d.variant,
    }


def planned_document(n: Planned) -> dict[str, Any]:
    return {
        "deps": list(n.deps),
        "key": n.key,
        "latest": [latest_document(r) for r in n.latest],
        "ref": n.ref,
        "steps": list(n.steps),
        "writes_latest_file": n.writes_latest_file,
    }


def plan_document(p: Plan) -> dict[str, Any]:
    return {
        "context": p.context,
        "first": {"build_number": p.first.build_number, "project": p.first.project},
        "id": p.id,
        "projects": {name: planned_document(n) for name, n in p.projects.items()},
        "rebuild": p.rebuild,
    }


def encode_plan(p: Plan) -> str:
    """The plan as canonical JSON, as it is written into build meta-data."""
    return canonical_json(plan_document(p)).decode("utf-8")


def latest_of(d: Mapping[str, Any]) -> Latest:
    step = None if d["step"] is None else tuple(d["step"])
    download = LatestDownload(d["project"], d["variant"], d["ref"], d["strict"], step)
    return Latest(download, d["keys"])


def planned_of(d: Mapping[str, Any]) -> Planned:
    return Planned(
        d["ref"],
        commit_of(d["key"]),
        d["key"],
        d["writes_latest_file"],
        tuple(d["deps"]),
        tuple(d["steps"]),
        tuple(latest_of(r) for r in d["latest"]),
    )


def decode_plan(text: str) -> Plan:
    """The plan in ``text``, as ``encode_plan`` wrote it."""
    d = json.loads(text)
    first = d["first"]
    return Plan(
        id=d["id"],
        context=d["context"],
        rebuild=d["rebuild"],
        first=First(first["project"], first["build_number"]),
        projects={name: planned_of(n) for name, n in d["projects"].items()},
    )


# ---------------------------------------------------------------------------
# Using a plan
# ---------------------------------------------------------------------------


def forced_build(rebuild: str, is_first: bool, plan_id: str) -> str | None:
    """The build a key of one build's own names when ``SNAPSHOT_REBUILD`` forces a project
    (the first build's under ``one``, every project's under ``all``): the plan's id; else
    None."""
    forced = rebuild == REBUILD_ALL or (rebuild == REBUILD_ONE and is_first)
    return plan_id if forced else None


def key_json_of(p: Plan, project: str) -> KeyJson:
    """What the planned key of ``project`` stands for, as the plan fixed it: its commit, the
    key of each snapshot dependency and each key read for a latest successful download, and
    the plan's id when the project is forced."""
    n = p.projects[project]
    planned = ((d, p.projects[d].key) for d in n.deps)
    read = ((r.download.project, k) for r in n.latest for k in r.keys.values())
    forced = forced_build(p.rebuild, project == p.first.project, p.id)
    return KeyJson(project, n.commit, (*planned, *read), forced)


def planned_project(p: Plan, project: str, commit: str) -> Planned:
    """The project ``project`` of the plan, for a build of it at ``commit``.

    Raises ``PlanError`` when the plan has no such project or plans it at
    another commit.
    """
    n = p.projects.get(project)
    if n is None:
        raise PlanError(f"plan {p.id} has no project {project}")
    if n.commit != commit:
        raise PlanError(f"plan {p.id} plans {project} at {n.commit}, this build is at {commit}")
    return n


def dependencies_first(p: Plan, project: str) -> list[str]:
    """Every project of ``project``'s closure in ``p``, each after its dependencies, itself last."""
    order: list[str] = []

    def visit(q: str) -> None:
        if q in order:
            return
        for d in p.projects[q].deps:
            visit(d)
        order.append(q)

    visit(project)
    return order


def keyed(p: Plan) -> Plan:
    """``p`` with the key of every project, dependencies first: the key of what
    ``key_json_of`` says it stands for."""

    def with_key(q: Plan, project: str) -> Plan:
        key = key_of(key_json_of(q, project))
        projects = {**q.projects, project: replace(q.projects[project], key=key)}
        return replace(q, projects=projects)

    return functools.reduce(with_key, dependencies_first(p, p.first.project), p)


def short_commit(commit: str) -> str:
    """A commit id as a label or a message shows it: its first ``SHORT_COMMIT`` hex digits."""
    return commit[:SHORT_COMMIT]


def trigger_label(p: Plan, project: str) -> str:
    """The label of the step that starts the build of ``project`` for the chain.

    The trigger step is written with it, and a failure of that build names it,
    so that a person finds the step on the build page.
    """
    return f":link: {project} @ {short_commit(p.projects[project].commit)}"
