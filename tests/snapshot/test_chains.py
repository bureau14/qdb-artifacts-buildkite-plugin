"""Chains of builds, played in process over the fakes of the planning half (``chain.py``).

The four guarantees of snapshot dependencies, and what a person relies on around them:

1. a build uses up-to-date dependencies;
2. a dependency shared by several nodes of one tree is one build for all of them;
3. a failed dependency fails its dependents;
4. the same on every platform of the matrix, and for every step id of a platform.

Every case is a graph of ``chain.py`` (chain, diamond, wide, the real five
projects), a node or root of it, a platform matrix, and the step ids of a
platform.
"""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml
from chain import LOWER_NODES, MATRICES, SHAPED, STEP_IDS, above, below, folders, tree
from fakes import url
from world import paired

from lib import layout
from lib.forms import branch_ref, name_of, tag_ref
from lib.layout import Folder
from lib.manifest import Build, Promoted, Uploaded, captured, promoted_by, write_manifest
from lib.snapshot.buildkite import PipelineFacts
from lib.snapshot.records import RULE

MASTER = "refs/heads/master"
TAG = "v1.0.0"
# A name no repository has, as a Rebuild of a build of a deleted branch runs under.
NEITHER = "gone"


def compiled_since(world, done: int) -> set[tuple[str, Folder]]:
    """``(project, folder)`` of every compile after the first ``done``."""
    return {(c.build.project, c.folder) for c in world.compiled[done:]}


def makers(world, root_build, projects, wanted) -> dict:
    """The manifest of each folder of each project made at the key the root's plan names;
    None where it is not made."""
    plan = world.plan(root_build)
    return {(p, f): world.made(p, plan.projects[p].key, f) for p in projects for f in wanted}


def made_projects(world) -> set[str]:
    """The projects with a folder whose manifest says promoted, at any key."""
    found = (k for k in world.store.objects if k.endswith("/" + layout.MANIFEST))
    return {
        k.split("/")[0] for k in found if json.loads(world.store.objects[k])["promoted"] is not None
    }


def held_responsible(world, root_build) -> set[tuple[str, object]]:
    """The ``(project, folder)`` that failed, and the ``(project, reason)`` of a build that
    failed before it built, found by following the root's failure record down the records of
    its chain that it names as causes."""
    plan = world.plan(root_build)
    records = {(r.project, r.key): r for r in world.records() if r.plan == plan.id}
    start = (root_build.project, plan.projects[root_build.project].key)
    pending, seen, found = [start], set(), set()
    while pending:
        at = pending.pop()
        if at in seen or at not in records:
            continue
        seen.add(at)
        r = records[at]
        found |= {(at[0], f.folder) for f in r.failed}
        found |= {(at[0], r.reason)} if r.reason else set()
        pending += [(c.project, c.key) for c in r.caused_by]
    return found


def taken_at(world, graph: dict, named: str) -> tuple[str, dict[str, str]]:
    """The name a root of ``graph`` builds under, and the commit its plan takes each project at:
    the branch master, at its head; a tag every project carries, on a commit of its own; a name
    no repository has, at master's head."""
    if named == "tag":
        return TAG, {p: world.tag(p, TAG) for p in sorted(graph)}
    heads = {p: world.hosting.head(p) for p in graph}
    return ("master" if named == "branch" else NEITHER), heads


@pytest.mark.parametrize("named", ["branch", "tag", "neither"])
def test_a_first_build_builds_every_project_of_its_tree_once_dependencies_first(
    worlds, graph_root, shape, named
):
    """A root's first build, under a branch, a tag or a name that is neither, compiles every
    step of every project of its tree in one build, each after its dependencies, at the commit
    the root planned, although every dependency gets a new commit while the chain runs; every
    download of every node takes every step of the dependency at the key the plan names, made
    by the build of the chain; every folder of every project is made."""
    graph, root = graph_root.graph, graph_root.node
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    name, heads = taken_at(world, graph, named)
    world.at_gate = lambda build: world.commit(build.project)

    top = world.build(root, name, commit=heads[root])

    projects, every = tree(graph, root), folders(matrix, step_ids)
    made = makers(world, top, projects, every)
    plan = world.plan(top)
    order = [c.build.project for c in world.compiled]
    first = {p: order.index(p) for p in projects}
    last = {p: len(order) - 1 - order[::-1].index(p) for p in projects}
    built = {b.project: b.id for b in top.chain()}
    downloads = {(d.build.project, d.project, d.folder): d.made_by for d in world.downloads}
    assert top.state == "passed"
    assert sorted(b.project for b in top.chain()) == sorted(projects)
    assert {b.project: b.commit for b in top.chain()} == {p: heads[p] for p in projects}
    assert compiled_since(world, 0) == {(p, f) for p in projects for f in every}
    assert all(last[d] < first[c] for c in projects for d in graph[c])
    assert all(m.promoted.build.id == built[p] for (p, _), m in made.items())
    assert downloads == {(c, d, f): built[d] for c in projects for d in graph[c] for f in every}
    assert all(d.key == plan.projects[d.project].key for d in world.downloads)


