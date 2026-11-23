"""The plan's inputs and outputs, without builds: the key, the declaration a template makes,
the step ids the plan carries, and the rewrite that writes the keys of every step a download
of another project takes.

Graphs and templates come from ``builds.py``; the five sanitised real pipelines of
``fixtures/`` are read as their repositories would commit them.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest
import yaml
from builds import (
    GRAPH_PROJECTS,
    PLUGIN,
    PLUGIN_COMMIT,
    REAL,
    STEP_IDS,
    above,
    build_step,
    entry,
    generated,
    tops,
    tree,
)
from fakes import url
from world import GIT_IDENTITY, git

from lib.options import Options, options_of
from lib.snapshot.git import RealGit, run_command
from lib.snapshot.latest import latest_download
from lib.snapshot.lines import release_line
from lib.snapshot.pipeline import dependencies, entries_of, steps_of
from lib.snapshot.plan import Planned
from lib.snapshot.planner import RootBuild, make_plan, name_refs
from lib.snapshot.rewrite import Rewrite, planned
from lib.snapshot.rule import rule_violations
from lib.snapshot.templates import kind, read_declaration

MASTER = "refs/heads/master"
KEY = "0" * 40 + "-" + "0" * 32
# The two kinds of dependency, as a declaration names them.
SNAPSHOT, LATEST_SUCCESSFUL = "snapshot", "latest successful"


# ---------------------------------------------------------------------------
# The key, and the step ids the plan carries
# ---------------------------------------------------------------------------


def plans(world, number: int) -> list:
    """The plans of every top of the world, made by root builds numbered ``number``."""
    found = []
    for t in tops({p: project.deps for p, project in world.projects.items()}):
        commit = world.hosting.tip(t)
        refs = world.git.commits_of(url(t), name_refs("master"))
        root = RootBuild(
            t, "master", commit, refs, f"01890000-0000-7000-8000-{number:012d}", number
        )
        declaration = read_declaration(world.hosting.trees[commit], t)
        found.append(make_plan(root, declaration, (), world.git, world.pipelines))
    return found


def keys(world, number: int) -> dict[str, set[str]]:
    """The keys each project has in the plans of every top of the world."""
    found: dict[str, set[str]] = {}
    for plan in plans(world, number):
        for p, planned_project in plan.projects.items():
            found.setdefault(p, set()).add(planned_project.key)
    return found


def test_a_new_commit_changes_the_keys_of_its_project_and_every_project_above(
    worlds, graph_project
):
    """A new commit of a project changes the key of that project and of every project above
    it, and of no other; the same commits give the same keys, in another build and through
    another top."""
    graph, project = graph_project.graph, graph_project.project
    world = worlds(graph)

    before, again = keys(world, 1), keys(world, 2)
    world.commit(project)
    after = keys(world, 3)

    assert before == again
    assert all(len(k) == 1 for k in (*before.values(), *after.values()))
    assert {p for p in graph if before[p] != after[p]} == {project} | above(graph, project)


def test_the_plan_carries_the_step_ids_of_every_project(worlds, graph_root):
    """The plan names, for every project of a root build's tree, the step ids its templates
    promote at the commit the plan takes it from."""
    graph, root = graph_root.graph, graph_root.project
    world = worlds(graph)
    shapes = sorted(STEP_IDS.values())
    steps = {p: shapes[i % len(shapes)] for i, p in enumerate(sorted(graph))}
    for p in graph:
        world.projects[p].steps = steps[p]
        world.commit(p)

    found = [plan for plan in plans(world, 1) if plan.root.project == root]

    (plan,) = found
    assert {p: planned_project.steps for p, planned_project in plan.projects.items()} == {
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


# The plugin's name in another case: the agent runs the plugin for it, so the plan step reads
# and rewrites its configuration as under the plugin's own name.
MISNAMED = PLUGIN.capitalize()

# The step an entry of kind "named" takes.
NAMED_STEP = "notes"


@dataclasses.dataclass(frozen=True)
class Template:
    """A committed template of ``own``, the ref a build of it generates for, the entries
    ``(project, type, pin)`` its declaration must hold, and the step ids it must give."""

    name: str
    own: str
    text: str
    own_ref: str
    expected: frozenset
    steps: tuple = ()

    def __str__(self) -> str:
        return self.name


def generated_template(at, spelling: str, kind: str, misnamed: bool) -> Template:
    """A project's template whose download entries are of ``kind``, its keys spelled
    ``spelling``; when ``misnamed``, its macos step names the plugin ``MISNAMED``."""
    deps = at.graph[at.project]
    ref = PIN if kind == "pinned" else "{git_ref}"
    named = NAMED_STEP if kind == "named" else None
    steps = [
        build_step(
            at.project,
            v,
            [entry(d, ref, kind == "latest", spelling, named) for d in deps],
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
    return Template(name, at.project, text, MASTER, frozenset(expected), ("build",))


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
        for at in GRAPH_PROJECTS
        if at.graph[at.project]
        for spelling in ("snake", "kebab")
        for kind, misnamed in KINDS
    ),
    *(real_template(own) for own in sorted(REAL)),
]


@pytest.fixture(params=TEMPLATES, ids=str)
def template(request) -> Template:
    """Templates of every project that has dependencies, in both spellings of option keys and
    with each type of entry, also with a step that names the plugin another way; and the five
    real pipelines."""
    return request.param


def test_the_declaration_holds_every_download_of_another_project(template):
    """Reading a template gives every other project it downloads, with the entry's type and
    pin, in either spelling and under either name of the plugin, and the step ids its steps
    that promote write; and the pipeline generated from it keeps the rule."""
    declaration = read_declaration({".buildkite/steps.yml": template.text}, template.own)
    doc = yaml.safe_load(generated(template.text, template.own_ref))

    entries = {(e.project, kind(e.latest), e.pin) for e in declaration.entries}
    violations = rule_violations(steps_of(doc), declaration, template.own, template.own_ref)
    assert (entries, declaration.steps) == (template.expected, template.steps)
    assert violations == []


# ---------------------------------------------------------------------------
# The rewrite
# ---------------------------------------------------------------------------


def leaf_steps(doc: dict) -> list[dict]:
    return [s for top in doc["steps"] for s in (top.get("steps", []) if "group" in top else [top])]


def plugin_options(doc: dict) -> list[tuple[str, Options]]:
    """``(plugin key, options)`` of every step's artifacts plugin, under either of its names,
    in document order, as a job reads them."""
    return [
        (name, options_of(options))
        for s in leaf_steps(doc)
        for plugin in s.get("plugins", [])
        for name, options in plugin.items()
        if name.split("#", 1)[0] in (PLUGIN, MISNAMED)
    ]


# The step ids of every dependency in the rewrite.
DEPENDENCY_STEPS = ("build", "notes")


def taken_of(projects) -> dict[str, Planned]:
    """Each dependency as a plan holds it, with a key of its own and its step ids."""
    return {
        p: Planned(MASTER, f"{n:040x}", f"{n:040x}-{n:032x}", True, steps=DEPENDENCY_STEPS)
        for n, p in enumerate(sorted(projects), 1)
    }


def read_of(doc, own: str) -> dict:
    """A key of its own for each step each latest successful download of ``doc`` takes: each
    step it names, else each of ``DEPENDENCY_STEPS``, as the plan step reads them."""
    latest = dependencies(steps_of(doc), own, latest=True)
    downloads = dict.fromkeys(latest_download(e, project) for _, project, e in latest)
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
    doc = yaml.safe_load(generated(template.text, template.own_ref))
    taken = taken_of(p for p, t, _ in template.expected if t == SNAPSHOT)
    read = read_of(doc, template.own)

    rewrite = Rewrite(template.own, KEY, PLUGIN_COMMIT, taken, read, {})
    out = planned(doc, steps_of(doc), rewrite)

    blocks = (plugin_options(doc), plugin_options(out))
    assert len(blocks[0]) == len(blocks[1]) > 0
    for (_, before), (plugin, after) in zip(*blocks):
        assert plugin == f"{PLUGIN}#{PLUGIN_COMMIT}"
        assert after.key == KEY
        downloads = (entries_of(before), entries_of(after))
        assert len(downloads[0]) == len(downloads[1])
        for generated_entry, e in zip(*downloads):
            project = generated_entry.project_id or template.own
            if project == template.own:
                continue
            steps = generated_entry.step or DEPENDENCY_STEPS
            if generated_entry.latest:
                keys = read[latest_download(generated_entry, project)]
            else:
                keys = {s: taken[project].key for s in steps}
            assert e.keys == {s: keys[s] for s in steps}
            assert e.variant == generated_entry.variant
            # Nothing that would find a build is left.
            assert (e.git_ref, e.step, e.require_strict_ref_match, e.latest) == (
                None,
                (),
                False,
                False,
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
