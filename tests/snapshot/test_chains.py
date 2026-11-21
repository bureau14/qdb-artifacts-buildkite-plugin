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
from lib.layout import Folder
from lib.manifest import Build, Promoted, Uploaded, captured, promoted_by, write_manifest
from lib.snapshot.buildkite import PipelineFacts
from lib.snapshot.records import RULE

MASTER = "refs/heads/master"


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


def test_a_first_build_builds_every_project_of_its_tree_once_dependencies_first(
    worlds, graph_root, shape
):
    """A root's first build compiles every step of every project of its tree in one build,
    each after its dependencies, at the commit the root planned, although every dependency
    gets a new commit while the chain runs; every download of every node takes every step of
    the dependency at the key the plan names, made by the build of the chain; every folder of
    every project is made."""
    graph, root = graph_root.graph, graph_root.node
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    heads = {p: world.hosting.head(p) for p in graph}
    world.at_gate = lambda build: world.commit(build.project)

    top = world.build(root)

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


def test_a_second_build_with_nothing_changed_builds_nothing(worlds, graph_root, shape):
    """A root built again without a new commit is reused: it compiles nothing, triggers
    nothing, uploads no pipeline and writes nothing into the bucket, and its plan names the
    keys the first build made."""
    graph, root = graph_root.graph, graph_root.node
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    first = world.build(root)
    done, stored = len(world.compiled), dict(world.store.objects)

    again = world.build(root)

    projects, every = tree(graph, root), folders(matrix, step_ids)
    assert again.state == "passed"
    assert (again.children, world.agents.uploads[again.id]) == ([], [])
    assert compiled_since(world, done) == set()
    assert world.store.objects == stored
    assert makers(world, again, projects, every) == makers(world, first, projects, every)


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


def test_a_pin_places_its_project_for_the_whole_chain(worlds, lower_node, shape):
    """A literal git ref in one consumer's template pins a node: the chain builds it once, at
    the head of that ref, and every download of it in the chain, on every platform and of
    every step, takes what that build made."""
    graph, node, root = lower_node.graph, lower_node.node, lower_node.root
    world = worlds(graph, *shape)
    pinned = world.commit(node, branch="3.14.x")
    consumer = first_consumer(lower_node)
    world.projects[consumer].pins[node] = "refs/heads/3.14.x"
    world.commit(consumer)

    top = world.build(root)

    (built,) = world.builds_of(node)
    assert top.state == "passed"
    assert built.commit == pinned
    assert {d.made_by for d in world.downloads if d.project == node} == {built.id}


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


TOOLS_KEYS = {7: "7" * 40 + "-" + "7" * 32, 8: "8" * 40 + "-" + "8" * 32}


def tools_made(world, wanted, number: int) -> None:
    """tools' build ``number``, which made ``wanted`` at its key and promoted them on master."""
    key = TOOLS_KEYS[number]
    url = f"https://buildkite.example.test/example/tools/builds/{number}"
    build = Build(f"tools-build-{number}", number, url)
    for f in wanted:
        location = layout.step_folder("", "tools", key, f)
        made = captured([("out/tools.bin", 5)], Uploaded("master", build))
        at = "2026-11-19T00:00:00.000Z"
        write_manifest(world.store, location, promoted_by(made, Promoted(at, "master", build)))
        world.store.put(layout.latest_file("", "tools", MASTER, f), key.encode(), "")


# Which steps of tools a root's latest successful download names: none, so it takes every
# step of the newest key, or every one by name; and which of them tools' second build
# promotes: every step, or the first alone, so that two steps of a platform come from two
# builds.
TAKEN = ("every-step", "named")


@pytest.mark.parametrize("taken", TAKEN)
def test_a_root_with_a_latest_successful_dependency_is_reused_while_its_latest_files_stay(
    worlds, graph_root, shape, taken
):
    """A root on a branch that a project it takes as latest successful lacks reads, in its
    plan step, through that project's latest files of master, the key of each step it takes:
    without a step, of every step whose latest file names the newest key; of each step it
    names, the key that step's own latest file names. It writes them into each of those
    downloads and into its key.json, and its resume step and its jobs take them, although
    the latest files move while its chain runs. Built again after they moved, it compiles
    under a key that names the keys they name now; built once more, it is reused. Its
    snapshot dependencies are built once."""
    graph, root = graph_root.graph, graph_root.node
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    every = folders(matrix, step_ids)
    moves = every if taken == "every-step" else folders(matrix, step_ids[:1])
    tools_made(world, every, 7)
    world.projects[root].latest = ("tools",)
    world.projects[root].latest_step = None if taken == "every-step" else list(step_ids)
    world.commit(root)
    world.hosting.cut(root, "feature")
    world.at_gate = lambda build: tools_made(world, moves, 8)

    first = world.build(root, "feature")
    world.at_gate = lambda build: None
    moved, again = world.build(root, "feature"), world.build(root, "feature")

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


# What SNAPSHOT_REBUILD builds again of a tree: its root, or every project.
REBUILT = {"1": lambda graph, root: {root}, "all": tree}