# The names of two builds of a root at one set of commits: a branch twice; a branch and a tag
# that every project carries on those commits, in either order; or a branch, then a name no
# repository has.
TWICE = {
    "branch-then-branch": ("master", "master"),
    "branch-then-tag": ("master", TAG),
    "tag-then-branch": (TAG, "master"),
    "branch-then-neither": ("master", NEITHER),
}


@pytest.mark.parametrize("twice", sorted(TWICE))
def test_a_second_build_with_nothing_changed_builds_nothing(worlds, graph_root, shape, twice):
    """A root built again without a new commit, under the same name, under a tag on the same
    commits as a branch, either way round, or under a name that is neither a branch nor a tag,
    is reused: it compiles nothing, triggers nothing, uploads no pipeline, and its plan names
    the keys the first build made; it writes nothing into the bucket but the latest files of
    its steps under its own name, naming its key, and none under a name that is neither. Each
    of the two builds asks git about the root's repository once."""
    graph, root = graph_root.graph, graph_root.node
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    for p in graph:
        world.hosting.tag(p, TAG, world.hosting.head(p))
    names = TWICE[twice]
    asked = len(world.git.asked)
    first = world.build(root, names[0])
    done, stored, between = len(world.compiled), dict(world.store.objects), len(world.git.asked)

    again = world.build(root, names[1], commit=first.commit)

    projects, every = tree(graph, root), folders(matrix, step_ids)
    key = world.plan(again).projects[root].key.encode()
    written = every if names[1] != NEITHER else set()
    pointed = {layout.latest_file("", root, branch_ref(names[1]), f): key for f in written}
    questions = (world.git.asked[asked:between], world.git.asked[between:])
    assert again.state == "passed"
    assert (again.children, world.agents.uploads[again.id]) == ([], [])
    assert compiled_since(world, done) == set()
    assert world.store.objects == {**stored, **pointed}
    assert makers(world, again, projects, every) == makers(world, first, projects, every)
    assert [[r for r, _ in q if r == url(root)] for q in questions] == [[url(root)]] * 2


def test_a_new_commit_builds_exactly_its_node_and_the_nodes_above(worlds, graph_node, shape):
    """After a new commit of a node, the root's next build compiles that node and every node
    above it in the tree, each step on every platform, and nothing else; every download of
    that chain takes a key of its project's newest commit."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    world.build(root)
    done, fetched = len(world.compiled), len(world.downloads)

    world.commit(node)
    again = world.build(root)

    rebuilt = ({node} | above(graph, node)) & tree(graph, root)
    every = folders(matrix, step_ids)
    assert again.state == "passed"
    assert compiled_since(world, done) == {(p, f) for p in rebuilt for f in every}
    assert all(
        d.key.startswith(world.hosting.head(d.project) + "-") for d in world.downloads[fetched:]
    )


def test_two_roots_use_one_build_of_a_dependency_they_share(worlds, graph_node, shape):
    """A second root that takes a node of a built tree as its snapshot dependency compiles
    nothing of that node's tree, and downloads, on every platform and of every step, what the
    first chain made of it."""
    at = graph_node
    graph = {**at.graph, "other": (at.node,)}
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    first = world.build(at.root)
    done = len(world.compiled)

    other = world.build("other")

    (shared,) = [b for b in first.chain() if b.project == at.node]
    every = folders(matrix, step_ids)
    assert other.state == "passed"
    assert compiled_since(world, done) == {("other", f) for f in every}
    assert {(d.project, d.folder, d.made_by) for d in world.downloads if d.build is other} == {
        (at.node, f, shared.id) for f in every
    }


# How a node fails its last folder: its step fails, or its pipeline promotes that platform's
# build folder in two steps, which its gate refuses before anything compiles; and what its
# record holds then.
FAILURES = {"its-step-fails": None, "two-steps-promote-it": RULE}


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_failed_step_fails_every_build_above_and_holds_its_project_responsible(
    worlds, graph_node, shape, failure
):
    """When a step a consumer needs fails at a node, in its job or at the node's gate, that
    node's build, every build above it and the root fail; no project is built twice in the
    chain; nothing above it is promoted, nor its branch's latest file written; and the
    failure records of the chain lead from the root to that node and folder alone, or to that
    node and the rule its pipeline breaks."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    failed = Folder(matrix[-1], step_ids[-1])
    if FAILURES[failure] is None:
        world.broken.add((node, failed))
    else:
        world.projects[node].packaged = (matrix[-1],)
        world.commit(node)

    top = world.build(root)

    failing = ({node} | above(graph, node)) & tree(graph, root)
    built = [b.project for b in top.chain()]
    every = folders(matrix, step_ids)
    moved = {p for p in failing for f in every if world.latest(p, MASTER, f) is not None}
    assert top.state == "failed"
    assert {b.project for b in top.chain() if b.state == "failed"} == failing
    assert len(built) == len(set(built))
    assert made_projects(world) & above(graph, node) == set()
    assert moved <= {node}
    assert held_responsible(world, top) == {(node, FAILURES[failure] or failed)}


