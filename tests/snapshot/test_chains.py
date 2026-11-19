"""Chains of builds, played in process over the fakes of the planning half (``chain.py``).

The four guarantees of snapshot dependencies, and what a person relies on around them:

1. a build uses up-to-date dependencies;
2. a dependency shared by several nodes of one tree is one build for all of them;
3. a failed dependency fails its dependents;
4. the same on every platform of the matrix.

Every case is a graph of ``chain.py`` (chain, diamond, wide, the real five
projects), a node or root of it, and a platform matrix.
"""

from __future__ import annotations

import pytest
import yaml
from chain import LOWER_NODES, MATRICES, SHAPED, above, below, entry, tree
from fakes import url
from world import paired

from lib import layout
from lib.snapshot.buildkite import PipelineFacts

MASTER = "refs/heads/master"
POINTERS = "_snapshot/v1/pointers/"


def compiled_since(world, done: int) -> set[tuple[str, str]]:
    """``(project, variant)`` of every compile after the first ``done``."""
    return {(c.build.project, c.variant) for c in world.compiled[done:]}


def holders(world, root_build, projects, variants) -> dict:
    """The build that holds each variant of each project at the key the root's plan names."""
    plan = world.plan(root_build)
    return {(p, v): world.pointer(p, plan.nodes[p].key, v) for p in projects for v in variants}


def held_responsible(world, root_build) -> set[tuple[str, str]]:
    """The ``(project, variant)`` that failed, found by following the root's failure record
    down the records of its chain that it names as causes."""
    plan = world.plan(root_build)
    records = {(r.project, r.key): r for r in world.records() if r.plan == plan.id}
    pending, seen, found = [(root_build.project, plan.nodes[root_build.project].key)], set(), set()
    while pending:
        at = pending.pop()
        if at in seen or at not in records:
            continue
        seen.add(at)
        found |= {(at[0], f.variant) for f in records[at].failed}
        pending += [(c.project, c.key) for c in records[at].caused_by]
    return found


def test_a_first_build_builds_every_project_of_its_tree_once_dependencies_first(
    worlds, graph_root, matrix
):
    """A root's first build compiles every project of its tree in one build, each after its
    dependencies; every download of every node takes the build the plan names for that project
    and variant; every project and variant has its pointer."""
    graph, root = graph_root.graph, graph_root.node
    world = worlds(graph, matrix)

    top = world.build(root)

    projects = tree(graph, root)
    held = holders(world, top, projects, matrix)
    order = [c.build.project for c in world.compiled]
    first = {p: order.index(p) for p in projects}
    last = {p: len(order) - 1 - order[::-1].index(p) for p in projects}
    built = {b.project: b.id for b in top.chain()}
    downloads = {(d.build.project, d.project, d.variant): d.build_id for d in world.downloads}
    assert top.state == "passed"
    assert sorted(b.project for b in top.chain()) == sorted(projects)
    assert compiled_since(world, 0) == {(p, v) for p in projects for v in matrix}
    assert all(last[d] < first[c] for c in projects for d in graph[c])
    assert all(held[(p, v)].build.id == built[p] for p, v in held)
    assert downloads == {(c, d, v): built[d] for c in projects for d in graph[c] for v in matrix}


def test_a_second_build_with_nothing_changed_builds_nothing(worlds, graph_root, matrix):
    """A root built again without a new commit is reused: it compiles nothing, triggers nothing
    and uploads no pipeline, and the builds its plan names are the ones the first build made."""
    graph, root = graph_root.graph, graph_root.node
    world = worlds(graph, matrix)
    first = world.build(root)
    done, pointers = len(world.compiled), world.objects(POINTERS)

    again = world.build(root)

    projects = tree(graph, root)
    assert again.state == "passed"
    assert (again.children, world.agents.uploads[again.id]) == ([], [])
    assert compiled_since(world, done) == set()
    assert world.objects(POINTERS) == pointers
    assert holders(world, again, projects, matrix) == holders(world, first, projects, matrix)


