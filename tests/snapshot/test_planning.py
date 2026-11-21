"""The plan's inputs and outputs, without builds: the key of a snapshot, the declaration
a template makes, and the rewrite that writes one build into every snapshot download.

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
    above,
    agent_view,
    build_step,
    entry,
    generated,
    roots,
)
from world import GIT_IDENTITY, git

from lib.pointer import Build, Pointer
from lib.snapshot.declaration import (
    LATEST_SUCCESSFUL,
    SNAPSHOT,
    pipeline_downloads,
    read_declaration,
    rule_violations,
)
from lib.snapshot.document import Rewrite, load_document, needed_variants, planned
from lib.snapshot.git import RealGit, run_command
from lib.snapshot.lines import release_line
from lib.snapshot.planner import Root, make_plan

MASTER = "refs/heads/master"
KEY = "0" * 40 + "-" + "0" * 32


# ---------------------------------------------------------------------------
# The key
# ---------------------------------------------------------------------------


def keys(world, number: int) -> dict[str, set[str]]:
    """The keys each project has in the plans of every root of the world, made by root
    builds numbered ``number``."""
    found: dict[str, set[str]] = {}
    for r in roots({p: n.deps for p, n in world.projects.items()}):
        commit = world.hosting.head(r)
        root = Root(r, "master", commit, f"01890000-0000-7000-8000-{number:012d}", number)
        declaration = read_declaration(world.hosting.trees[commit], r)
        for p, node in make_plan(root, declaration, world.git, world.pipelines).nodes.items():
            found.setdefault(p, set()).add(node.key)
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


# ---------------------------------------------------------------------------
# Templates: generated ones, and the five real pipelines
# ---------------------------------------------------------------------------

TYPES = ("snapshot", "pinned", "latest")
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


@dataclasses.dataclass(frozen=True)
class Template:
    """A committed template of ``own``, the ref a build of it generates for, the entries
    ``(project, type, pin)`` its declaration must hold, and the keys of the steps that name the
    plugin another way, which the rule reports in the pipeline generated from it."""

    name: str
    own: str
    text: str
    own_ref: str
    expected: frozenset
    misnamed: tuple = ()

    def __str__(self) -> str:
        return self.name


def generated_template(at, spelling: str, kind: str, misnamed: bool) -> Template:
    """A node's template whose download entries are of ``kind``, its keys spelled ``spelling``;
    when ``misnamed``, its macos step names the plugin ``MISNAMED``."""
    deps = at.graph[at.node]
    ref = PIN if kind == "pinned" else "{git_ref}"
    steps = [
        build_step(
            at.node,
            v,
            [entry(d, v, ref, kind == "latest", spelling) for d in deps],
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
    return Template(name, at.node, text, MASTER, frozenset(expected), reported)


def real_template(own: str) -> Template:
    """A real generated pipeline, committed as a template: its literal refs are pins."""
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
    pin, in either spelling; and the pipeline generated from it keeps the rule, but in each step
    that names the plugin another way."""
    declaration = read_declaration({".buildkite/steps.yml": template.text}, template.own)
    doc = load_document(generated(template.text, template.own_ref))

    entries = {(e.project, e.type, e.pin) for e in declaration.entries}
    violations = rule_violations(
        pipeline_downloads(doc), declaration, template.own, template.own_ref
    )
    assert (entries, [v.step for v in violations]) == (template.expected, list(template.misnamed))


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


def held_by(project: str, variant: str, number: int) -> Pointer:
    build = Build(f"01890000-0000-7000-9000-{number:012d}", number, f"https://ci.example/{number}")
    return Pointer(
        project,
        "c" * 40 + "-" + "1" * 32,
        variant,
        MASTER,
        "c" * 40,
        build,
        "2026-11-19T00:00:00.000Z",
    )


def test_the_rewrite_names_the_planned_build_in_every_snapshot_download(template):
    """The pipeline the plan step uploads names, in every snapshot download, the build, ref and
    strict matching of the pointer of its project and variant; a latest successful download is
    left as generated; every upload and promote carries the key; every step runs the plugin at
    the plan step's commit."""
    doc = load_document(generated(template.text, template.own_ref))
    needs = needed_variants(doc, template.own, {})
    wanted = sorted((p, v) for p in needs for v in needs[p])
    held = {(p, v): held_by(p, v, n) for n, (p, v) in enumerate(wanted)}

    out = planned(doc, Rewrite(template.own, KEY, PLUGIN_COMMIT, held, {}))

    blocks = (options_of(doc), options_of(out))
    assert len(blocks[0]) == len(blocks[1]) > 0
    for (_, before), (plugin, after) in zip(*blocks):
        view = agent_view(after)
        assert plugin == f"{PLUGIN}#{PLUGIN_COMMIT}"
        assert all(
            view[block]["SNAPSHOT"] == KEY for block in ("UPLOAD", "PROMOTE") if block in view
        )
        downloads = (
            [agent_view(e) for e in before.get("download", {}).get("projects", [])],
            view.get("DOWNLOAD", {}).get("PROJECTS", []),
        )
        assert len(downloads[0]) == len(downloads[1])
        for generated_entry, e in zip(*downloads):
            if generated_entry.get("PROJECT_ID", template.own) == template.own:
                continue
            if "BUILD_ID" in generated_entry:
                assert generated_entry == e
                continue
            p = held[(generated_entry["PROJECT_ID"], generated_entry["VARIANT"])]
            assert (e["BUILD_ID"], e["GIT_REF"], e["REQUIRE_STRICT_REF_MATCH"]) == (
                p.build.id,
                p.git_ref,
                True,
            )


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