def first_consumer(at) -> str:
    """The first project of the node's tree that takes the node as a snapshot dependency."""
    return min(c for c in tree(at.graph, at.root) if at.node in at.graph[c])


@pytest.mark.parametrize("docs", ["passes", "fails"])
def test_a_platform_nobody_downloads_is_built_once_a_consumer_starts_to(
    worlds, lower_node, shape, docs
):
    """The steps of a platform of a dependency that no consumer downloads may fail, and the
    root passes with every needed folder made. When one consumer then starts to download that
    platform, with no change outside that consumer, the next chain compiles that consumer and
    the nodes above it and, of the dependency, the steps of that platform alone when they are
    missing; each download of it takes what the build that made it made."""
    graph, node, root = lower_node.graph, lower_node.node, lower_node.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    world.projects[node].variants += ("docs",)
    world.commit(node)
    docs_folders = folders(("docs",), step_ids)
    if docs == "fails":
        world.broken |= {(node, f) for f in docs_folders}
    first = world.build(root)
    world.broken.clear()
    consumer = first_consumer(lower_node)
    world.projects[consumer].extra = ((node, "docs", None),)
    world.commit(consumer)
    done, fetched = len(world.compiled), len(world.downloads)

    again = world.build(root)

    made = makers(world, first, tree(graph, root), folders(matrix, step_ids))
    docs_made = makers(world, again, [node], docs_folders)
    rebuilt = ({consumer} | above(graph, consumer)) & tree(graph, root)
    every = folders(matrix, step_ids)
    missing = {(node, f) for f in docs_folders} if docs == "fails" else set()
    assert (first.state, again.state) == ("passed", "passed")
    assert all(m is not None for m in made.values())
    assert compiled_since(world, done) == {(p, f) for p in rebuilt for f in every} | missing
    assert {
        (d.folder, d.made_by) for d in world.downloads[fetched:] if d.folder in docs_folders
    } == {(f, m.promoted.build.id) for (_, f), m in docs_made.items()}


