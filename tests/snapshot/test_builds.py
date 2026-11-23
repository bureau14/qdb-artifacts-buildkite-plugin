"""The builds of a tree, played in process over the fakes of the planning half (``builds.py``).

The four guarantees of snapshot dependencies, and what a person relies on around them:

1. a build uses up-to-date dependencies;
2. a dependency shared by several projects of one tree is one build for all of them;
3. a failed dependency fails its dependents;
4. the same on every platform of the matrix, and for every step id of a platform.

Every case is a graph of ``builds.py`` (chain, diamond, wide, the real five
projects), a project or a top of it, a platform matrix, and the step ids of a
platform.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import json

import pytest
import yaml
from builds import (
    GRAPH_PROJECTS,
    LOWER_PROJECTS,
    MATRICES,
    SHAPED,
    SHAPES,
    STEP_IDS,
    above,
    below,
    folders,
    tree,
)
from fakes import url
from world import paired

from lib import layout
from lib.forms import branch_of, branch_ref, name_of, tag_ref
from lib.job import Build
from lib.layout import Folder
from lib.manifest import Promoted, Uploaded, captured, write_manifest
from lib.snapshot.buildkite import PipelineFacts
from lib.snapshot.lines import is_release_line, release_line
from lib.snapshot.records import RULE

MASTER = "refs/heads/master"
TAG = "v1.0.0"
# A name no repository has, as a Rebuild of a build of a deleted branch runs under.
NEITHER = "gone"


def compiled_since(world, done: int) -> set[tuple[str, Folder]]:
    """``(project, folder)`` of every compile after the first ``done``."""
    return {(c.build.project, c.folder) for c in world.compiled[done:]}


def makers(world, root_build, projects, wanted) -> dict:
    """The manifest of each folder of each project made at the key the root build's plan
    names; None where it is not made."""
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
    failed before it built, found by following the root build's failure record down the
    records of its tree that it names as causes."""
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
    """The name a root build of ``graph`` builds under, and the commit its plan takes each
    project at: the branch master, at its tip; a tag every project carries, on a commit of its
    own; a name no repository has, at master's tip."""
    if named == "tag":
        return TAG, {p: world.tag(p, TAG) for p in sorted(graph)}
    tips = {p: world.hosting.tip(p) for p in graph}
    return ("master" if named == "branch" else NEITHER), tips


@pytest.mark.parametrize("named", ["branch", "tag", "neither"])
def test_a_root_build_builds_every_project_of_its_tree_once_dependencies_first(
    worlds, graph_root, shape, named
):
    """A root build, under a branch, a tag or a name that is neither, compiles every step of
    every project of its tree in one build, each after its dependencies, at the commit its plan
    takes it at, although every dependency gets a new commit while the builds of the tree run;
    every download of every project takes every step of the dependency at the key the plan
    names, made by the dependency's build in the tree; every folder of every project is
    made."""
    graph, root = graph_root.graph, graph_root.project
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    name, tips = taken_at(world, graph, named)
    world.at_plan_step = lambda build: world.commit(build.project)

    root_build = world.build(root, name, commit=tips[root])

    projects, every = tree(graph, root), folders(matrix, step_ids)
    made = makers(world, root_build, projects, every)
    plan = world.plan(root_build)
    order = [c.build.project for c in world.compiled]
    first_compile = {p: order.index(p) for p in projects}
    last_compile = {p: len(order) - 1 - order[::-1].index(p) for p in projects}
    built = {b.project: b.id for b in root_build.tree()}
    downloads = {(d.build.project, d.project, d.folder): d.made_by for d in world.downloads}
    assert root_build.state == "passed"
    assert sorted(b.project for b in root_build.tree()) == sorted(projects)
    assert {b.project: b.commit for b in root_build.tree()} == {p: tips[p] for p in projects}
    assert compiled_since(world, 0) == {(p, f) for p in projects for f in every}
    assert all(last_compile[d] < first_compile[c] for c in projects for d in graph[c])
    assert all(m.promoted.build.id == built[p] for (p, _), m in made.items())
    assert downloads == {(c, d, f): built[d] for c in projects for d in graph[c] for f in every}
    assert all(d.key == plan.projects[d.project].key for d in world.downloads)


# The names of two root builds of a top at one set of commits: a branch twice; a branch and a
# tag that every project carries on those commits, in either order; or a branch, then a name
# no repository has.
TWICE = {
    "branch-then-branch": ("master", "master"),
    "branch-then-tag": ("master", TAG),
    "tag-then-branch": (TAG, "master"),
    "branch-then-neither": ("master", NEITHER),
}