def test_a_new_commit_builds_exactly_its_node_and_the_nodes_above(worlds, graph_node, matrix):
    """After a new commit of a node, the root's next build compiles that node and every node
    above it in the tree, each on every platform, and nothing else; every download of that
    chain takes a build of its project's newest commit."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    world = worlds(graph, matrix)
    world.build(root)
    done, fetched = len(world.compiled), len(world.downloads)

    world.commit(node)
    again = world.build(root)

    rebuilt = ({node} | above(graph, node)) & tree(graph, root)
    commits = {b.id: b.commit for b in world.builds}
    assert again.state == "passed"
    assert compiled_since(world, done) == {(p, v) for p in rebuilt for v in matrix}
    assert all(
        commits[d.build_id] == world.hosting.head(d.project) for d in world.downloads[fetched:]
    )


def test_two_roots_use_one_build_of_a_dependency_they_share(worlds, graph_node, matrix):
    """A second root that takes a node of a built tree as its snapshot dependency compiles
    nothing of that node's tree, and downloads, on every platform, the build the first chain
    made of it."""
    at = graph_node
    graph = {**at.graph, "other": (at.node,)}
    world = worlds(graph, matrix)
    first = world.build(at.root)
    done = len(world.compiled)

    other = world.build("other")

    (shared,) = [b for b in first.chain() if b.project == at.node]
    assert other.state == "passed"
    assert compiled_since(world, done) == {("other", v) for v in matrix}
    assert {(d.project, d.variant, d.build_id) for d in world.downloads if d.build is other} == {
        (at.node, v, shared.id) for v in matrix
    }


def test_a_failed_variant_fails_every_build_above_and_holds_its_project_responsible(
    worlds, graph_node, matrix
):
    """When a variant a consumer needs fails at a node, that node's build, every build above it
    and the root fail; no project is built twice in the chain; nothing above it is promoted;
    and the failure records of the chain lead from the root to that node and variant alone."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    world = worlds(graph, matrix)
    world.broken.add((node, matrix[-1]))

    top = world.build(root)

    failing = ({node} | above(graph, node)) & tree(graph, root)
    built = [b.project for b in top.chain()]
    promoted = {k.split("/")[3] for k in world.objects(POINTERS)}
    moved = {p for p in failing for v in matrix if world.latest(p, MASTER, v) is not None}
    assert top.state == "failed"
    assert {b.project for b in top.chain() if b.state == "failed"} == failing
    assert len(built) == len(set(built))
    assert promoted & above(graph, node) == set()
    assert moved <= {node}
    assert held_responsible(world, top) == {(node, matrix[-1])}


def first_consumer(at) -> str:
    """The first project of the node's tree that takes the node as a snapshot dependency."""
    return min(c for c in tree(at.graph, at.root) if at.node in at.graph[c])


@pytest.mark.parametrize("docs", ["passes", "fails"])
def test_a_variant_nobody_downloads_is_built_once_a_consumer_starts_to(
    worlds, lower_node, matrix, docs
):
    """A variant of a dependency that no consumer downloads may fail, and the root passes with
    every needed variant held. When one consumer then starts to download it, with no change
    outside that consumer, the next chain compiles that consumer and the nodes above it and, of
    the dependency, that variant alone when it is missing; each download of it takes the build
    that holds it."""
    graph, node, root = lower_node.graph, lower_node.node, lower_node.root
    world = worlds(graph, matrix)
    world.projects[node].variants += ("docs",)
    world.commit(node)
    if docs == "fails":
        world.broken.add((node, "docs"))
    first = world.build(root)
    world.broken.clear()
    consumer = first_consumer(lower_node)
    world.projects[consumer].extra = ((node, "docs"),)
    world.commit(consumer)
    done, fetched = len(world.compiled), len(world.downloads)

    again = world.build(root)

    held = holders(world, first, tree(graph, root), matrix)
    (docs_held,) = holders(world, again, [node], ["docs"]).values()
    rebuilt = ({consumer} | above(graph, consumer)) & tree(graph, root)
    missing = {(node, "docs")} if docs == "fails" else set()
    assert (first.state, again.state) == ("passed", "passed")
    assert all(found is not None for found in held.values())
    assert compiled_since(world, done) == {(p, v) for p in rebuilt for v in matrix} | missing
    assert {d.build_id for d in world.downloads[fetched:] if d.variant == "docs"} == {
        docs_held.build.id
    }