def test_a_key_missing_a_step_builds_only_that_step_of_that_platform(worlds, graph_node, shape):
    """After a chain in which one step of one platform of a node failed, the next chain
    compiles that step of that platform alone of that node, and runs no other job of it, also
    none of a step that only promotes; every download takes the build that made its
    folder."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    matrix, step_ids = MATRICES["three"], shape[1]
    world = worlds(graph, matrix, step_ids)
    failed = Folder(matrix[-1], step_ids[-1])
    world.broken.add((node, failed))
    world.build(root)
    world.broken.clear()
    done, ran, fetched = len(world.compiled), len(world.ran), len(world.downloads)

    again = world.build(root)

    made = makers(world, again, tree(graph, root), folders(matrix, step_ids))
    assert again.state == "passed"
    assert {f for p, f in compiled_since(world, done) if p == node} == {failed}
    assert {c.folder for c in world.ran[ran:] if c.build.project == node} == {failed}
    assert all(
        d.made_by == made[(d.project, d.folder)].promoted.build.id
        for d in world.downloads[fetched:]
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
    worlds, lower_node, shape, when
):
    """When a node's build hard fails a step, a build of the same key fails without compiling
    it in every chain whose root started before the failure, whether that build waited at its
    gate meanwhile or reached the node later; a chain whose root started after the failure,
    or within five seconds before it was recorded, compiles that step once again, and nothing
    else of the node, also when a chain that started before has shared the failure since."""
    graph, node, root = lower_node.graph, lower_node.node, lower_node.root
    matrix, step_ids = shape
    world = worlds({**graph, "other": (node,)}, matrix, step_ids)
    failed = Folder(matrix[-1], step_ids[-1])
    first = {"compiled": 0}

    def a_chain_fails_at_the_node() -> None:
        world.now += 10
        world.broken.add((node, failed))
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
        # With the tree built, nothing below the node builds again in the failing chain, so the
        # node's record follows other's start within the margin.
        world.build(root)
        world.commit(node)
        started = at_the_node_once(start_other)
        a_chain_fails_at_the_node()
        (other,) = started
    world.play(other)

    again = [c.folder for c in world.compiled[first["compiled"] :] if c.build.project == node]
    assert other.state == ("passed" if WHEN[when] else "failed")
    assert again == ([failed] if WHEN[when] else [])


@pytest.mark.parametrize("name", ["master", TAG])
def test_a_pin_places_its_project_for_the_whole_chain(worlds, lower_node, shape, name):
    """A literal git ref in one consumer's template pins a node: the chain builds it once, at
    the head of that ref, also under a root at a tag that every project carries, and every
    download of it in the chain, on every platform and of every step, takes what that build
    made. Every started build runs under the name of the ref the plan takes it from."""
    graph, node, root = lower_node.graph, lower_node.node, lower_node.root
    world = worlds(graph, *shape)
    pinned = world.commit(node, branch="3.14.x")
    consumer = first_consumer(lower_node)
    world.projects[consumer].pins[node] = "refs/heads/3.14.x"
    world.commit(consumer)
    if name == TAG:
        for p in sorted(graph):
            world.tag(p, TAG)

    top = world.build(root, name)

    (built,) = world.builds_of(node)
    plan = world.plan(top)
    names = {b.project: b.branch for b in top.chain()[1:]}
    assert top.state == "passed"
    assert built.commit == pinned
    assert {d.made_by for d in world.downloads if d.project == node} == {built.id}
    assert names == {p: name_of(plan.projects[p].ref) for p in names}


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


TOOLS_KEYS = {n: str(n) * 40 + "-" + str(n) * 32 for n in (6, 7, 8)}


def tools_made(world, wanted, number: int, ref: str = MASTER) -> None:
    """tools' build ``number``, which made ``wanted`` at its key and promoted them under the
    name of ``ref``."""
    key = TOOLS_KEYS[number]
    url = f"https://buildkite.example.test/example/tools/builds/{number}"
    build = Build(f"tools-build-{number}", number, url)
    name = name_of(ref)
    for f in wanted:
        location = layout.step_folder("", "tools", key, f)
        made = captured([("out/tools.bin", 5)], Uploaded(name, build))
        at = "2026-11-19T00:00:00.000Z"
        write_manifest(world.store, location, promoted_by(made, Promoted(at, name, build)))
        world.store.put(layout.latest_file("", "tools", ref, f), key.encode(), "")


# Which steps of tools a root's latest successful download names: none, so it takes every
# step of the newest key, or every one by name; and which of them tools' second build
# promotes: every step, or the first alone, so that two steps of a platform come from two
# builds.
TAKEN = ("every-step", "named")

# The name a root builds under, and the branch whose latest files of tools its plan step
# reads: a branch tools lacks, whose walk goes on to master; or a tag every project carries,
# whose read takes that name's branch alone, although master names another key of tools.
READ_UNDER = {"feature": MASTER, TAG: branch_ref(TAG)}


@pytest.mark.parametrize("taken, name", paired(TAKEN, sorted(READ_UNDER)), ids=str)
def test_a_root_with_a_latest_successful_dependency_is_reused_while_its_latest_files_stay(
    worlds, graph_root, shape, taken, name
):
    """A root that takes a project as latest successful reads, in its plan step, the key of
    each step it takes through that project's latest files: under a branch the project lacks,
    those of master; under a tag, those of the entry's own branch and of no other. Without a
    step, of every step whose latest file names the newest key; of each step it names, the
    key that step's own latest file names. It writes them into each of those downloads and
    into its key.json, and its resume step and its jobs take them, although the latest files
    move while its chain runs. Built again after they moved, it compiles under a key that
    names the keys they name now; built once more, it is reused. Its snapshot dependencies
    are built once."""
    graph, root = graph_root.graph, graph_root.node
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    every = folders(matrix, step_ids)
    moves = every if taken == "every-step" else folders(matrix, step_ids[:1])
    tools_made(world, every, 6)
    tools_made(world, every, 7, READ_UNDER[name])
    world.projects[root].latest = ("tools",)
    world.projects[root].latest_step = None if taken == "every-step" else list(step_ids)
    world.commit(root)
    if name == TAG:
        for p in sorted(graph):
            world.hosting.tag(p, TAG, world.hosting.head(p))
    else:
        world.hosting.cut(root, name)
    world.at_gate = lambda build: tools_made(world, moves, 8, READ_UNDER[name])

    first = world.build(root, name)
    world.at_gate = lambda build: None
    moved, again = world.build(root, name), world.build(root, name)

    read = {
        first: {f: TOOLS_KEYS[7] for f in every},
        moved: {f: TOOLS_KEYS[8 if f in moves else 7] for f in every},
    }
    keys = [world.plan(b).projects[root].key for b in (first, moved, again)]
    deps = [json.loads(world.store.objects[layout.key_json("", root, k)])["deps"] for k in keys]
    written = {
        (b.id, tuple(sorted(e["keys"].items())))
        for b in read
        for e in entries_of(world.agents.uploads[b.id][-1], "tools")
    }
    tools = {(d.build.id, d.folder, d.key) for d in world.downloads if d.project == "tools"}
    own = {(c.build.id, c.folder) for c in world.compiled if c.build.project == root}
    assert [b.state for b in (first, moved, again)] == ["passed"] * 3
    assert keys[0] != keys[1] == keys[2]
    assert [{k for p, k in d if p == "tools"} for d in deps] == [
        set(read[b].values()) for b in (first, moved, moved)
    ]
    assert written == {
        (b.id, tuple(sorted({f.step: k for f, k in r.items()}.items()))) for b, r in read.items()
    }
    assert own == {(b.id, f) for b in read for f in every}
    assert tools == {(b.id, f, k) for b, r in read.items() for f, k in r.items()}
    assert all(len(world.builds_of(p)) == 1 for p in below(graph, root))


# The branches at which every step of a node's pipeline also downloads its own project, as a
# compatibility test does, besides the build's own branch; and the branch at which the root
# pins a dependency, a download of another project at another branch.
ELSEWHERE = ("3.14.x", "runner")
PINNED_AT = "3.13.x"


def test_the_plan_steps_page_names_each_download_of_its_own_project_at_another_branch(
    worlds, graph_node, shape
):
    """When every step of a node's pipeline also downloads its own project at other branches,
    and the root pins a dependency to yet another branch, the page of the plan step of the
    node's build, the first build or one started for a dependency alike, names each download
    of its own project at another branch, with the step, its platform and that branch, on a
    line of its own, and no other download."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    world.build(root)
    world.projects[node].elsewhere = tuple((step_ids[0], branch_ref(b)) for b in ELSEWHERE)
    world.commit(node)
    pinned = graph[root][0]
    world.commit(pinned, branch=PINNED_AT)
    world.projects[root].pins[pinned] = branch_ref(PINNED_AT)
    world.commit(root)

    top = world.build(root)

    shown = next(b for b in top.chain() if b.project == node)
    _, page = world.agents.annotations[shown.id]["snapshot-plan"]
    expected = {(f"{s}-{v}", v, b) for v in matrix for s in step_ids for b in ELSEWHERE}
    naming = [line for line in page.splitlines() if any(k in line for k, _, _ in expected)]
    assert top.state == "passed"
    assert len(naming) == len(expected)
    assert all(any(all(x in line for x in e) for line in naming) for e in expected)


