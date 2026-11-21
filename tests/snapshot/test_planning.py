"""The plan's inputs and outputs, without builds: the key, the declaration a template makes,
the step ids the plan carries, and the rewrite that writes the keys of every step a download
of another project takes.

Graphs and templates come from ``chain.py``; the five sanitised real pipelines of
``fixtures/`` are read as their repositories would commit them.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml
from chain import (
    GRAPH_NODES,
    PLUGIN,
    PLUGIN_COMMIT,
    REAL,
    STEP_IDS,
    above,
    agent_view,
    build_step,
    entry,
    generated,
    roots,
    tree,
)
from world import GIT_IDENTITY, git

from lib.snapshot.declaration import (
    LATEST_SUCCESSFUL,
    SNAPSHOT,
    pipeline_downloads,
    read_declaration,
    rule_violations,
)
from lib.snapshot.document import (
    Rewrite,
    Taken,
    latest_dependencies,
    latest_download,
    load_document,
    planned,
)
from lib.snapshot.git import RealGit, run_command
from lib.snapshot.lines import release_line
from lib.snapshot.planner import Root, make_plan

MASTER = "refs/heads/master"
KEY = "0" * 40 + "-" + "0" * 32


# ---------------------------------------------------------------------------
# The key, and the step ids the plan carries
# ---------------------------------------------------------------------------


def plans(world, number: int) -> list:
    """The plans of every root of the world, made by root builds numbered ``number``."""
    found = []
    for r in roots({p: n.deps for p, n in world.projects.items()}):
        commit = world.hosting.head(r)
        root = Root(r, "master", commit, f"01890000-0000-7000-8000-{number:012d}", number)
        declaration = read_declaration(world.hosting.trees[commit], r)
        found.append(make_plan(root, declaration, (), world.git, world.pipelines))
    return found


def keys(world, number: int) -> dict[str, set[str]]:
    """The keys each project has in the plans of every root of the world."""
    found: dict[str, set[str]] = {}
    for plan in plans(world, number):
        for p, planned_project in plan.projects.items():
            found.setdefault(p, set()).add(planned_project.key)
    return found


def test_a_new_commit_changes_the_keys_of_its_node_and_every_node_above(worlds, graph_node):
    """A new commit of a node changes the key of that node and of every node above it, and of
    no other; the same commits give the same keys, in another build and through another root."""
    graph, node = graph_node.graph, graph_node.node
    world = worlds(graph)

    before, again = keys(world, 1), keys(world, 2)
    world.commit(node)
    after = keys(world, 3)

    assert before == again
    assert all(len(k) == 1 for k in (*before.values(), *after.values()))
    assert {p for p in graph if before[p] != after[p]} == {node} | above(graph, node)


def test_the_plan_carries_the_step_ids_of_every_project(worlds, graph_root):
    """The plan names, for every project of a root's tree, the step ids its templates
    promote at the commit the plan takes it from."""
    graph, root = graph_root.graph, graph_root.node
    world = worlds(graph)
    shapes = sorted(STEP_IDS.values())
    steps = {p: shapes[i % len(shapes)] for i, p in enumerate(sorted(graph))}
    for p in graph:
        world.projects[p].steps = steps[p]
        world.commit(p)

    found = [plan for plan in plans(world, 1) if plan.first.project == root]

    (plan,) = found
    assert {p: n.steps for p, n in plan.projects.items()} == {
        p: steps[p] for p in tree(graph, root)
    }


# ---------------------------------------------------------------------------
# Templates: generated ones, and the five real pipelines
# ---------------------------------------------------------------------------

TYPES = ("snapshot", "pinned", "latest", "named")
PIN = "refs/heads/3.14.x"
FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE_FILES = {
    "qdb-api-go": "qdb-api-go.yml",
    "qdb-api-python": "qdb-api-python.yml",
    "qdb-api-rest": "qdb-api-rest.yml",
    "qdb-duck": "qdb-duck.yml",
    "quasardb-build": "quasardb.yml",
}


# The plugin's name in another case: the agent runs the plugin for it, the reader reads only
# the plugin's own name, so the rule refuses it in a generated pipeline.
MISNAMED = PLUGIN.capitalize()

# The step an entry of kind "named" takes.
NAMED_STEP = "notes"


@dataclasses.dataclass(frozen=True)
class Template:
    """A committed template of ``own``, the ref a build of it generates for, the entries
    ``(project, type, pin)`` its declaration must hold, the step ids it must give, and the
    keys of the steps that name the plugin another way, which the rule reports in the
    pipeline generated from it."""

    name: str
    own: str
    text: str
    own_ref: str
    expected: frozenset
    steps: tuple = ()
    misnamed: tuple = ()

    def __str__(self) -> str:
        return self.name


def generated_template(at, spelling: str, kind: str, misnamed: bool) -> Template:
    """A node's template whose download entries are of ``kind``, its keys spelled ``spelling``;
    when ``misnamed``, its macos step names the plugin ``MISNAMED``."""
    deps = at.graph[at.node]
    ref = PIN if kind == "pinned" else "{git_ref}"
    named = NAMED_STEP if kind == "named" else None
    steps = [
        build_step(
            at.node,
            v,
            [entry(d, v, ref, kind == "latest", spelling, named) for d in deps],
            spelling,
            plugin=MISNAMED if misnamed and v == "macos" else PLUGIN,
        )
        for v in ("linux", "macos")
    ]
    if kind == "latest":
        expected = {(d, LATEST_SUCCESSFUL, None) for d in deps}
    else:
        expected = {(d, SNAPSHOT, PIN if kind == "pinned" else None) for d in deps}
    name = f"{at}-{spelling}-{kind}" + ("-misnamed" if misnamed else "")
    text = yaml.safe_dump({"steps": steps})
    reported = ("build-macos",) if misnamed else ()
    return Template(name, at.node, text, MASTER, frozenset(expected), ("build",), reported)


def real_template(own: str) -> Template:
    """A real generated pipeline, committed as a template: its literal refs are pins, and it
    writes the plugin's step and no step_id, so it gives no step id."""
    text = (FIXTURES / FIXTURE_FILES[own]).read_text()
    feature = "refs/heads/feature"
    return Template(
        f"real-{own}", own, text, feature, frozenset((d, SNAPSHOT, feature) for d in REAL[own])
    )


