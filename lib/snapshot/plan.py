"""The plan of a chain, as data and as the JSON document of specification section 4.

A root build makes the plan once (``planner.make_plan``) and keeps it as its
build meta-data ``snapshot-plan``; every build of the chain reads it back from
there. ``encode_plan`` writes compact JSON with sorted keys; ``decode_plan``
reads it and refuses a document that is not a plan. ``planned_node`` is the check a build of the
chain makes against it; ``dependencies_first`` orders a tree for triggers.

The nodes are the snapshot tree: the root and, recursively, the snapshot
dependencies of every node. Every node but the root has a snapshot, so has a
key; the root's key is null when it has no snapshot (its own declaration
holds a latest successful dependency).

The document holds what the builds of the chain read. The root's release
line is not in it: no build reads it, and a root finds it only when it places
a dependency without a pin. The ref each node was taken from is in it::

    {"schema": 2, "id": "<root build uuid>", "context": "<root branch>",
     "rebuild": "none|one|all", "created_at": "<UTC ISO 8601>",
     "root": {"project": "...", "build_number": 0, "build_url": "..."},
     "nodes": {"<project>": {"repository": "https://...",
                             "ref": "refs/heads/..." or "refs/tags/...",
                             "commit": "<sha>", "key": "v1-..." or null,
                             "label": "<branch>", "deps": ["<project>", ...]}}}
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from ..forms import (
    is_build_id,
    is_full_ref,
    is_https_url,
    is_key,
    is_object_id,
    is_ref_name,
    is_slug,
)
from .canonical import canonical_json
from .members import member
from .shown import short_commit, shown_text, shown_value

SCHEMA = 2

REBUILD_NONE = "none"
REBUILD_ONE = "one"
REBUILD_ALL = "all"
REBUILD_SETTINGS = (REBUILD_NONE, REBUILD_ONE, REBUILD_ALL)


class PlanError(RuntimeError):
    """The plan cannot be made, written or read, or does not fit the build that reads it."""


@dataclass(frozen=True)
class RootInfo:
    """The root build of a chain, as the plan document names it; its uuid is the plan's id."""

    project: str
    build_number: int
    build_url: str


@dataclass(frozen=True)
class Node:
    """A pipeline of the chain: every build of the chain uses its build at ``key``.

    ``ref`` is the full ref its commit was taken from (``refs/heads/<branch>``
    or ``refs/tags/<tag>``), ``label`` the branch name a build of it
    triggered by the chain runs under, ``deps`` its direct snapshot
    dependencies (projects of the same plan). ``key`` is None only for a root
    that has no snapshot.
    """

    repository: str
    ref: str
    commit: str
    key: str | None
    label: str
    deps: tuple[str, ...] = ()


@dataclass(frozen=True)
class Plan:
    """What a root build fixed for its chain; ``id`` is the root build's uuid."""

    id: str
    context: str
    rebuild: str
    created_at: str
    root: RootInfo
    nodes: Mapping[str, Node]


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def node_document(n: Node) -> dict[str, Any]:
    return {
        "repository": n.repository,
        "ref": n.ref,
        "commit": n.commit,
        "key": n.key,
        "label": n.label,
        "deps": list(n.deps),
    }


def plan_document(p: Plan) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "id": p.id,
        "context": p.context,
        "rebuild": p.rebuild,
        "created_at": p.created_at,
        "root": {
            "project": p.root.project,
            "build_number": p.root.build_number,
            "build_url": p.root.build_url,
        },
        "nodes": {name: node_document(n) for name, n in p.nodes.items()},
    }


def encode_plan(p: Plan) -> str:
    """The plan as compact JSON with sorted keys, as it is written into build meta-data."""
    return canonical_json(plan_document(p)).decode("utf-8")


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _member(d: Any, name: str, kind: type | tuple[type, ...], where: str) -> Any:
    """``d[name]``, which must be of ``kind``; ``PlanError`` naming ``where`` otherwise."""
    return member(d, name, kind, where, PlanError)


# What each text of a plan must be. The plan is read from meta-data, which any
# job of the root build can write, and its texts go into the trigger steps a
# chain node uploads, where the agent interpolates "$".
_FORMS: dict[str, tuple[Callable[[str], bool], str]] = {
    "id": (is_build_id, "a build id"),
    "context": (is_ref_name, "a branch name"),
    "project": (is_slug, "a pipeline slug"),
    "build_url": (is_https_url, "an https URL"),
    "repository": (is_https_url, "an https URL"),
    "ref": (is_full_ref, "a full ref"),
    "commit": (is_object_id, "a commit id"),
    "key": (is_key, "a key"),
    "label": (is_ref_name, "a branch name"),
}