def cannot_give_a_snapshot(world, at, condition: str) -> str:
    """Make the node ``at`` names, or for a latest successful download its root, unable to give
    what the plan needs by ``condition``; the name the root builds under."""
    project = at.node
    if condition == "untagged":
        for p in sorted(world.projects.keys() - {project}):
            world.tag(p, TAG)
        return TAG
    if condition == "latest-on-master-only":
        tools_made(world, folders(("linux",), ("build",)), 7)
        world.projects[at.root].latest = ("tools",)
        for p in sorted(world.projects):
            world.tag(p, TAG)
        return TAG
    if condition == "buildkite":
        world.facts[project] = PipelineFacts(url(project), False)
    elif condition == "shape":
        world.projects[project].pipeline_py = SHAPED.replace(b"run(", b"print(")
    else:
        world.projects[project].latest = ("tools",)
    world.commit(project)
    return "master"


CONDITIONS = ("buildkite", "shape", "latest", "untagged", "latest-on-master-only")


@pytest.mark.parametrize("lower_node, condition", paired(LOWER_NODES, CONDITIONS), ids=str)
def test_a_dependency_that_cannot_give_a_snapshot_stops_the_plan(worlds, lower_node, condition):
    """A dependency that cannot give what the plan needs fails the root before anything is
    triggered: no plan, no trigger, no compile, nothing stored. A snapshot dependency cannot
    when Buildkite's configuration does not run its pipeline.py, that file lacks the runner's
    call, it takes a project as latest successful, or under a root at a tag its repository
    lacks the tag; a latest successful one, under a root at a tag, when the latest files of the
    tag's name name no key of it, although master's do."""
    world = worlds(lower_node.graph)
    name = cannot_give_a_snapshot(world, lower_node, condition)
    stored = dict(world.store.objects)

    top = world.build(lower_node.root, name)

    assert top.state == "failed"
    assert (top.children, world.compiled, world.store.objects) == ([], [], stored)
    assert "snapshot-plan" not in world.agents.meta_data[top.id]