@pytest.mark.parametrize("rebuild", sorted(REBUILT))
def test_snapshot_rebuild_builds_again_and_keeps_what_was_made(worlds, graph_root, shape, rebuild):
    """SNAPSHOT_REBUILD=1 compiles the root again although its key is made, and no dependency;
    SNAPSHOT_REBUILD=all compiles every project of the tree again. Either leaves what the
    earlier build made as it was, and the next build at the branch's tip, which is reused,
    points the root's latest files back at the earlier key."""
    graph, root = graph_root.graph, graph_root.node
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    world.build(root)
    done, stored = len(world.compiled), dict(world.store.objects)

    again = world.build(root, SNAPSHOT_REBUILD=rebuild)
    compiled = compiled_since(world, done)
    after = world.build(root)

    latest = {k for k in stored if k.split("/")[1] == "refs"}
    kept = {k: b for k, b in stored.items() if k not in latest}
    own = {k: b for k, b in stored.items() if k in latest and k.startswith(f"{root}/")}
    assert (again.state, after.state) == ("passed", "passed")
    assert compiled == {(p, f) for p in REBUILT[rebuild](graph, root) for f in folders(*shape)}
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


RELEASE_LINES = ("master", "3.14.x")


def runs_under(graph: dict, node: str, branch_of: dict, root_branch: str) -> str:
    """The rule of the branch for a tree without pins in which every project has a repository
    of its own: a node runs under its own branch when its build is the one its own pipeline
    would make for that branch, that is when its branch is the root's and that is a release
    line or every node below it is on it too, or when its branch is a release line every node
    below it is on; else under ``snapshot/<root branch>``."""
    own = branch_of[node]
    level = all(branch_of[m] == own for m in below(graph, node))
    as_itself = (own == root_branch and (own in RELEASE_LINES or level)) or (
        own in RELEASE_LINES and level
    )
    return own if as_itself else f"snapshot/{root_branch}"


def latest_refs(world, project: str) -> set[str]:
    """The refs under which a latest file of some folder of ``project`` was written."""
    found = (k.split("/") for k in world.store.objects if k.startswith(f"{project}/refs/"))
    return {"/".join(parts[1:-2]) for parts in found}


@pytest.mark.parametrize("root_branch", ["master", "feature", "3.14.x"])
def test_a_node_runs_under_its_own_branch_only_as_its_own_build(
    worlds, graph_node, shape, root_branch
):
    """When a root builds a branch that a node and every node above it also have, each
    triggered build runs under the branch the rule gives: its own when it is the build its own
    pipeline would make, and then its promotes write that branch's latest files on every
    platform; else snapshot/<root branch>, and then they write none."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    world = worlds(graph, *shape)
    branched = ({node} | above(graph, node)) & tree(graph, root)
    for p in sorted(branched):
        world.commit(p, branch=root_branch)

    top = world.build(root, branch=root_branch)

    branch_of = {p: root_branch if p in branched else "master" for p in tree(graph, root)}
    triggered = top.chain()[1:]
    assert top.state == "passed"
    assert {b.project: b.branch for b in triggered} == {
        b.project: runs_under(graph, b.project, branch_of, root_branch) for b in triggered
    }
    assert all(
        latest_refs(world, b.project)
        == (set() if b.branch.startswith("snapshot/") else {"refs/heads/" + b.branch})
        for b in triggered
    )


# Where a branch is cut at the commits a chain built: in every project of the tree, or in the
# node of the failed step and the projects above it, so that the node is not the build its own
# pipeline would make for the branch, unless nothing lies below it.
CUTS = ("every-project", "the-node-and-above")


@pytest.mark.parametrize("cut", CUTS)
def test_a_build_at_its_branchs_tip_points_its_latest_files_at_its_key(worlds, graph_node, cut):
    """After a chain in which one step of one platform of a node failed, a branch is cut at
    the same commits, and the root's build on it builds that step again. Every build of that
    chain that runs under the branch, at its newest commit, leaves the branch's latest file
    of every step of its project naming its key, of the steps it found made as well as those
    it promoted; one under a snapshot/ name leaves none, and neither does a project the chain
    did not build. Once the root's branch has moved on to a newer commit and built it, a
    build of the older commit, which is reused, leaves the root's latest files naming the
    newer key."""
    graph, node, root = graph_node.graph, graph_node.node, graph_node.root
    matrix, step_ids = MATRICES["three"], STEP_IDS["build-notes"]
    world = worlds(graph, matrix, step_ids)
    world.broken.add((node, Folder(matrix[-1], step_ids[-1])))
    world.build(root)
    world.broken.clear()
    projects = tree(graph, root)
    cut_in = projects if cut == "every-project" else ({node} | above(graph, node)) & projects
    for p in cut_in:
        world.hosting.cut(p, "feature")

    top = world.build(root, branch="feature")
    plan, every = world.plan(top), folders(matrix, step_ids)
    named = {p: {f: world.latest(p, "refs/heads/feature", f) for f in every} for p in projects}
    world.commit(root, branch="feature")
    newer = world.build(root, branch="feature")
    older = world.play(world.start(root, "feature", top.commit, {}))

    built = {b.project: b.branch for b in top.chain()}
    expected = {
        p: {f: plan.projects[p].key if built.get(p) == "feature" else None for f in every}
        for p in projects
    }
    snapshot_refs = {r for p in projects for r in latest_refs(world, p) if "/snapshot/" in r}
    kept = {f: world.latest(root, "refs/heads/feature", f) for f in every}
    assert (top.state, newer.state, older.state) == ("passed", "passed", "passed")
    assert node in built
    assert named == expected
    assert snapshot_refs == set()
    assert (older.children, world.agents.uploads[older.id]) == ([], [])
    assert kept == {f: world.plan(newer).projects[root].key for f in every}
