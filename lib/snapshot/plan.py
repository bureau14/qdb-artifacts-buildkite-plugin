"""The plan of a chain, as data and as the JSON document of specification section 4.

A root build makes the plan once (``planner.make_plan``) and keeps it as its
build meta-data ``snapshot-plan``; every build of the chain reads it back from
there. ``encode_plan`` writes compact JSON with sorted keys; ``decode_plan``
reads it. ``planned_node`` is the check a build of the chain makes against it;
``dependencies_first`` orders a tree for triggers.

The nodes are the snapshot tree: the root and, recursively, the snapshot
dependencies of every node. Every node but the root has a snapshot, so has a
key; the root's key is null when it has no snapshot (its own declaration
holds a latest successful dependency).

The document holds what the builds of the chain read. The root's release
line is not in it: no build reads it, and a root finds it only when it places
a dependency without a pin. The ref each node was taken from is in it::

    {"schema": 3, "id": "<root build uuid>", "context": "<root branch>",
     "rebuild": "none|one|all",
     "root": {"project": "...", "build_number": 0},
     "nodes": {"<project>": {"ref": "refs/heads/..." or "refs/tags/...",
                             "commit": "<sha>", "key": "v1-..." or null,
                             "label": "<branch>", "deps": ["<project>", ...]}}}
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .canonical import canonical_json

SCHEMA = 3

# How many hex digits of a commit a label or a message shows.
SHORT_COMMIT = 12

REBUILD_NONE = "none"
REBUILD_ONE = "one"
REBUILD_ALL = "all"


class PlanError(RuntimeError):
    """The plan cannot be made, or does not fit the build that reads it."""


@dataclass(frozen=True)
class RootInfo:
    """The root build of a chain, as the plan document names it; its uuid is the plan's id."""

    project: str
    build_number: int


@dataclass(frozen=True)
class Node:
    """A pipeline of the chain: every build of the chain uses its build at ``key``.

    ``ref`` is the full ref its commit was taken from (``refs/heads/<branch>``
    or ``refs/tags/<tag>``), ``label`` the branch name a build of it
    triggered by the chain runs under, ``deps`` its direct snapshot
    dependencies (projects of the same plan). ``key`` is None only for a root
    that has no snapshot.
    """

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
    root: RootInfo
    nodes: Mapping[str, Node]


# ---------------------------------------------------------------------------
# Writing and reading
# ---------------------------------------------------------------------------


def node_document(n: Node) -> dict[str, Any]:
    return {"ref": n.ref, "commit": n.commit, "key": n.key, "label": n.label, "deps": list(n.deps)}


def plan_document(p: Plan) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "id": p.id,
        "context": p.context,
        "rebuild": p.rebuild,
        "root": {"project": p.root.project, "build_number": p.root.build_number},
        "nodes": {name: node_document(n) for name, n in p.nodes.items()},
    }


def encode_plan(p: Plan) -> str:
    """The plan as compact JSON with sorted keys, as it is written into build meta-data."""
    return canonical_json(plan_document(p)).decode("utf-8")


def node_of(d: Mapping[str, Any]) -> Node:
    return Node(d["ref"], d["commit"], d["key"], d["label"], tuple(d["deps"]))


def decode_plan(text: str) -> Plan:
    """The plan in ``text``, as ``encode_plan`` wrote it."""
    d = json.loads(text)
    root = d["root"]
    return Plan(
        id=d["id"],
        context=d["context"],
        rebuild=d["rebuild"],
        root=RootInfo(root["project"], root["build_number"]),
        nodes={name: node_of(n) for name, n in d["nodes"].items()},
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

    def visit(q: str) -> None:
        if q in order:
            return
        for d in p.nodes[q].deps:
            visit(d)
        order.append(q)

    visit(project)
    return order


def short_commit(commit: str) -> str:
    """A commit id as a label or a message shows it: its first ``SHORT_COMMIT`` hex digits."""
    return commit[:SHORT_COMMIT]


def trigger_label(p: Plan, project: str) -> str:
    """The label of the step that triggers the build of ``project`` for the chain.

    The trigger step is written with it, and a failure of that build names it,
    so that a person finds the step on the build page.
    """
    return f":link: {project} @ {short_commit(p.nodes[project].commit)}"