# What SNAPSHOT_REBUILD builds again of a tree: its root, or every project.
REBUILT = {"1": lambda graph, root: {root}, "all": tree}


@pytest.mark.parametrize("rebuild", sorted(REBUILT))
def test_snapshot_rebuild_builds_again_and_keeps_what_was_made(worlds, graph_root, shape, rebuild):
    """SNAPSHOT_REBUILD=1 compiles the root again although its key is made, and no dependency;
    SNAPSHOT_REBUILD=all compiles every project of the tree again. Either leaves what the
    earlier build made as it was, and the latest files of every project it did not build
    again; the next build at the branch's tip, which is reused, points the root's latest
    files back at the earlier key."""
    graph, root = graph_root.graph, graph_root.node
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    world.build(root)
    done, stored = len(world.compiled), dict(world.store.objects)

    again = world.build(root, SNAPSHOT_REBUILD=rebuild)
    compiled = compiled_since(world, done)
    after = world.build(root)

    rebuilt = REBUILT[rebuild](graph, root)
    moved = {k for k in stored if k.split("/")[1] == "refs" and k.split("/")[0] in rebuilt}
    kept = {k: b for k, b in stored.items() if k not in moved}
    own = {k: b for k, b in stored.items() if k in moved and k.startswith(f"{root}/")}
    assert (again.state, after.state) == ("passed", "passed")
    assert compiled == {(p, f) for p in rebuilt for f in folders(*shape)}
    assert compiled_since(world, done) == compiled
    assert kept.items() <= world.store.objects.items()
    assert own.items() <= world.store.objects.items()