# Each type of entry, and the snapshot type with one step that names the plugin another way.
KINDS = [*((kind, False) for kind in TYPES), ("snapshot", True)]

TEMPLATES = [
    *(
        generated_template(at, spelling, kind, misnamed)
        for at in GRAPH_NODES
        if at.graph[at.node]
        for spelling in ("snake", "kebab")
        for kind, misnamed in KINDS
    ),
    *(real_template(own) for own in sorted(REAL)),
]


@pytest.fixture(params=TEMPLATES, ids=str)
def template(request) -> Template:
    """Templates of every node that has dependencies, in both spellings of option keys and
    with each type of entry, also with a step that names the plugin another way; and the five
    real pipelines."""
    return request.param


def test_the_declaration_holds_every_download_of_another_project(template):
    """Reading a template gives every other project it downloads, with the entry's type and
    pin, in either spelling, and the step ids its steps that promote write; and the pipeline
    generated from it keeps the rule, but in each step that names the plugin another way."""
    declaration = read_declaration({".buildkite/steps.yml": template.text}, template.own)
    doc = load_document(generated(template.text, template.own_ref))

    entries = {(e.project, e.type, e.pin) for e in declaration.entries}
    violations = rule_violations(
        pipeline_downloads(doc), declaration, template.own, template.own_ref
    )
    assert (entries, declaration.steps) == (template.expected, template.steps)
    assert [v.step for v in violations] == list(template.misnamed)


# ---------------------------------------------------------------------------
# The rewrite
# ---------------------------------------------------------------------------


def leaf_steps(doc: dict) -> list[dict]:
    return [s for top in doc["steps"] for s in (top.get("steps", []) if "group" in top else [top])]


def options_of(doc: dict) -> list[tuple[str, dict]]:
    """``(plugin key, options)`` of every step's artifacts plugin, in document order."""
    return [
        (name, options)
        for s in leaf_steps(doc)
        for plugin in s.get("plugins", [])
        for name, options in plugin.items()
        if name.split("#", 1)[0] == PLUGIN
    ]


# The step ids of every dependency in the rewrite.
DEPENDENCY_STEPS = ("build", "notes")


def taken_of(projects) -> dict[str, Taken]:
    """A key of its own for each dependency, and its step ids."""
    return {
        p: Taken(f"{n:040x}-{n:032x}", DEPENDENCY_STEPS) for n, p in enumerate(sorted(projects), 1)
    }