@pytest.mark.parametrize("twice", sorted(TWICE))
def test_a_second_build_with_nothing_changed_builds_nothing(worlds, graph_root, shape, twice):
    """A top built again without a new commit, under the same name, under a tag on the same
    commits as a branch, either way round, or under a name that is neither a branch nor a tag,
    is reused: it compiles nothing, triggers nothing, uploads no pipeline, and its plan names
    the keys the earlier build made; it writes nothing into the bucket but the latest files of
    its steps under its own name, naming its key, and none under a name that is neither. Each
    of the two builds asks git about the top's repository once."""
    graph, root = graph_root.graph, graph_root.project
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    for p in graph:
        world.hosting.tag(p, TAG, world.hosting.tip(p))
    names = TWICE[twice]
    asked = len(world.git.asked)
    earlier = world.build(root, names[0])
    done, stored, between = len(world.compiled), dict(world.store.objects), len(world.git.asked)

    again = world.build(root, names[1], commit=earlier.commit)

    projects, every = tree(graph, root), folders(matrix, step_ids)
    key = world.plan(again).projects[root].key.encode()
    written = every if names[1] != NEITHER else set()
    pointed = {layout.latest_file("", root, branch_ref(names[1]), f): key for f in written}
    questions = (world.git.asked[asked:between], world.git.asked[between:])
    assert again.state == "passed"
    assert (again.children, world.agents.uploads[again.id]) == ([], [])
    assert compiled_since(world, done) == set()
    assert world.store.objects == {**stored, **pointed}
    assert makers(world, again, projects, every) == makers(world, earlier, projects, every)
    assert [[r for r, _ in q if r == url(root)] for q in questions] == [[url(root)]] * 2


def test_a_new_commit_builds_exactly_its_project_and_the_projects_above(
    worlds, graph_project, shape
):
    """After a new commit of a project, the next root build compiles that project and every
    project above it in the tree, each step on every platform, and nothing else; every
    download of the builds of that tree takes a key of its project's newest commit."""
    graph, project, root = graph_project.graph, graph_project.project, graph_project.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    world.build(root)
    done, fetched = len(world.compiled), len(world.downloads)

    world.commit(project)
    again = world.build(root)

    rebuilt = ({project} | above(graph, project)) & tree(graph, root)
    every = folders(matrix, step_ids)
    assert again.state == "passed"
    assert compiled_since(world, done) == {(p, f) for p in rebuilt for f in every}
    assert all(
        d.key.startswith(world.hosting.tip(d.project) + "-") for d in world.downloads[fetched:]
    )


def test_two_trees_use_one_build_of_a_dependency_they_share(worlds, graph_project, shape):
    """A second top that takes a project of a built tree as its snapshot dependency compiles
    nothing of that project's tree, and downloads, on every platform and of every step, what
    the builds of the first tree made of it."""
    at = graph_project
    graph = {**at.graph, "other": (at.project,)}
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    root_build = world.build(at.root)
    done = len(world.compiled)

    other = world.build("other")

    (shared,) = [b for b in root_build.tree() if b.project == at.project]
    every = folders(matrix, step_ids)
    assert other.state == "passed"
    assert compiled_since(world, done) == {("other", f) for f in every}
    assert {(d.project, d.folder, d.made_by) for d in world.downloads if d.build is other} == {
        (at.project, f, shared.id) for f in every
    }


# How a project fails its last folder: its step fails, or its pipeline promotes that
# platform's build folder in two steps, which its plan step refuses before anything compiles;
# and what its record holds then.
FAILURES = {"its-step-fails": None, "two-steps-promote-it": RULE}


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_a_failed_step_fails_every_build_above_and_holds_its_project_responsible(
    worlds, graph_project, shape, failure
):
    """When a step a dependent needs fails at a project, in its job or at the project's plan
    step, that project's build, every build above it and the root build fail; no project is
    built twice in the tree; nothing above it is promoted, nor its branch's latest file
    written; and the failure records of the tree lead from the root build to that project and
    folder alone, or to that project and the rule its pipeline breaks."""
    graph, project, root = graph_project.graph, graph_project.project, graph_project.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    failed = Folder(matrix[-1], step_ids[-1])
    if FAILURES[failure] is None:
        world.broken.add((project, failed))
    else:
        world.projects[project].packaged = (matrix[-1],)
        world.commit(project)

    root_build = world.build(root)

    failing = ({project} | above(graph, project)) & tree(graph, root)
    built = [b.project for b in root_build.tree()]
    every = folders(matrix, step_ids)
    moved = {p for p in failing for f in every if world.latest(p, MASTER, f) is not None}
    assert root_build.state == "failed"
    assert {b.project for b in root_build.tree() if b.state == "failed"} == failing
    assert len(built) == len(set(built))
    assert made_projects(world) & above(graph, project) == set()
    assert moved <= {project}
    assert held_responsible(world, root_build) == {(project, FAILURES[failure] or failed)}


def first_dependent(at) -> str:
    """The first project of the root build's tree that takes the project as a snapshot
    dependency."""
    return min(c for c in tree(at.graph, at.root) if at.project in at.graph[c])