def canonical(document) -> bytes:
    """``document`` as canonical JSON: members sorted, no white space, UTF-8."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def test_key_json_read_down_from_any_key_gives_every_commit_of_its_tree(worlds, graph_root):
    """After a root's first build, the key.json of the key the plan names for any project of
    its tree, read down through the keys it names, gives the commit of every project of that
    project's tree; every key is its commit, a dash and the first 32 hex digits of the SHA-256
    of its key.json, which is canonical JSON with its deps sorted."""
    graph, root = graph_root.graph, graph_root.node
    world = worlds(graph)

    top = world.build(root)

    plan = world.plan(top)
    stored = {}

    def read_down(project: str, key: str) -> dict[str, str]:
        """The commit of ``project`` and of every project below it, read from key.json down."""
        stored[key] = world.store.objects[layout.key_json("", project, key)]
        found = json.loads(stored[key])
        commits = {project: found["commit"]}
        for d, k in found["deps"]:
            commits.update(read_down(d, k))
        return commits

    assert top.state == "passed"
    for p in tree(graph, root):
        key = plan.projects[p].key
        assert read_down(p, key) == {q: plan.projects[q].commit for q in tree(graph, p)}
    for key, body in stored.items():
        found = json.loads(body)
        assert key == f"{found['commit']}-{hashlib.sha256(body).hexdigest()[:32]}"
        assert body == canonical(found)
        assert found["deps"] == sorted(found["deps"])


# The name a first build runs under: a story branch, a release line, or a tag.
FIRST_NAMES = ("feature", "3.14.x", TAG)

# Where that name lies, on a commit of its own in each project: in the projects below the
# node and the first build's own, at master's newest commits there, so that a started build
# lacking the name takes them under another ref than the plan; in the node and every project
# above it; in every project; or in the projects below the node and the first build's own.
# A project without it lacks the branch, or carries the tag on master's newest commit, since
# a build at a tag takes every dependency from that tag.
SPREADS = (
    "below-the-node-at-masters-commits",
    "the-node-and-above",
    "every-project",
    "below-the-node",
)

# A pin of the node by its first dependent, on a commit of its own: none, to the release line
# 3.14.x, or to a tag.
PIN_TAG = "v0.9.0"
PINS = (None, "3.14.x", PIN_TAG)

# Whether the first build is played once, or first with the last step of the node's last
# platform failing and then again, so that the node's build finds its other steps made.
PLAYED = ("once", "after-a-failed-step")


def pin_the_node(world, at, pin: str) -> None:
    """Pin the node of ``at`` to ``pin``, a branch or a tag on a commit of its own, in the
    template of its first dependent."""
    if pin == PIN_TAG:
        world.tag(at.node, pin)
        ref = tag_ref(pin)
    else:
        world.commit(at.node, branch=pin)
        ref = branch_ref(pin)
    consumer = first_consumer(at)
    world.projects[consumer].pins[at.node] = ref
    world.commit(consumer)


def named_projects(at, spread: str) -> frozenset[str]:
    """The projects of the node's tree that carry the first build's name as ``spread`` says."""
    graph, node, root = at.graph, at.node, at.root
    if spread == "the-node-and-above":
        return ({node} | above(graph, node)) & tree(graph, root)
    if spread == "every-project":
        return tree(graph, root)
    return below(graph, node) | {root}


def put_name(world, project: str, name: str, own_commit: bool) -> None:
    """Put the branch or the tag ``name`` in the repository of ``project``: on a commit of its
    own, or on master's newest commit."""
    if name != TAG and own_commit:
        world.commit(project, branch=name)
    elif name != TAG:
        world.hosting.cut(project, name)
    elif own_commit:
        world.tag(project, name)
    else:
        world.hosting.tag(project, name, world.hosting.head(project))


def name_the_tree(world, at, name: str, spread: str) -> None:
    """Put the first build's ``name`` on the projects of the node's tree as ``spread`` says."""
    named = named_projects(at, spread)
    own_commits = not spread.endswith("at-masters-commits")
    for p in sorted(tree(at.graph, at.root)):
        if p in named:
            put_name(world, p, name, own_commits)
        elif name == TAG:
            put_name(world, p, name, False)


def own_build_takes_the_same_commits(world, graph: dict, plan, build) -> bool:
    """Whether a first build of the project of ``build`` at its name and commit, played now,
    plans every project below it at the commit ``plan`` takes it at; not when that build
    cannot be planned."""
    own = world.build(build.project, build.branch, build.commit)
    if "snapshot-plan" not in world.agents.meta_data[own.id]:
        return False
    projects = world.plan(own).projects
    return all(
        m in projects and projects[m].commit == plan.projects[m].commit
        for m in below(graph, build.project)
    )