def read_of(doc, own: str) -> dict:
    """A key of its own for each step each latest successful download of ``doc`` takes: each
    step it names, else each of ``DEPENDENCY_STEPS``, as the plan step reads them."""
    downloads = dict.fromkeys(d for d, _ in latest_dependencies(doc, own))
    return {
        d: {s: f"{n:040x}-{m:032x}" for m, s in enumerate(d.step or DEPENDENCY_STEPS, 1)}
        for n, d in enumerate(downloads, 100)
    }


def test_the_rewrite_writes_the_key_of_every_step_a_download_of_another_project_takes(template):
    """The pipeline the plan step uploads gives every download of another project the key of
    each step it takes, the one it names or every step: of a snapshot download its project's
    planned key, of a latest successful download the key read for that step; and nothing that
    would find a build; every configuration of the plugin carries the build's key; every step
    runs the plugin at the plan step's commit."""
    doc = load_document(generated(template.text, template.own_ref))
    taken = taken_of(p for p, t, _ in template.expected if t == SNAPSHOT)
    read = read_of(doc, template.own)

    out = planned(doc, Rewrite(template.own, KEY, PLUGIN_COMMIT, taken, read, {}))

    blocks = (options_of(doc), options_of(out))
    found = ("GIT_REF", "STEP", "REQUIRE_STRICT_REF_MATCH", "LATEST")
    assert len(blocks[0]) == len(blocks[1]) > 0
    for (_, before), (plugin, after) in zip(*blocks):
        view = agent_view(after)
        assert plugin == f"{PLUGIN}#{PLUGIN_COMMIT}"
        assert view["KEY"] == KEY
        entries = before.get("download", {}).get("projects", [])
        downloads = ([agent_view(e) for e in entries], view.get("DOWNLOAD", {}).get("PROJECTS", []))
        assert len(downloads[0]) == len(downloads[1])
        for written, generated_entry, e in zip(entries, *downloads):
            project = generated_entry.get("PROJECT_ID", template.own)
            if project == template.own:
                continue
            named = generated_entry.get("STEP")
            steps = [named] if isinstance(named, str) else named or DEPENDENCY_STEPS
            if generated_entry.get("LATEST"):
                keys = read[latest_download(written, project)]
            else:
                keys = {s: taken[project].key for s in steps}
            assert e["KEYS"] == {s.upper(): keys[s] for s in steps}
            assert e["VARIANT"] == generated_entry["VARIANT"]
            assert not set(found) & set(e)


# ---------------------------------------------------------------------------
# The release line, asked of git itself
# ---------------------------------------------------------------------------

LEFT = ("master", "3.14.x")


def as_on_an_agent(args, cwd, stdin, env):
    """``run_command`` without the configuration of the host."""
    return run_command(args, cwd, stdin, {**env, **GIT_IDENTITY})


def lined_repository(directory: Path, commit) -> None:
    """Make ``directory`` a repository with three release lines: ``master``, ``3.14.x`` that
    left it, and ``3.8.x`` that began from another root commit. ``commit(repository, time)``
    commits on the branch checked out."""
    git(directory, "init", "-q")
    git(directory, "symbolic-ref", "HEAD", "refs/heads/master")
    commit(directory, 1000)
    git(directory, "branch", "3.14.x")
    commit(directory, 3000)
    git(directory, "checkout", "-q", "3.14.x")
    commit(directory, 2000)
    git(directory, "checkout", "-q", "--orphan", "3.8.x")
    commit(directory, 500)


@pytest.mark.parametrize("left", LEFT)
def test_a_branch_belongs_to_the_release_line_it_left_last(tmp_path, monkeypatch, left):
    """The release line of a branch that is not one is the line it left last, in a repository
    that also has a release line with which the branch shares no history."""

    def commit(repository: Path, time: int) -> None:
        monkeypatch.setenv("GIT_COMMITTER_DATE", f"@{time} +0000")
        git(repository, "commit", "-q", "--allow-empty", "-m", f"at {time}")

    origin, checkout = tmp_path / "origin", tmp_path / "checkout"
    origin.mkdir()
    lined_repository(origin, commit)
    git(tmp_path, "clone", "-q", str(origin), str(checkout))
    git(checkout, "checkout", "-q", "-b", "feature", f"origin/{left}")
    commit(checkout, 4000)

    asked = RealGit(str(checkout), run=as_on_an_agent)

    assert release_line(asked, str(origin), "feature") == left