@pytest.mark.parametrize("docs", ["passes", "fails"])
def test_a_platform_nobody_downloads_is_built_once_a_dependent_starts_to(
    worlds, lower_project, shape, docs
):
    """The steps of a platform of a dependency that no dependent downloads may fail, and the
    root build passes with every needed folder made. When one dependent then starts to
    download that platform, with no change outside that dependent, the next root build
    compiles that dependent and the projects above it and, of the dependency, the steps of
    that platform alone when they are missing; each download of it takes what the build that
    made it made."""
    graph, project, root = lower_project.graph, lower_project.project, lower_project.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    world.projects[project].variants += ("docs",)
    world.commit(project)
    docs_folders = folders(("docs",), step_ids)
    if docs == "fails":
        world.broken |= {(project, f) for f in docs_folders}
    earlier = world.build(root)
    world.broken.clear()
    dependent = first_dependent(lower_project)
    world.projects[dependent].extra = ((project, "docs", None),)
    world.commit(dependent)
    done, fetched = len(world.compiled), len(world.downloads)

    again = world.build(root)

    made = makers(world, earlier, tree(graph, root), folders(matrix, step_ids))
    docs_made = makers(world, again, [project], docs_folders)
    rebuilt = ({dependent} | above(graph, dependent)) & tree(graph, root)
    every = folders(matrix, step_ids)
    missing = {(project, f) for f in docs_folders} if docs == "fails" else set()
    assert (earlier.state, again.state) == ("passed", "passed")
    assert all(m is not None for m in made.values())
    assert compiled_since(world, done) == {(p, f) for p in rebuilt for f in every} | missing
    assert {
        (d.folder, d.made_by) for d in world.downloads[fetched:] if d.folder in docs_folders
    } == {(f, m.promoted.build.id) for (_, f), m in docs_made.items()}


def test_a_key_missing_a_step_builds_only_that_step_of_that_platform(worlds, graph_project, shape):
    """After the builds of a tree in which one step of one platform of a project failed, the
    next root build compiles that step of that platform alone of that project, and runs no
    other job of it, also none of a step that only promotes; every download takes the build
    that made its folder."""
    graph, project, root = graph_project.graph, graph_project.project, graph_project.root
    matrix, step_ids = MATRICES["three"], shape[1]
    world = worlds(graph, matrix, step_ids)
    failed = Folder(matrix[-1], step_ids[-1])
    world.broken.add((project, failed))
    world.build(root)
    world.broken.clear()
    done, ran, fetched = len(world.compiled), len(world.ran), len(world.downloads)

    again = world.build(root)

    made = makers(world, again, tree(graph, root), folders(matrix, step_ids))
    assert again.state == "passed"
    assert {f for p, f in compiled_since(world, done) if p == project} == {failed}
    assert {c.folder for c in world.ran[ran:] if c.build.project == project} == {failed}
    assert all(
        d.made_by == made[(d.project, d.folder)].promoted.build.id
        for d in world.downloads[fetched:]
    )


# When the root build of ``other`` starts against the hard failure of a project, and whether
# the builds of its tree compile the project again: only when it started after the failure,
# or so shortly before it was recorded (records.SHARED_AFTER) that it counts as after.
WHEN = {
    "waits-at-the-plan-step": False,
    "reaches-it-later": False,
    "starts-after": True,
    "starts-after-and-an-earlier-tree-shares-it": True,
    "starts-as-it-is-recorded": True,
}


@pytest.mark.parametrize("when", sorted(WHEN))
def test_a_hard_failure_fails_the_same_snapshot_in_every_tree_whose_root_build_started_before_it(
    worlds, lower_project, shape, when
):
    """When a project's build hard fails a step, a build of the same key fails without
    compiling it in every tree whose root build started before the failure, whether that
    build waited at its plan step meanwhile or reached the project later; a tree whose root build started after the failure, or within five seconds before it was recorded, compiles
    that step once again, and nothing else of the project, also when a tree that started
    before has shared the failure since."""
    graph, project, root = lower_project.graph, lower_project.project, lower_project.root
    matrix, step_ids = shape
    world = worlds({**graph, "other": (project,)}, matrix, step_ids)
    failed = Folder(matrix[-1], step_ids[-1])
    after_failure = {"compiled": 0}

    def a_tree_fails_at_the_project() -> None:
        world.now += 10
        world.broken.add((project, failed))
        world.build(root)
        world.broken.clear()
        world.now += 10
        after_failure["compiled"] = len(world.compiled)

    def at_the_project_once(then) -> list:
        """Call ``then`` when the next build of the project waits at its plan step; what it
        gave."""
        given: list = []

        def at_plan_step(build) -> None:
            if build.project == project:
                world.at_plan_step = lambda b: None
                given.append(then())

        world.at_plan_step = at_plan_step
        return given

    def start_other():
        return world.start("other", "master", None, {})

    if when == "waits-at-the-plan-step":
        other = start_other()
        at_the_project_once(a_tree_fails_at_the_project)
    elif when == "reaches-it-later":
        other = start_other()
        a_tree_fails_at_the_project()
    elif when == "starts-after":
        a_tree_fails_at_the_project()
        other = start_other()
    elif when == "starts-after-and-an-earlier-tree-shares-it":
        earlier = world.start(root, "master", None, {})
        a_tree_fails_at_the_project()
        other = start_other()
        world.now += 10
        world.play(earlier)
    else:
        # With the tree built, nothing below the project builds again in the failing tree, so
        # the project's record follows other's start within the margin.
        world.build(root)
        world.commit(project)
        started = at_the_project_once(start_other)
        a_tree_fails_at_the_project()
        (other,) = started
    world.play(other)

    again = [
        c.folder for c in world.compiled[after_failure["compiled"] :] if c.build.project == project
    ]
    assert other.state == ("passed" if WHEN[when] else "failed")
    assert again == ([failed] if WHEN[when] else [])