def test_a_snapshot_missing_a_variant_builds_only_that_variant(worlds, graph_node):
    """After a chain in which one variant of a node failed, the next chain compiles that variant
    alone of that node, and every download takes the build that holds its variant."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    matrix = MATRICES["three"]
    world = worlds(graph, matrix)
    world.broken.add((node, matrix[-1]))
    world.build(root)
    world.broken.clear()
    done, fetched = len(world.compiled), len(world.downloads)

    again = world.build(root)

    held = holders(world, again, tree(graph, root), matrix)
    assert again.state == "passed"
    assert {v for p, v in compiled_since(world, done) if p == node} == {matrix[-1]}
    assert all(
        d.build_id == held[(d.project, d.variant)].build.id for d in world.downloads[fetched:]
    )


# When the root of the chain of ``other`` starts against the hard failure of a node, and
# whether that chain compiles the node again: only when it started after the failure, or so
# shortly before it was recorded (records.SHARED_AFTER) that it counts as after.
WHEN = {
    "waits-at-the-gate": False,
    "reaches-it-later": False,
    "starts-after": True,
    "starts-after-and-an-earlier-chain-shares-it": True,
    "starts-as-it-is-recorded": True,
}


@pytest.mark.parametrize("when", sorted(WHEN))
def test_a_hard_failure_fails_the_same_snapshot_in_every_chain_whose_root_started_before_it(
    worlds, lower_node, matrix, when
):
    """When a node's build hard fails a variant, a build of the same snapshot fails without
    compiling it in every chain whose root started before the failure, whether that build
    waited at its gate meanwhile or reached the node later; a chain whose root started after
    the failure, or within five seconds before it was recorded, compiles that variant once
    again, and nothing else of the node, also when a chain that started before has shared the
    failure since."""
    graph, node, root = lower_node.graph, lower_node.node, lower_node.root
    world = worlds({**graph, "other": (node,)}, matrix)
    first = {"compiled": 0}

    def a_chain_fails_at_the_node() -> None:
        world.now += 10
        world.broken.add((node, matrix[-1]))
        world.build(root)
        world.broken.clear()
        world.now += 10
        first["compiled"] = len(world.compiled)

    def at_the_node_once(then) -> list:
        """Call ``then`` when the next build of the node waits at its gate; what it gave."""
        given: list = []

        def at_gate(build) -> None:
            if build.project == node:
                world.at_gate = lambda b: None
                given.append(then())

        world.at_gate = at_gate
        return given

    def start_other():
        return world.start("other", "master", None, {})

    if when == "waits-at-the-gate":
        other = start_other()
        at_the_node_once(a_chain_fails_at_the_node)
    elif when == "reaches-it-later":
        other = start_other()
        a_chain_fails_at_the_node()
    elif when == "starts-after":
        a_chain_fails_at_the_node()
        other = start_other()
    elif when == "starts-after-and-an-earlier-chain-shares-it":
        earlier = world.start(root, "master", None, {})
        a_chain_fails_at_the_node()
        other = start_other()
        world.now += 10
        world.play(earlier)
    else:
        # Only the node builds in the failing chain, so its record follows other's start at once.
        world.build(root)
        world.commit(node)
        started = at_the_node_once(start_other)
        a_chain_fails_at_the_node()
        (other,) = started
    world.play(other)

    again = [c.variant for c in world.compiled[first["compiled"] :] if c.build.project == node]
    assert other.state == ("passed" if WHEN[when] else "failed")
    assert again == ([matrix[-1]] if WHEN[when] else [])


def test_a_pin_places_its_project_for_the_whole_chain(worlds, lower_node, matrix):
    """A literal git ref in one consumer's template pins a node: the chain builds it once, at
    the head of that ref, and every download of it in the chain, on every platform, takes that
    build."""
    graph, node, root = lower_node.graph, lower_node.node, lower_node.root
    world = worlds(graph, matrix)
    pinned = world.commit(node, branch="3.14.x")
    consumer = first_consumer(lower_node)
    world.projects[consumer].pins[node] = "refs/heads/3.14.x"
    world.commit(consumer)

    top = world.build(root)

    (built,) = world.builds_of(node)
    assert top.state == "passed"
    assert built.commit == pinned
    assert {d.build_id for d in world.downloads if d.project == node} == {built.id}


def entries_of(document: str, project: str) -> list[dict]:
    steps = yaml.safe_load(document)["steps"]
    found = (
        e
        for s in steps
        for p in s.get("plugins", [])
        for o in p.values()
        for e in (o or {}).get("download", {}).get("projects", [])
    )
    return [e for e in found if e.get("project_id") == project]


def test_a_root_with_a_latest_successful_dependency_builds_every_time(worlds, graph_root, matrix):
    """A root that takes a project as latest successful compiles in every build, writes no
    pointer, keeps that download as generated and gets that project's promoted build; its
    snapshot dependencies are built once."""
    graph, root = graph_root.graph, graph_root.node
    world = worlds(graph, matrix)
    for v in matrix:
        world.store.put(
            layout.latest_pointer("", "tools", MASTER, v), b"tools-build-7", "text/plain"
        )
    world.projects[root].latest = ("tools",)
    world.commit(root)

    builds = [world.build(root), world.build(root)]

    generated = [entry("tools", v, MASTER, latest=True) for v in matrix]
    tools = {(d.build.id, d.variant, d.build_id) for d in world.downloads if d.project == "tools"}
    assert [b.state for b in builds] == ["passed", "passed"]
    assert {(c.build.id, c.variant) for c in world.compiled if c.build.project == root} == {
        (b.id, v) for b in builds for v in matrix
    }
    assert world.objects(POINTERS + root + "/") == {}
    assert all(entries_of(world.agents.uploads[b.id][-1], "tools") == generated for b in builds)
    assert tools == {(b.id, v, "tools-build-7") for b in builds for v in matrix}
    assert all(len(world.builds_of(p)) == 1 for p in below(graph, root))


def cannot_give_a_snapshot(world, project: str, condition: str) -> None:
    if condition == "buildkite":
        world.facts[project] = PipelineFacts(url(project), False)
    elif condition == "shape":
        world.projects[project].pipeline_py = SHAPED.replace(b"run(", b"print(")
    else:
        world.projects[project].latest = ("tools",)
    world.commit(project)


CONDITIONS = ("buildkite", "shape", "latest")


@pytest.mark.parametrize("lower_node, condition", paired(LOWER_NODES, CONDITIONS), ids=str)
def test_a_dependency_that_cannot_give_a_snapshot_stops_the_plan(worlds, lower_node, condition):
    """A snapshot dependency that cannot give a snapshot (Buildkite's configuration does not run
    its pipeline.py, that file lacks the runner's call, or it takes a project as latest
    successful) fails the root before anything is triggered: no plan, no trigger, no compile,
    nothing stored."""
    world = worlds(lower_node.graph)
    cannot_give_a_snapshot(world, lower_node.node, condition)

    top = world.build(lower_node.root)

    assert top.state == "failed"
    assert (top.children, world.compiled, world.store.objects) == ([], [], {})
    assert "snapshot-plan" not in world.agents.meta_data[top.id]


def test_snapshot_rebuild_builds_the_root_again_and_keeps_its_pointers(worlds, graph_root, matrix):
    """SNAPSHOT_REBUILD=1 compiles the root again although its snapshot exists, compiles no
    dependency, and leaves the pointers the earlier build wrote as they were."""
    graph, root = graph_root.graph, graph_root.node
    world = worlds(graph, matrix)
    world.build(root)
    done, pointers = len(world.compiled), world.objects(POINTERS)

    again = world.build(root, SNAPSHOT_REBUILD="1")

    assert again.state == "passed"
    assert compiled_since(world, done) == {(root, v) for v in matrix}
    assert pointers.items() <= world.objects(POINTERS).items()


RELEASE_LINES = ("master", "3.14.x")


def label(graph: dict, node: str, branch_of: dict, root_branch: str) -> str:
    """The rule of the label for a tree without pins in which every project has a repository of
    its own: a node runs under its own branch when its build is the one its own pipeline would
    make for that branch, that is when its branch is the root's and that is a release line or
    every node below it is on it too, or when its branch is a release line every node below it
    is on; else under ``snapshot/<root branch>``."""
    own = branch_of[node]
    level = all(branch_of[m] == own for m in below(graph, node))
    as_itself = (own == root_branch and (own in RELEASE_LINES or level)) or (
        own in RELEASE_LINES and level
    )
    return own if as_itself else f"snapshot/{root_branch}"


def latest_refs(world, project: str) -> set[str]:
    """The refs whose LATEST_SUCCESSFUL of some variant of ``project`` was moved."""
    found = (
        k
        for k in world.store.objects
        if k.startswith(project + "/") and k.endswith("/LATEST_SUCCESSFUL")
    )
    return {k[len(project) + 1 :].rsplit("/variants/", 1)[0] for k in found}


@pytest.mark.parametrize("root_branch", ["master", "feature", "3.14.x"])
def test_a_node_runs_under_its_own_branch_only_as_its_own_build(
    worlds, graph_node, matrix, root_branch
):
    """When a root builds a branch that a node and every node above it also have, each
    triggered build runs, and moves LATEST_SUCCESSFUL of every platform, under the branch the
    label rule gives: its own when it is the build its own pipeline would make, else
    snapshot/<root branch>."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    world = worlds(graph, matrix)
    branched = ({node} | above(graph, node)) & tree(graph, root)
    for p in sorted(branched):
        world.commit(p, branch=root_branch)

    top = world.build(root, branch=root_branch)

    branch_of = {p: root_branch if p in branched else "master" for p in tree(graph, root)}
    triggered = top.chain()[1:]
    assert top.state == "passed"
    assert {b.project: b.branch for b in triggered} == {
        b.project: label(graph, b.project, branch_of, root_branch) for b in triggered
    }
    assert all(latest_refs(world, b.project) == {"refs/heads/" + b.branch} for b in triggered)