def _text(d: Any, name: str, where: str) -> str:
    """``d[name]``: a string of the form ``_FORMS`` gives for ``name``."""
    v = _member(d, name, str, where)
    ok, form = _FORMS[name]
    if not ok(v):
        raise PlanError(f"{where}.{name}: {shown_value(v)} is not {form}")
    return v


def _project(name: Any, where: str) -> str:
    if not isinstance(name, str) or not is_slug(name):
        raise PlanError(f"{where}: {shown_value(name)} is not a pipeline slug")
    return name


def _node(d: Any, where: str) -> Node:
    deps = _member(d, "deps", list, where)
    key = _member(d, "key", (str, type(None)), where)
    return Node(
        repository=_text(d, "repository", where),
        ref=_text(d, "ref", where),
        commit=_text(d, "commit", where),
        key=None if key is None else _text(d, "key", where),
        label=_text(d, "label", where),
        deps=tuple(_project(p, f"{where}.deps") for p in deps),
    )


def _check_graph(p: Plan) -> Plan:
    """The root is a node, only the root may lack a key, every dependency is a node."""
    if p.root.project not in p.nodes:
        raise PlanError(f"plan {p.id}: the root {p.root.project} is not a node")
    for name, n in p.nodes.items():
        if n.key is None and name != p.root.project:
            raise PlanError(f"plan {p.id}: {name} has no key, and only the root may lack one")
        missing = [d for d in n.deps if d not in p.nodes]
        if missing:
            raise PlanError(f"plan {p.id}: {name} depends on {', '.join(missing)}, not in the plan")
    return p


def decode_plan(text: str) -> Plan:
    """The plan in ``text``, as ``encode_plan`` wrote it; ``PlanError`` when it is not one.

    Every text of the plan must have its form (``_FORMS``): a node is a
    pipeline slug, a commit 40 hex digits, a ref a full ref, a label a branch
    name, and so on.
    """
    try:
        d = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise PlanError(f"plan: not JSON: {shown_text(str(exc))}") from exc
    if not isinstance(d, dict) or type(d.get("schema")) is not int or d["schema"] != SCHEMA:
        shown = shown_value(d.get("schema") if isinstance(d, dict) else type(d).__name__)
        raise PlanError(f"plan: not a plan of schema {SCHEMA} ({shown})")
    rebuild = _member(d, "rebuild", str, "plan")
    if rebuild not in REBUILD_SETTINGS:
        shown = shown_value(rebuild)
        raise PlanError(f"plan.rebuild: {shown} is none of {', '.join(REBUILD_SETTINGS)}")
    root = _member(d, "root", dict, "plan")
    nodes = _member(d, "nodes", dict, "plan")
    named = {_project(name, "plan.nodes"): n for name, n in nodes.items()}
    return _check_graph(
        Plan(
            id=_text(d, "id", "plan"),
            context=_text(d, "context", "plan"),
            rebuild=rebuild,
            created_at=_member(d, "created_at", str, "plan"),
            root=RootInfo(
                project=_text(root, "project", "plan.root"),
                build_number=_member(root, "build_number", int, "plan.root"),
                build_url=_text(root, "build_url", "plan.root"),
            ),
            nodes={name: _node(n, f"plan.nodes.{name}") for name, n in named.items()},
        )
    )


# ---------------------------------------------------------------------------
# Using a plan
# ---------------------------------------------------------------------------


def planned_node(p: Plan, project: str, commit: str) -> Node:
    """The node of ``project`` for a build of it at ``commit`` that the chain triggered.

    Raises ``PlanError`` when the plan has no such node or plans it at another
    commit.
    """
    n = p.nodes.get(project)
    if n is None:
        raise PlanError(f"plan {p.id} has no node {project}")
    if n.commit != commit:
        raise PlanError(f"plan {p.id} plans {project} at {n.commit}, this build is at {commit}")
    return n


def dependencies_first(p: Plan, project: str) -> list[str]:
    """Every project of ``project``'s closure in ``p``, each after its dependencies, itself last."""
    order: list[str] = []

    def visit(q: str, stack: Sequence[str]) -> None:
        if q in order:
            return
        if q in stack:
            raise PlanError(f"plan {p.id}: dependency cycle through {q}")
        for d in p.nodes[q].deps:
            visit(d, (*stack, q))
        order.append(q)

    visit(project, ())
    return order


def trigger_label(p: Plan, project: str) -> str:
    """The label of the step that triggers the build of ``project`` for the chain.

    The trigger step is written with it, and a failure of that build names it,
    so that a person finds the step on the build page.
    """
    return f":link: {project} @ {short_commit(p.nodes[project].commit)}"