@pytest.mark.parametrize("name", ["master", TAG])
def test_a_pin_places_its_project_for_the_whole_tree(worlds, lower_project, shape, name):
    """A literal git ref in one dependent's template pins a project: the builds of the tree
    build it once, at the tip of that ref, also under a root build at a tag that every
    project carries, and every download of it in the tree, on every platform and of every
    step, takes what that build made. Every started build runs under the name of the ref the
    plan takes it from."""
    graph, project, root = lower_project.graph, lower_project.project, lower_project.root
    world = worlds(graph, *shape)
    pinned = world.commit(project, branch="3.14.x")
    dependent = first_dependent(lower_project)
    world.projects[dependent].pins[project] = "refs/heads/3.14.x"
    world.commit(dependent)
    if name == TAG:
        for p in sorted(graph):
            world.tag(p, TAG)

    root_build = world.build(root, name)

    (built,) = world.builds_of(project)
    plan = world.plan(root_build)
    names = {b.project: b.branch for b in root_build.tree()[1:]}
    assert root_build.state == "passed"
    assert built.commit == pinned
    assert {d.made_by for d in world.downloads if d.project == project} == {built.id}
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
        promoted = dataclasses.replace(made, promoted=Promoted(at, name, build))
        write_manifest(world.store, location, promoted)
        world.store.put(layout.latest_file("", "tools", ref, f), key.encode(), "")


# Which steps of tools a root build's latest successful download names: none, so it takes
# every step of the newest key, or every one by name; and which of them tools' second build
# promotes: every step, or the first alone, so that two steps of a platform come from two
# builds.
TAKEN = ("every-step", "named")

# The name a root build builds under, and the branch whose latest files of tools its plan step
# reads: a branch tools lacks, whose walk goes on to master; or a tag every project carries,
# whose read takes that name's branch alone, although master names another key of tools.
READ_UNDER = {"feature": MASTER, TAG: branch_ref(TAG)}


@pytest.mark.parametrize("taken, name", paired(TAKEN, sorted(READ_UNDER)), ids=str)
def test_a_root_build_with_a_latest_successful_dependency_is_reused_while_its_latest_files_stay(
    worlds, graph_root, shape, taken, name
):
    """A root build that takes a project as latest successful reads, in its plan step, the
    key of each step it takes through that project's latest files: under a branch the project
    lacks, those of master; under a tag, those of the entry's own branch and of no other.
    Without a step, of every step whose latest file names the newest key; of each step it
    names, the key that step's own latest file names. It writes them into each of those
    downloads and into its key.json, and its plan step's second run and its jobs take them,
    although the latest files move while the builds of its tree run. Built again after they
    moved, it compiles under a key that names the keys they name now; built once more, it is
    reused. Its snapshot dependencies are built once."""
    graph, root = graph_root.graph, graph_root.project
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
            world.hosting.tag(p, TAG, world.hosting.tip(p))
    else:
        world.hosting.cut(root, name)
    world.at_plan_step = lambda build: tools_made(world, moves, 8, READ_UNDER[name])

    earlier = world.build(root, name)
    world.at_plan_step = lambda build: None
    moved, again = world.build(root, name), world.build(root, name)

    read = {
        earlier: {f: TOOLS_KEYS[7] for f in every},
        moved: {f: TOOLS_KEYS[8 if f in moves else 7] for f in every},
    }
    keys = [world.plan(b).projects[root].key for b in (earlier, moved, again)]
    deps = [json.loads(world.store.objects[layout.key_json("", root, k)])["deps"] for k in keys]
    written = {
        (b.id, tuple(sorted(e["keys"].items())))
        for b in read
        for e in entries_of(world.agents.uploads[b.id][-1], "tools")
    }
    tools = {(d.build.id, d.folder, d.key) for d in world.downloads if d.project == "tools"}
    own = {(c.build.id, c.folder) for c in world.compiled if c.build.project == root}
    assert [b.state for b in (earlier, moved, again)] == ["passed"] * 3
    assert keys[0] != keys[1] == keys[2]
    assert [{k for p, k in d if p == "tools"} for d in deps] == [
        set(read[b].values()) for b in (earlier, moved, moved)
    ]
    assert written == {
        (b.id, tuple(sorted({f.step: k for f, k in r.items()}.items()))) for b, r in read.items()
    }
    assert own == {(b.id, f) for b in read for f in every}
    assert tools == {(b.id, f, k) for b, r in read.items() for f, k in r.items()}
    assert all(len(world.builds_of(p)) == 1 for p in below(graph, root))


# The branches at which every step of a project's pipeline also downloads its own project, as
# a compatibility test does, besides the build's own branch; and the branch at which the first
# build's project pins a dependency, a download of another project at another branch.
ELSEWHERE = ("3.14.x", "runner")
PINNED_AT = "3.13.x"