@pytest.mark.parametrize(
    "name, spread, pin, played", paired(FIRST_NAMES, SPREADS, PINS, PLAYED), ids=str
)
def test_a_started_build_writes_its_branchs_latest_files_as_its_branchs_own_build(
    worlds, graph_node, shape, name, spread, pin, played
):
    """Every build started for a dependency runs under the name of its ref, and leaves that
    branch's latest files, on every platform and of every step, through its promotes and
    through its plan step for the steps it finds made, exactly when a first build of its
    project at its ref and commit, played after, takes every project below it at the same
    commit; else it leaves none, also when that build cannot be planned. The fakes know no
    merge-base, so every branch leaves master, as the rule takes it. Every case is one in
    which the plan was told all that the own build asks: a pin to a branch neither the first
    build's nor a release line, or one that moves a project with dependencies off the first
    build's name, is not generated, as the rule counts what it was not told as no."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    if pin is not None and node != root:
        pin_the_node(world, graph_node, pin)
    name_the_tree(world, graph_node, name, spread)
    if played == "after-a-failed-step":
        world.broken.add((node, Folder(matrix[-1], step_ids[-1])))
        world.build(root, name)
        world.broken.clear()

    top = world.build(root, name)

    plan, every = world.plan(top), folders(matrix, step_ids)
    started = [b for b in world.builds if "SNAPSHOT_PLAN" in b.env]
    last = {b.project: b for b in started}
    left = {p: world.objects(f"{p}/refs/") for p in last}
    speaks = {p: own_build_takes_the_same_commits(world, graph, plan, b) for p, b in last.items()}

    def latest_files(p: str) -> dict[str, bytes]:
        ref, key = branch_ref(name_of(plan.projects[p].ref)), plan.projects[p].key
        return {layout.latest_file("", p, ref, f): key.encode() for f in every}

    assert top.state == "passed"
    assert [b.branch for b in started] == [name_of(plan.projects[b.project].ref) for b in started]
    assert left == {p: latest_files(p) if speaks[p] else {} for p in last}


# Where a name is put on the commits a chain built: a branch cut in every project of the tree,
# or in the node of the failed step and the projects above it, whose own builds take the
# projects below from master, at the same commits; or a tag put on every project of the tree,
# which, once, moves off the node's commit while the node's build waits at its gate, and is
# put back after the chain.
PUT_ON = {
    "every-project": ("feature", False),
    "the-node-and-above": ("feature", False),
    "a-tag-on-every-project": (TAG, False),
    "a-tag-that-moves-while-the-node-waits": (TAG, True),
}


@pytest.mark.parametrize("put_on", sorted(PUT_ON))
def test_a_build_at_its_names_commit_points_its_latest_files_at_its_key(worlds, graph_node, put_on):
    """After a chain in which one step of one platform of a node failed, a branch is cut, or a
    tag put, at the same commits, and the root's build under that name builds that step
    again. Every build of that chain that runs under the name and writes its branch's latest
    files leaves the name's latest file of every step it promoted naming its key, and of every
    step it found made when git names its commit for the name as it starts, and not when the
    name moved off its commit while it waited; a project the chain did not build leaves
    none. Once the root's name has moved on to a newer commit and built it, a build of
    the older commit under the name, which is reused, leaves the root's latest files naming
    the newer key."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    matrix, step_ids = MATRICES["three"], STEP_IDS["build-notes"]
    world = worlds(graph, matrix, step_ids)
    failed = Folder(matrix[-1], step_ids[-1])
    world.broken.add((node, failed))
    world.build(root)
    world.broken.clear()
    projects, (name, moves) = tree(graph, root), PUT_ON[put_on]
    node_and_above = ({node} | above(graph, node)) & projects
    for p in node_and_above if put_on == "the-node-and-above" else projects:
        if name == TAG:
            world.hosting.tag(p, TAG, world.hosting.head(p))
        else:
            world.hosting.cut(p, name)
    if moves:
        world.at_gate = lambda b: world.tag(node, TAG) if b.project == node else None

    top = world.build(root, branch=name)
    world.at_gate = lambda b: None
    if moves:
        world.hosting.tag(node, TAG, world.hosting.head(node))
    plan, every = world.plan(top), folders(matrix, step_ids)
    named = {p: {f: world.latest(p, branch_ref(name), f) for f in every} for p in projects}
    if name == TAG:
        world.tag(root, TAG)
    else:
        world.commit(root, branch=name)
    newer = world.build(root, branch=name)
    older = world.play(world.start(root, name, top.commit, {}))

    built = {b.project: b.branch for b in top.chain()}
    writes = {p for p in built if built[p] == name and plan.projects[p].writes_latest_file}
    waited = {node} - {root} if moves else set()
    expected = {
        p: {
            f: plan.projects[p].key if p in writes and (p not in waited or f == failed) else None
            for f in every
        }
        for p in projects
    }
    kept = {f: world.latest(root, branch_ref(name), f) for f in every}
    assert (top.state, newer.state, older.state) == ("passed", "passed", "passed")
    assert node in built
    assert named == expected
    assert (older.children, world.agents.uploads[older.id]) == ([], [])
    assert kept == {f: world.plan(newer).projects[root].key for f in every}