def test_the_plan_steps_page_names_each_download_of_its_own_project_at_another_branch(
    worlds, graph_project, shape
):
    """When every step of a project's pipeline also downloads its own project at other
    branches, and the root build's project pins a dependency to yet another branch, the page
    of the plan step of the project's build, the root build or one started for a dependency
    alike, names each download of its own project at another branch, with the step, its
    platform and that branch, on a line of its own, and no other download."""
    graph, project, root = graph_project.graph, graph_project.project, graph_project.root
    matrix, step_ids = shape
    world = worlds(graph, matrix, step_ids)
    world.build(root)
    world.projects[project].elsewhere = tuple((step_ids[0], branch_ref(b)) for b in ELSEWHERE)
    world.commit(project)
    pinned = graph[root][0]
    world.commit(pinned, branch=PINNED_AT)
    world.projects[root].pins[pinned] = branch_ref(PINNED_AT)
    world.commit(root)

    root_build = world.build(root)

    shown = next(b for b in root_build.tree() if b.project == project)
    _, page = world.agents.annotations[shown.id]["snapshot-plan"]
    expected = {(f"{s}-{v}", v, b) for v in matrix for s in step_ids for b in ELSEWHERE}
    naming = [line for line in page.splitlines() if any(k in line for k, _, _ in expected)]
    assert root_build.state == "passed"
    assert len(naming) == len(expected)
    assert all(any(all(x in line for x in e) for line in naming) for e in expected)


def cannot_give_a_snapshot(world, at, condition: str) -> str:
    """Make the project ``at`` names, or for a latest successful download the root build's
    project, unable to give what the plan needs by ``condition``; the name the root build
    builds under."""
    project = at.project
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


@pytest.mark.parametrize("lower_project, condition", paired(LOWER_PROJECTS, CONDITIONS), ids=str)
def test_a_dependency_that_cannot_give_a_snapshot_stops_the_plan(worlds, lower_project, condition):
    """A dependency that cannot give what the plan needs fails the root build before anything
    is triggered: no plan, no trigger, no compile, nothing stored. A snapshot dependency
    cannot when Buildkite's configuration does not run its pipeline.py, that file lacks the
    runner's call, it takes a project as latest successful, or under a root build at a tag
    its repository lacks the tag; a latest successful one, under a root build at a tag, when
    the latest files of the tag's name name no key of it, although master's do."""
    world = worlds(lower_project.graph)
    name = cannot_give_a_snapshot(world, lower_project, condition)
    stored = dict(world.store.objects)

    root_build = world.build(lower_project.root, name)

    assert root_build.state == "failed"
    assert (root_build.children, world.compiled, world.store.objects) == ([], [], stored)
    assert "snapshot-plan" not in world.agents.meta_data[root_build.id]


# What SNAPSHOT_REBUILD builds again of a tree: the root build's project, or every project.
REBUILT = {"1": lambda graph, root: {root}, "all": tree}


@pytest.mark.parametrize("rebuild", sorted(REBUILT))
def test_snapshot_rebuild_builds_again_and_keeps_what_was_made(worlds, graph_root, shape, rebuild):
    """SNAPSHOT_REBUILD=1 compiles the root build's project again although its key is made,
    and no dependency; SNAPSHOT_REBUILD=all compiles every project of the tree again. Either
    leaves what the earlier build made as it was, and the latest files of every project it did
    not build again; the next build at the branch's tip, which is reused, points the latest
    files of the root build's project back at the earlier key."""
    graph, root = graph_root.graph, graph_root.project
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
    """After a root build, the key.json of the key the plan names for any project of its
    tree, read down through the keys it names, gives the commit of every project of that
    project's tree; every key is its commit, a dash and the first 32 hex digits of the SHA-256
    of its key.json, which is canonical JSON with its deps sorted."""
    graph, root = graph_root.graph, graph_root.project
    world = worlds(graph)

    root_build = world.build(root)

    plan = world.plan(root_build)
    stored = {}

    def read_down(project: str, key: str) -> dict[str, str]:
        """The commit of ``project`` and of every project below it, read from key.json down."""
        stored[key] = world.store.objects[layout.key_json("", project, key)]
        found = json.loads(stored[key])
        commits = {project: found["commit"]}
        for d, k in found["deps"]:
            commits.update(read_down(d, k))
        return commits

    assert root_build.state == "passed"
    for p in tree(graph, root):
        key = plan.projects[p].key
        assert read_down(p, key) == {q: plan.projects[q].commit for q in tree(graph, p)}
    for key, body in stored.items():
        found = json.loads(body)
        assert key == f"{found['commit']}-{hashlib.sha256(body).hexdigest()[:32]}"
        assert body == canonical(found)
        assert found["deps"] == sorted(found["deps"])


# The root build's story branch; a branch that a pin names and that is neither a release line
# nor the root build's, as a test runner's branch is; a release line; and a tag that a pin
# names besides the root build's.
X = "feature"
PIN_BRANCH = "pinned"
LINE = "3.14.x"
PIN_TAG = "v0.9.0"


def branch_off(world, projects, branch: str, line: str = "master") -> None:
    """Give each of ``projects`` the branch ``branch`` on a commit of its own after the newest
    commit of ``line``, the line the branch left."""
    for p in sorted(projects):
        world.hosting.cut(p, branch, line)
        world.commit(p, branch=branch)


def tag_each(world, projects, tag: str) -> None:
    """Put ``tag`` on a commit of its own in each of ``projects``."""
    for p in sorted(projects):
        world.tag(p, tag)


def pin_dependencies(world, at, project: str, ref: str) -> None:
    """Pin every dependency of ``project`` to ``ref`` in its templates, on a commit of master."""
    world.projects[project].pins = {d: ref for d in at.graph[project]}
    world.commit(project)


def project_and_above(at) -> frozenset[str]:
    """The project of ``at`` and every project of its tree above it, the root build's too."""
    return ({at.project} | above(at.graph, at.project)) & tree(at.graph, at.root)


# Each story puts names and pins in the repositories before the root build, a build of
# ``at.first`` under the name the story returns; "the project" is ``at.project``. Every
# repository has master, and each story makes a world that no other story makes.


def x_only_in_the_root_build(world, at) -> str:
    """X in the root build's repository alone."""
    branch_off(world, {at.root}, X)
    return X


def x_in_the_root_build_and_below_the_project(world, at) -> str:
    """X in the root build's repository and in those below the project, which lacks it."""
    branch_off(world, below(at.graph, at.project) | {at.root}, X)
    return X


def x_in_the_project_and_above(world, at) -> str:
    """X in the project and in every repository above it; those below it lack X."""
    branch_off(world, project_and_above(at), X)
    return X


def x_in_every_project(world, at) -> str:
    """X in every repository."""
    branch_off(world, tree(at.graph, at.root), X)
    return X


def the_project_pins_its_dependencies_to_a_branch(world, at) -> str:
    """The project's templates pin each of its dependencies to a branch of its own, neither a
    release line nor X; X in the root build's repository alone."""
    pin_dependencies(world, at, at.project, branch_ref(PIN_BRANCH))
    branch_off(world, at.graph[at.project], PIN_BRANCH)
    branch_off(world, {at.root}, X)
    return X


def the_project_pins_its_dependencies_to_a_release_line(world, at) -> str:
    """The project's templates pin each of its dependencies to 3.14.x, on a commit of its
    own; X in the root build's repository alone."""
    pin_dependencies(world, at, at.project, branch_ref(LINE))
    branch_off(world, at.graph[at.project], LINE)
    branch_off(world, {at.root}, X)
    return X


def the_root_build_pins_its_dependencies_to_x(world, at) -> str:
    """X in every repository, and the root build's templates pin each of its dependencies to
    it."""
    pin_dependencies(world, at, at.root, branch_ref(X))
    branch_off(world, tree(at.graph, at.root), X)
    return X


def x_in_a_repository_without_the_root_builds_line(world, at) -> str:
    """The root build's X left 3.14.x; the project's X left master, in a repository without
    3.14.x; the repositories below it have 3.14.x and lack X."""
    branch_off(world, {at.project}, X)
    branch_off(world, below(at.graph, at.project) | {at.root}, LINE)
    branch_off(world, {at.root}, X, LINE)
    return X


def x_left_another_line_in_the_project(world, at) -> str:
    """The root build's X left master; the project's X left 3.14.x, which the repositories
    below it have too, and they lack X."""
    branch_off(world, {at.root}, X)
    branch_off(world, below(at.graph, at.project) | {at.project}, LINE)
    branch_off(world, {at.project}, X, LINE)
    return X


def x_at_masters_commits_below_the_project(world, at) -> str:
    """X in the root build's repository, and cut at master's newest commit in those below the
    project: a build that lacks X takes them from master, at the same commits."""
    branch_off(world, {at.root}, X)
    for p in sorted(below(at.graph, at.project)):
        world.hosting.cut(p, X)
    return X


def a_root_build_on_a_release_line(world, at) -> str:
    """3.14.x in the project and in every repository above it."""
    branch_off(world, project_and_above(at), LINE)
    return LINE


def a_root_build_at_a_tag(world, at) -> str:
    """The tag in every repository, and the project's templates pin each of its dependencies to
    another tag, on a commit of its own."""
    pin_dependencies(world, at, at.project, tag_ref(PIN_TAG))
    tag_each(world, at.graph[at.project], PIN_TAG)
    tag_each(world, tree(at.graph, at.root), TAG)
    return TAG


STORIES = {
    story.__name__.replace("_", "-"): story
    for story in (
        x_only_in_the_root_build,
        x_in_the_root_build_and_below_the_project,
        x_in_the_project_and_above,
        x_in_every_project,
        the_project_pins_its_dependencies_to_a_branch,
        the_project_pins_its_dependencies_to_a_release_line,
        the_root_build_pins_its_dependencies_to_x,
        x_in_a_repository_without_the_root_builds_line,
        x_left_another_line_in_the_project,
        x_at_masters_commits_below_the_project,
        a_root_build_on_a_release_line,
        a_root_build_at_a_tag,
    )
}

# Whether the root build is played once, or first with the step of the project's last step id
# on its last platform failing and then again, so that the project's build finds its other
# steps made.
PLAYED = ("once", "after-a-failed-step")

# Every project of a graph whose root build starts a build that has a dependency of its own:
# where none has, no own build takes a project elsewhere, and only a rule that never writes
# can fail.
PROJECTS_WHOSE_ROOT_BUILD_STARTS_A_DEPENDENT = [
    a for a in GRAPH_PROJECTS if any(a.graph[d] for d in below(a.graph, a.root))
]

# How the own build of a build started for a dependency takes the projects below it, against
# the plan: each at the same commit, one at another, or it cannot be planned.
SAME, OTHER, NO_PLAN = "same", "other", "no-plan"


@dataclasses.dataclass(frozen=True)
class Own:
    """The own build of a build started for a dependency: how it takes the projects below it;
    every ``(repository, ref)`` of another repository than its own whose answer it needed
    (``answering``); and whether the repository of a dependency lacked its branch, so that its
    release line counted."""

    takes: str
    asked: frozenset
    used_line: bool


def asked_refs(questions) -> frozenset[tuple[str, str]]:
    """Every ``(repository, ref)`` of ``questions``, as ``FakeGit.asked`` holds them."""
    return frozenset((r, ref) for r, refs in questions for ref in refs)


def answering(hosting, repository: str, refs: tuple) -> tuple:
    """The refs of a question to ``repository`` up to the first one it has, the answer: a ref
    after it changes nothing, though a plan that knows nothing asks it in the same question."""
    held = [i for i, ref in enumerate(refs) if (repository, ref) in hosting.refs]
    return refs[: held[0] + 1] if held else refs


def own_build(world, graph: dict, plan, build) -> Own:
    """The own build of ``build``: a root build of its project at its name and commit, played
    now, against ``plan``."""
    before = len(world.git.asked)
    own = world.build(build.project, build.branch, build.commit)
    needed = [(r, answering(world.hosting, r, refs)) for r, refs in world.git.asked[before:]]
    others = [(r, refs) for r, refs in needed if r != url(build.project)]
    asked = asked_refs(others)
    # Only a dependency without a pin is asked for more than one ref, its branch first, each in
    # a question of its own.
    used_line = any(n > 1 for n in collections.Counter(r for r, _ in asked).values())
    if "snapshot-plan" not in world.agents.meta_data[own.id]:
        return Own(NO_PLAN, asked, used_line)
    projects = world.plan(own).projects
    same = all(
        m in projects and projects[m].commit == plan.projects[m].commit
        for m in below(graph, build.project)
    )
    return Own(SAME if same else OTHER, asked, used_line)


def line_of(world, project: str, planned) -> str | None:
    """The release line of a build of ``project`` at the ref and commit ``planned`` gives it, as
    its plan step finds it; none at a tag."""
    branch = branch_of(planned.ref)
    if branch is None:
        return None
    return release_line(world.git_at(planned.commit), url(project), branch)


def assumed_line(world, plan, told, root: str, p: str) -> str | None:
    """The release line the plan takes the own build of ``p`` to have: its branch when that is
    a release line; on the root build's branch, the root build's line, when git told the plan
    that the repository of ``p`` has a branch of that name; else none, at a tag or not known."""
    at, root_at = plan.projects[p], plan.projects[root]
    branch = branch_of(at.ref)
    if branch is None or is_release_line(branch):
        return branch
    if at.ref != root_at.ref:
        return None
    line = line_of(world, root, root_at)
    known = (url(p), branch_ref(line)) in told & world.hosting.refs.keys()
    return line if known else None


@pytest.mark.parametrize("graph_project", PROJECTS_WHOSE_ROOT_BUILD_STARTS_A_DEPENDENT, ids=str)
@pytest.mark.parametrize(
    "story, shaped, played", paired(sorted(STORIES), sorted(SHAPES), PLAYED), ids=str
)
def test_a_started_build_writes_its_branchs_latest_files_only_as_its_own_build_would(
    worlds, graph_project, story, shaped, played
):
    """Every build started for a dependency runs under the name of its ref, and leaves that
    branch's latest files, on every platform and of every step, through its promotes and
    through its plan step for the steps it finds made, or none. Its own build is a root build
    of its project at its ref and commit, played after, over a git that knows which release
    line each branch left.

    A build that leaves them has an own build that takes every project below it at the commit
    the plan takes it at, but in the one case the README names among its limits, where one
    whose own build takes a project elsewhere leaves them too: on the root build's branch, in
    a repository that has a branch of the root build's release line, which the plan then takes
    as its line, where its branch left another line. A build that leaves none has an own build
    that takes a project below it at another commit, or that cannot be planned, or whose
    answers need what git did not tell the plan: a ref of another repository, or, for a
    dependency whose repository lacks its branch, a release line other than the one the plan
    takes it to have."""
    graph, project, root = graph_project.graph, graph_project.project, graph_project.root
    matrix, step_ids = SHAPES[shaped]
    world = worlds(graph, matrix, step_ids)
    name = STORIES[story](world, graph_project)
    if played == "after-a-failed-step":
        world.broken.add((project, Folder(matrix[-1], step_ids[-1])))
        world.build(root, name)
        world.broken.clear()
    asked = len(world.git.asked)

    root_build = world.build(root, name)

    plan, every = world.plan(root_build), folders(matrix, step_ids)
    told = asked_refs(world.git.asked[asked:])
    started = [b for b in world.builds if "SNAPSHOT_PLAN" in b.env]
    last = {b.project: b for b in started}
    left = {p: world.objects(f"{p}/refs/") for p in last}
    own = {p: own_build(world, graph, plan, b) for p, b in last.items()}
    found = {p: line_of(world, p, plan.projects[p]) for p in last}
    assumed = {p: assumed_line(world, plan, told, root, p) for p in last}

    def latest_files(p: str) -> dict[str, bytes]:
        ref, key = branch_ref(name_of(plan.projects[p].ref)), plan.projects[p].key
        return {layout.latest_file("", p, ref, f): key.encode() for f in every}

    writes = {p for p in last if left[p]}
    wrong = {p for p in writes if own[p].takes != SAME}
    late = {p for p in last.keys() - writes if own[p].takes == SAME}
    accepted = {p for p in last if assumed[p] is not None and found[p] != assumed[p]}
    line_untold = {p for p in last if own[p].used_line and found[p] != assumed[p]}
    untold = {p for p in last if own[p].asked - told} | line_untold
    assert root_build.state == "passed"
    assert [b.branch for b in started] == [name_of(plan.projects[b.project].ref) for b in started]
    assert left == {p: latest_files(p) if p in writes else {} for p in last}
    assert wrong == {p for p in accepted if own[p].takes != SAME}
    assert late <= untold


# Where a name is put on the commits the builds of a tree built: a branch cut in every project
# of the tree, or in the project of the failed step and the projects above it, whose own
# builds take the projects below from master, at the same commits; or a tag put on every
# project of the tree, which, once, moves off the project's commit while the project's build
# waits at its plan step, and is put back after the builds of the tree.
PUT_ON = {
    "every-project": ("feature", False),
    "the-project-and-above": ("feature", False),
    "a-tag-on-every-project": (TAG, False),
    "a-tag-that-moves-while-the-project-waits": (TAG, True),
}


@pytest.mark.parametrize("put_on", sorted(PUT_ON))
def test_a_build_at_its_names_commit_points_its_latest_files_at_its_key(
    worlds, graph_project, put_on
):
    """After the builds of a tree in which one step of one platform of a project failed, a
    branch is cut, or a tag put, at the same commits, and the root build under that name
    builds that step again. Every build of that tree that runs under the name and writes its
    branch's latest files leaves the name's latest file of every step it promoted naming its
    key, and of every step it found made when git names its commit for the name as it starts,
    and not when the name moved off its commit while it waited; a project the tree's builds did
    not build leaves none. Once the name has moved on to a newer commit of the root build's
    project and built it, a build of the older commit under the name, which is reused, leaves
    the latest files of the root build's project naming the newer key."""
    graph, project, root = graph_project.graph, graph_project.project, graph_project.root
    matrix, step_ids = MATRICES["three"], STEP_IDS["build-notes"]
    world = worlds(graph, matrix, step_ids)
    failed = Folder(matrix[-1], step_ids[-1])
    world.broken.add((project, failed))
    world.build(root)
    world.broken.clear()
    projects, (name, moves) = tree(graph, root), PUT_ON[put_on]
    the_project_and_above = ({project} | above(graph, project)) & projects
    for p in the_project_and_above if put_on == "the-project-and-above" else projects:
        if name == TAG:
            world.hosting.tag(p, TAG, world.hosting.tip(p))
        else:
            world.hosting.cut(p, name)
    if moves:
        world.at_plan_step = lambda b: world.tag(project, TAG) if b.project == project else None

    root_build = world.build(root, branch=name)
    world.at_plan_step = lambda b: None
    if moves:
        world.hosting.tag(project, TAG, world.hosting.tip(project))
    plan, every = world.plan(root_build), folders(matrix, step_ids)
    named = {p: {f: world.latest(p, branch_ref(name), f) for f in every} for p in projects}
    if name == TAG:
        world.tag(root, TAG)
    else:
        world.commit(root, branch=name)
    newer = world.build(root, branch=name)
    older = world.play(world.start(root, name, root_build.commit, {}))

    built = {b.project: b.branch for b in root_build.tree()}
    writes = {p for p in built if built[p] == name and plan.projects[p].writes_latest_file}
    waited = {project} - {root} if moves else set()
    expected = {
        p: {
            f: plan.projects[p].key if p in writes and (p not in waited or f == failed) else None
            for f in every
        }
        for p in projects
    }
    kept = {f: world.latest(root, branch_ref(name), f) for f in every}
    assert (root_build.state, newer.state, older.state) == ("passed", "passed", "passed")
    assert project in built
    assert named == expected
    assert (older.children, world.agents.uploads[older.id]) == ([], [])
    assert kept == {f: world.plan(newer).projects[root].key for f in every}
