"""The planner on graphs that mirror the real one.

qdb-api-python -> qdb-api-rest -> qdb-duck -> quasardb-build
qdb-api-python -> quasardb-build
qdb-api-rest   -> quasardb-build

Each project has a repository of its own (``plans.URLS``) unless a test says
otherwise. A world is the refs of each repository, the tree at each ref (a
step template that downloads from the project's dependencies, and a
``pipeline.py`` with or without the runner's shape) and Buildkite's answer per
pipeline.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import pytest
import yaml
from fakes import FakeGit, FakePipelines
from plans import DUCK, EDGES, PLAN_ID, PYTHON, QDB, REST, T0, URLS
from repos import T, clone, commit, git, new_repository, served, write

from lib.snapshot.buildkite import BuildkiteError, PipelineFacts
from lib.snapshot.canonical import snapshot_key
from lib.snapshot.declaration import FILE_LIMIT, read_declaration
from lib.snapshot.git import GitError, RealGit
from lib.snapshot.plan import (
    REBUILD_ALL,
    REBUILD_NONE,
    REBUILD_ONE,
    Plan,
    PlanError,
    decode_plan,
    encode_plan,
)
from lib.snapshot.planner import DIRECTORY, PIPELINE_PY, Root, counts, make_plan, rebuild_setting


def sha(*parts: str) -> str:
    return hashlib.sha1("/".join(parts).encode()).hexdigest()


def full(name: str) -> str:
    """A ref as the tests write it: a full ref as it is, any other name a branch."""
    return name if name.startswith("refs/") else "refs/heads/" + name


ADOPTED = (
    "from qdb_pipeline import run\n"
    "\n\n"
    "def generate_pipeline():\n"
    '    return {"steps": []}\n'
    "\n\n"
    'if __name__ == "__main__":\n'
    "    run(generate_pipeline)\n"
)
NOT_ADOPTED = (
    "from qdb_pipeline import validate_pipeline\n"
    "\n\n"
    "def main():\n"
    "    validate_pipeline({})\n"
    "\n\n"
    'if __name__ == "__main__":\n'
    "    main()\n"
)
UNPARSABLE = "steps:\n  - bureau14/qdb-artifacts#master: {download: [\n"
BUILD = ".buildkite/steps/_build.yml"


def template(deps, pins=None, latest=()) -> str:
    """A step template whose artifacts plugin block downloads from ``deps``.

    ``pins`` add git refs; the projects of ``latest`` are downloaded as latest
    successful dependencies.
    """
    pins = pins or {}
    entries = [{"project_id": d, **({"git_ref": pins[d]} if d in pins else {})} for d in deps]
    entries += [{"project_id": d, "build_id": "LATEST_SUCCESSFUL"} for d in latest]
    plugin = {"download": {"projects": entries}, "upload": {"files": ["artifacts/*"]}}
    step = {"label": "build {slug}", "plugins": [{"bureau14/qdb-artifacts#master": plugin}]}
    return yaml.safe_dump({"steps": [step]}, sort_keys=False)


def tree(deps=(), pins=None, adopted=True, latest=(), extra=None) -> dict[str, str]:
    files = {
        BUILD: template(deps, pins, latest),
        ".buildkite/pipeline.py": ADOPTED if adopted else NOT_ADOPTED,
        "README.md": "not under .buildkite\n",
    }
    return {**files, **(extra or {})}


@dataclass
class World:
    git: FakeGit
    pipelines: FakePipelines


def world(refs, trees=None, times=None, runs=None, urls=None) -> World:
    """``refs[project]`` lists the refs of the project's repository (a bare name is a branch).

    The commit of each is ``sha(project, name)`` and carries
    ``tree(EDGES[project])`` unless ``trees`` maps ``(project, name)`` to
    another tree. ``runs[project]``: Buildkite's answer whether its
    configuration runs ``pipeline.py`` (default yes). ``times[name]``: the
    time of the merge-base of the checkout with the branch ``name``.
    """
    urls = {**URLS, **(urls or {})}
    times = times or {}
    commits, files = {}, {}
    for project, names in refs.items():
        for name in names:
            commits[(urls[project], full(name))] = sha(project, name)
            files[(urls[project], sha(project, name))] = (trees or {}).get(
                (project, name), tree(EDGES.get(project, ()))
            )
    by_commit = {sha(p, n): times[n] for p, names in refs.items() for n in names if n in times}
    facts = {p: PipelineFacts(p, u, (runs or {}).get(p, True)) for p, u in urls.items()}
    return World(FakeGit(commits, files, times=by_commit), FakePipelines(facts))


def root(branch: str, project: str = PYTHON, rebuild: str = REBUILD_NONE) -> Root:
    return Root(
        project=project,
        branch=branch,
        commit=sha(project, branch),
        build_id=PLAN_ID,
        build_number=512,
        build_url="https://ci.example/qdb-api-python/512",
        rebuild=rebuild,
    )


def plan_of(w: World, branch="master", project=PYTHON, rebuild=REBUILD_NONE) -> Plan:
    """The plan of a root build of ``project`` at the head of ``branch``, from its own tree."""
    r = root(branch, project, rebuild)
    own = read_declaration(w.git.trees[(URLS[project], r.commit)], project)
    return make_plan(r, own, w.git, w.pipelines, T0)


def refused(w: World, branch="master") -> str:
    """The message of a plan that fails."""
    with pytest.raises(PlanError) as e:
        plan_of(w, branch)
    return str(e.value)


def refs(p: Plan) -> dict[str, str]:
    return {k: n.ref for k, n in p.nodes.items()}


def labels(p: Plan) -> dict[str, str]:
    return {k: n.label for k, n in p.nodes.items()}


def calls(w: World, method: str) -> list[tuple[str, ...]]:
    return [c for c in w.git.calls if c[0] == method]


def heads(**branches: str) -> dict[str, str]:
    """``{project: refs/heads/<branch>}`` from ``python=``, ``rest=``, ``duck=``, ``qdb=``."""
    names = {"python": PYTHON, "rest": REST, "duck": DUCK, "qdb": QDB}
    return {names[k]: full(v) for k, v in branches.items()}


ALL_MASTER = {PYTHON: ["master"], REST: ["master"], DUCK: ["master"], QDB: ["master"]}


def way_out(consumer: str, project: str) -> str:
    """The way out every refusal of a snapshot dependency ends with."""
    return (
        f"or {consumer} takes {project} as a latest successful dependency, with build_id: "
        f"LATEST_SUCCESSFUL in its entry of {project} in {BUILD}."
    )


# ---------------------------------------------------------------------------
# Scenarios: a chain, a diamond, branches and lines, labels
# ---------------------------------------------------------------------------


def test_all_on_master_keys_bottom_up():
    p = plan_of(world(ALL_MASTER))
    k_qdb = snapshot_key(QDB, sha(QDB, "master"), {})
    k_duck = snapshot_key(DUCK, sha(DUCK, "master"), {QDB: k_qdb})
    k_rest = snapshot_key(REST, sha(REST, "master"), {DUCK: k_duck, QDB: k_qdb})
    k_py = snapshot_key(PYTHON, sha(PYTHON, "master"), {REST: k_rest, QDB: k_qdb})
    assert {k: n.key for k, n in p.nodes.items()} == {
        QDB: k_qdb,
        DUCK: k_duck,
        REST: k_rest,
        PYTHON: k_py,
    }
    assert list(p.nodes) == [QDB, DUCK, REST, PYTHON], "dependencies first"
    assert set(refs(p).values()) == {"refs/heads/master"}
    assert set(labels(p).values()) == {"master"}
    assert p.nodes[PYTHON].deps == (REST, QDB)
    assert p.nodes[QDB].deps == ()
    assert p.nodes[REST].repository == URLS[REST]
    assert p.nodes[REST].commit == sha(REST, "master")
    assert (p.id, p.context, p.rebuild, p.root.build_number) == (PLAN_ID, "master", "none", 512)
    assert p.created_at == "2025-11-17T12:00:00.000Z"


def test_each_question_is_asked_once():
    w = world(ALL_MASTER)
    plan_of(w)
    assert sorted(calls(w, "commits_of")) == [
        ("commits_of", URLS[p], ("refs/heads/master", "refs/heads/main"))
        for p in sorted((REST, DUCK, QDB), key=URLS.get)
    ], "one per repository; the root's repository is the root's checkout"
    reads = calls(w, "read_files")
    assert len(reads) == len(set(reads)) == 3, "one per repository and commit, the root's none"
    assert sorted(w.pipelines.calls) == sorted({PYTHON, REST, DUCK, QDB}), "one per slug"
    assert calls(w, "branches") == calls(w, "merge_base_times") == [], "master is its own line"


def test_feature_branch_in_some_repositories_only():
    branches = {
        PYTHON: ["master", "feature"],
        REST: ["master"],
        DUCK: ["master"],
        QDB: ["master", "feature"],
    }
    p = plan_of(world(branches, times={"master": 200}), "feature")
    assert refs(p) == heads(python="feature", rest="master", duck="master", qdb="feature")
    assert p.nodes[QDB].commit == sha(QDB, "feature")
    assert labels(p) == {
        PYTHON: "feature",
        REST: "snapshot/feature",
        DUCK: "snapshot/feature",
        QDB: "feature",
    }, "a node on master over quasardb-build on the feature branch is not master's build"
    assert p.context == "feature"


def test_feature_branch_only_in_the_middle_keeps_line_labels_below():
    branches = {
        PYTHON: ["master", "feature"],
        REST: ["master", "feature"],
        DUCK: ["master"],
        QDB: ["master"],
    }
    p = plan_of(world(branches, times={"master": 200}), "feature")
    assert refs(p) == heads(python="feature", rest="feature", duck="master", qdb="master")
    assert labels(p) == {
        PYTHON: "feature",
        REST: "snapshot/feature",
        DUCK: "master",
        QDB: "master",
    }, "the plan does not know qdb-api-rest's own line, from which its own build takes the two"


def test_branch_cut_from_3_14_x_falls_back_to_3_14_x():
    branches = {
        PYTHON: ["master", "3.14.x", "fix"],
        REST: ["master", "3.14.x"],
        DUCK: ["master", "3.14.x"],
        QDB: ["master", "3.14.x"],
    }
    w = world(branches, times={"master": 100, "3.14.x": 200})
    p = plan_of(w, "fix")
    assert refs(p) == heads(python="fix", rest="3.14.x", duck="3.14.x", qdb="3.14.x")
    assert labels(p) == {PYTHON: "fix", REST: "3.14.x", DUCK: "3.14.x", QDB: "3.14.x"}
    lines = (sha(PYTHON, "3.14.x"), sha(PYTHON, "master"))
    assert calls(w, "branches") == [("branches", URLS[PYTHON])]
    assert calls(w, "merge_base_times") == [
        ("merge_base_times", URLS[PYTHON], tuple(sorted(lines)))
    ]


@pytest.mark.parametrize(
    "root_tree",
    [tree(()), tree((QDB,), pins={QDB: "refs/heads/master"}), tree((), latest=(QDB,))],
    ids=["no dependency", "its dependency pinned", "a latest successful dependency only"],
)
def test_the_release_line_is_found_only_for_a_dependency_without_a_pin(root_tree):
    # Finding the line costs a listing of the root's repository and a fetch
    # of every line's tip into the checkout.
    w = world({PYTHON: ["master", "fix"], QDB: ["master"]}, trees={(PYTHON, "fix"): root_tree})
    plan_of(w, "fix")
    assert calls(w, "branches") == calls(w, "merge_base_times") == []


def test_each_repository_is_asked_for_all_its_candidate_refs_in_one_ls_remote():
    branches = {
        PYTHON: ["master", "3.14.x", "fix"],
        REST: ["master", "3.14.x"],
        DUCK: ["master"],
        QDB: ["master", "fix"],
    }
    w = world(branches, times={"master": 100, "3.14.x": 200})
    p = plan_of(w, "fix")
    assert refs(p) == heads(python="fix", rest="3.14.x", duck="master", qdb="fix")
    candidates = ("refs/heads/fix", "refs/heads/3.14.x", "refs/heads/master", "refs/heads/main")
    assert sorted(calls(w, "commits_of")) == [
        ("commits_of", URLS[p], candidates) for p in sorted((REST, DUCK, QDB), key=URLS.get)
    ]


@pytest.mark.parametrize(
    "qdb_branches, taken", [(["main"], "main"), (["main", "master"], "master")]
)
def test_the_default_ref_ends_with_master_then_main(qdb_branches, taken):
    branches = {**ALL_MASTER, PYTHON: ["master", "fix"], QDB: qdb_branches}
    w = world(branches, times={"master": 1})
    assert refs(plan_of(w, "fix"))[QDB] == "refs/heads/" + taken
    assert [c for c in calls(w, "commits_of") if c[1] == URLS[QDB]] == [
        ("commits_of", URLS[QDB], ("refs/heads/fix", "refs/heads/master", "refs/heads/main"))
    ], "asked in the same ls-remote as the root's branch and line"


def test_line_missing_in_a_repository_falls_back_to_master():
    branches = {PYTHON: ["3.14.x"], REST: ["3.14.x"], DUCK: ["master"], QDB: ["3.14.x"]}
    p = plan_of(world(branches), "3.14.x")
    assert refs(p) == heads(python="3.14.x", rest="3.14.x", duck="master", qdb="3.14.x")
    assert labels(p) == {
        PYTHON: "3.14.x",
        REST: "3.14.x",
        DUCK: "snapshot/3.14.x",
        QDB: "3.14.x",
    }, "qdb-duck on master over quasardb-build on 3.14.x is not master's build"


def test_a_dependency_whose_repository_has_none_of_the_refs_fails():
    branches = {**ALL_MASTER, PYTHON: ["master", "feature"], DUCK: ["develop"]}
    w = world(branches, times={"master": 1})
    assert refused(w, "feature") == (
        f"qdb-duck cannot be planned: {URLS[DUCK]} has none of refs/heads/feature, "
        f"refs/heads/master, refs/heads/main. It is a snapshot dependency of qdb-api-rest "
        f"({BUILD})."
    )


def test_cycle_back_to_the_root_is_an_error():
    trees = {(QDB, "master"): tree([PYTHON])}
    assert refused(world(ALL_MASTER, trees=trees)) == (
        "dependency cycle: qdb-api-python -> qdb-api-rest -> qdb-duck -> quasardb-build"
        " -> qdb-api-python"
    )


def test_a_cycle_below_the_root_is_an_error():
    trees = {(QDB, "master"): tree([DUCK])}
    message = refused(world(ALL_MASTER, trees=trees))
    assert message.startswith("dependency cycle: ")
    assert "qdb-duck -> quasardb-build -> qdb-duck" in message


def test_another_pipeline_of_the_root_repository_is_planned_at_the_root_commit():
    tests = "quasardb-api-tests"
    checkout = "f" * 40
    both = tree(extra={".buildkite/steps/_tests.yml": template([QDB])})
    w = world({QDB: ["master"]}, urls={tests: URLS[QDB]})
    w.git.trees[(URLS[QDB], checkout)] = both
    r = Root(tests, "master", checkout, PLAN_ID, 1, "https://ci.example/quasardb-api-tests/1")
    p = make_plan(r, read_declaration(both, tests), w.git, w.pipelines, T0)
    assert p.nodes[QDB].commit == checkout, "not the remote head of master"
    assert p.nodes[tests].deps == (QDB,)
    assert p.nodes[QDB].deps == (), "its own project's entries are not dependencies"
    assert calls(w, "read_files") == [("read_files", URLS[QDB], checkout)]
    assert calls(w, "commits_of") == [], "the root's repository and branch are known"


def test_the_roots_own_templates_not_parsing_fail_the_plan():
    w = world(ALL_MASTER)
    own = read_declaration(
        tree(EDGES[PYTHON], extra={".buildkite/steps/_x.yml": UNPARSABLE}), PYTHON
    )
    with pytest.raises(PlanError) as e:
        make_plan(root("master"), own, w.git, w.pipelines, T0)
    assert str(e.value).startswith(
        f"the templates of qdb-api-python at {sha(PYTHON, 'master')} do not parse by the "
        "reading rule: .buildkite/steps/_x.yml: not valid YAML"
    )
    assert w.git.calls == [] and w.pipelines.calls == [], "nothing is asked"


@pytest.mark.parametrize("pinned", [True, False], ids=["its dependency pinned", "not pinned"])
@pytest.mark.parametrize("branch", ["a\nb", "a..b", "HEAD", "-x", "x/"])
def test_a_root_on_a_branch_name_git_refuses_fails_before_anything_is_asked(branch, pinned):
    pins = {QDB: "refs/heads/master"} if pinned else None
    w = world({PYTHON: ["master"], QDB: ["master"]})
    w.git.trees[(URLS[PYTHON], sha(PYTHON, branch))] = tree((QDB,), pins)
    with pytest.raises(PlanError) as e:
        plan_of(w, branch)
    assert str(e.value) == (
        f"qdb-api-python cannot be planned on the branch {branch!r}: git accepts no branch of "
        "that name"
    )
    assert w.git.calls == [] and w.pipelines.calls == []


@pytest.mark.parametrize("pinned", [True, False], ids=["its dependency pinned", "not pinned"])
@pytest.mark.parametrize(
    "branch", ["x/${BUILDKITE_AGENT_ACCESS_TOKEN}", "fix+1", "caf" + chr(0xE9)]
)
def test_a_root_on_any_branch_git_accepts_is_planned(branch, pinned):
    pins = {QDB: "refs/heads/master"} if pinned else None
    branches = {PYTHON: ["master", branch], QDB: ["master"]}
    w = world(branches, trees={(PYTHON, branch): tree((QDB,), pins)}, times={"master": 1})
    p = plan_of(w, branch)
    assert p.context == branch and p.nodes[PYTHON].ref == "refs/heads/" + branch
    assert p.nodes[QDB].ref == "refs/heads/master"
    assert decode_plan(encode_plan(p)) == p


def test_a_branch_git_accepts_is_shown_in_a_message_as_ascii():
    branch = "caf" + chr(0xE9)
    w = world({PYTHON: ["master", branch], QDB: ["develop"]}, times={"master": 1})
    w.git.trees[(URLS[PYTHON], sha(PYTHON, branch))] = tree((QDB,))
    message = refused(w, branch)
    assert "has none of refs/heads/caf\\xe9, refs/heads/master" in message
    message.encode("ascii")


LONG_PATH = ".buildkite/caf" + chr(0xE9) + "-" + "x" * 2000 + ".yml"


def ascii_and_short(message: str) -> bool:
    """Plain ASCII, and no line longer than a few foreign values shown short."""
    return message.isascii() and max(len(line) for line in message.splitlines()) < 1000


@pytest.mark.parametrize(
    "trees",
    [
        {(DUCK, "master"): tree(EDGES[DUCK], extra={LONG_PATH: UNPARSABLE})},
        {
            (REST, "master"): tree((), extra={LONG_PATH: template(EDGES[REST])}),
            (DUCK, "master"): tree((), adopted=False),
        },
        {
            (PYTHON, "master"): tree(
                (), extra={LONG_PATH: template((REST, DUCK), {DUCK: "refs/heads/a"})}
            ),
            (REST, "master"): tree(EDGES[REST], pins={DUCK: "refs/heads/b"}),
        },
        {(PYTHON, "master"): tree(EDGES[PYTHON], extra={LONG_PATH: UNPARSABLE})},
    ],
    ids=["a template that does not parse", "the template of an entry", "a pin", "the root's own"],
)
def test_a_template_path_is_shown_short_in_ascii_in_a_message(trees):
    # Paths are those of another repository's tree: any length, any letter.
    message = refused(world({**ALL_MASTER, DUCK: ["master", "a", "b"]}, trees=trees))
    assert "caf\\xe9-xxx" in message
    assert ascii_and_short(message), message[:300]


def test_rebuild_settings():
    w = world(ALL_MASTER)
    none = plan_of(w)
    one = plan_of(w, rebuild=REBUILD_ONE)
    every = plan_of(w, rebuild=REBUILD_ALL)
    assert (none.rebuild, one.rebuild, every.rebuild) == ("none", "one", "all")
    assert {p: one.nodes[p].key for p in (QDB, DUCK, REST)} == {
        p: none.nodes[p].key for p in (QDB, DUCK, REST)
    }, "under 1 only the root is forced"
    deps = {REST: none.nodes[REST].key, QDB: none.nodes[QDB].key}
    assert one.nodes[PYTHON].key == snapshot_key(PYTHON, sha(PYTHON, "master"), deps, PLAN_ID)
    k_qdb = snapshot_key(QDB, sha(QDB, "master"), {}, PLAN_ID)
    assert every.nodes[QDB].key == k_qdb
    assert every.nodes[DUCK].key == snapshot_key(DUCK, sha(DUCK, "master"), {QDB: k_qdb}, PLAN_ID)
    assert all(every.nodes[p].key != none.nodes[p].key for p in EDGES)


@pytest.mark.parametrize("value, setting", [(None, "none"), ("1", "one"), ("all", "all")])
def test_rebuild_setting(value, setting):
    assert rebuild_setting(value) == setting


@pytest.mark.parametrize("value", ["", "0", "ALL", " all", "1 ", "true", "one"])
def test_only_the_two_values_of_the_specification_are_rebuild_settings(value):
    with pytest.raises(PlanError, match=f"SNAPSHOT_REBUILD={value!r}: expected 1 or all"):
        rebuild_setting(value)


def test_a_rebuild_setting_that_means_nothing_fails():
    with pytest.raises(PlanError, match="expected 1 or all"):
        rebuild_setting("yes")
    with pytest.raises(PlanError) as e:
        rebuild_setting("x" * 100_000 + "\n")
    assert len(str(e.value)) < 300 and "\n" not in str(e.value)
    w = world(ALL_MASTER)
    own = read_declaration({}, PYTHON)
    with pytest.raises(PlanError, match="rebuild setting"):
        make_plan(root("master", rebuild="yes"), own, w.git, w.pipelines, T0)


# ---------------------------------------------------------------------------
# A dependency that cannot give a snapshot: each condition of section 3
# ---------------------------------------------------------------------------


def duck_at_master() -> str:
    return f"refs/heads/master {sha(DUCK, 'master')} of {URLS[DUCK]}"


def test_condition_1_buildkite_does_not_run_pipeline_py():
    w = world(ALL_MASTER, runs={DUCK: False})
    assert refused(w) == (
        f"qdb-duck cannot give a snapshot at {duck_at_master()}: Buildkite's configuration of "
        "qdb-duck does not run .buildkite/pipeline.py into a pipeline upload. It is a snapshot "
        f"dependency of qdb-api-rest ({BUILD}). Way out: qdb-duck adopts: the first step of its "
        "Buildkite configuration runs .buildkite/pipeline.py | buildkite-agent pipeline upload; "
        + way_out(REST, DUCK)
    )
    assert ("read_files", URLS[DUCK], sha(DUCK, "master")) not in calls(w, "read_files")


def test_condition_2_pipeline_py_without_the_runners_shape():
    w = world(ALL_MASTER, trees={(DUCK, "master"): tree(EDGES[DUCK], adopted=False)})
    assert refused(w) == (
        f"qdb-duck cannot give a snapshot at {duck_at_master()}: .buildkite/pipeline.py does "
        "not import run from qdb_pipeline at module level without an alias. It is a snapshot "
        f"dependency of qdb-api-rest ({BUILD}). Way out: qdb-duck adopts on refs/heads/master: "
        "its .buildkite/pipeline.py imports run from qdb_pipeline and ends with "
        'if __name__ == "__main__": run(generate_pipeline); ' + way_out(REST, DUCK)
    )


def test_condition_2_no_pipeline_py():
    w = world(ALL_MASTER, trees={(DUCK, "master"): {BUILD: template(EDGES[DUCK])}})
    assert refused(w).startswith(
        f"qdb-duck cannot give a snapshot at {duck_at_master()}: .buildkite/pipeline.py is not "
        f"there. It is a snapshot dependency of qdb-api-rest ({BUILD})."
    )


def test_condition_2_a_pipeline_py_over_the_size_limit_is_not_parsed():
    # ast.parse takes about 440 times the size of the source in memory.
    big = ADOPTED + "#" + "x" * FILE_LIMIT + "\n"
    w = world(ALL_MASTER, trees={(DUCK, "master"): {**tree(EDGES[DUCK]), PIPELINE_PY: big}})
    assert refused(w).startswith(
        f"qdb-duck cannot give a snapshot at {duck_at_master()}: .buildkite/pipeline.py is "
        f"{len(big)} bytes, more than the 1048576 bytes the reader parses. It is a snapshot "
        f"dependency of qdb-api-rest ({BUILD})."
    )


def test_condition_3_a_template_that_does_not_parse():
    bad = tree(EDGES[DUCK], extra={".buildkite/steps/_bad.yml": UNPARSABLE})
    message = refused(world(ALL_MASTER, trees={(DUCK, "master"): bad}))
    assert message.startswith(
        f"qdb-duck cannot give a snapshot at {duck_at_master()}: its templates do not parse by "
        "the reading rule: .buildkite/steps/_bad.yml: not valid YAML"
    )
    assert message.endswith(
        f"It is a snapshot dependency of qdb-api-rest ({BUILD}). Way out: qdb-duck fixes that "
        "template on refs/heads/master; " + way_out(REST, DUCK)
    )


@pytest.mark.parametrize("project", ["{project}", "[x](https://evil.example)"])
def test_condition_3_a_project_id_that_is_not_a_slug(project):
    bad = tree(EDGES[DUCK], extra={".buildkite/steps/_bad.yml": template([project])})
    w = world(ALL_MASTER, trees={(DUCK, "master"): bad})
    assert (
        "its templates do not parse by the reading rule: .buildkite/steps/_bad.yml: "
        f"project id {project!r} is not a pipeline slug"
    ) in refused(w)
    assert project not in w.pipelines.calls, "never asked of Buildkite"


def test_condition_4_a_latest_successful_dependency():
    go = "qdb-api-go"
    w = world(ALL_MASTER, trees={(DUCK, "master"): tree(EDGES[DUCK], latest=(go,))})
    assert refused(w) == (
        f"qdb-duck cannot give a snapshot at {duck_at_master()}: its declaration holds a latest "
        f"successful dependency, which a snapshot never contains: {BUILD} downloads qdb-api-go "
        f"with build_id. It is a snapshot dependency of qdb-api-rest ({BUILD}). Way out: "
        "qdb-duck takes qdb-api-go as a snapshot dependency on refs/heads/master (no build_id "
        f"in its entry of qdb-api-go in {BUILD}); " + way_out(REST, DUCK)
    )
    assert go not in w.pipelines.calls, "a latest successful dependency is never asked about"


@pytest.mark.parametrize(
    "runs, duck, first",
    [
        (False, tree(adopted=False), "Buildkite's configuration of qdb-duck"),
        (
            True,
            tree(
                adopted=False,
                latest=("qdb-api-go",),
                extra={".buildkite/steps/_bad.yml": UNPARSABLE},
            ),
            ".buildkite/pipeline.py does not import",
        ),
        (
            True,
            tree(latest=("qdb-api-go",), extra={".buildkite/steps/_bad.yml": UNPARSABLE}),
            "its templates do not parse",
        ),
    ],
    ids=["1 before 2", "2 before 3 and 4", "3 before 4"],
)
def test_the_conditions_are_checked_in_order_and_the_first_that_fails_is_named(runs, duck, first):
    w = world(ALL_MASTER, trees={(DUCK, "master"): duck}, runs={DUCK: runs})
    assert refused(w).startswith(f"qdb-duck cannot give a snapshot at {duck_at_master()}: {first}")


def test_a_dependency_that_has_not_adopted_on_a_feature_branch_only():
    branches = {**ALL_MASTER, PYTHON: ["master", "feature"], DUCK: ["master", "feature"]}
    trees = {(DUCK, "feature"): tree(EDGES[DUCK], adopted=False)}
    w = world(branches, trees=trees, times={"master": 1})
    message = refused(w, "feature")
    assert message.startswith(
        f"qdb-duck cannot give a snapshot at refs/heads/feature {sha(DUCK, 'feature')} of "
        f"{URLS[DUCK]}: .buildkite/pipeline.py does not import run"
    )
    assert "qdb-duck adopts on refs/heads/feature" in message
    assert refs(plan_of(w, "master"))[DUCK] == "refs/heads/master", "adopted on master"


def test_every_refused_project_is_named_one_per_line():
    trees = {
        (DUCK, "master"): tree(EDGES[DUCK], adopted=False),
        (QDB, "master"): tree(adopted=False),
    }
    lines = refused(world(ALL_MASTER, trees=trees)).splitlines()
    assert [line.split(" ")[0] for line in lines] == [DUCK, QDB]
    assert "snapshot dependency of qdb-api-python" in lines[1], "the first consumer to reach it"


def test_a_refused_project_is_not_descended_into():
    trees = {(REST, "master"): tree(EDGES[REST], adopted=False)}
    w = world(ALL_MASTER, trees=trees)
    assert refused(w).splitlines()[0].startswith("qdb-api-rest cannot give a snapshot")
    assert DUCK not in w.pipelines.calls, "qdb-duck is reached only through qdb-api-rest"


# ---------------------------------------------------------------------------
# Two pipelines of one repository
# ---------------------------------------------------------------------------

VALIDATION = "quasardb-validation"


def two_pipelines_of_quasardb(validation_runs: bool, latest=()) -> World:
    deps = [d for d in (QDB, VALIDATION) if d not in latest]
    root_tree = tree(deps, latest=latest)
    return world(
        {PYTHON: ["master"], QDB: ["master"]},
        trees={(PYTHON, "master"): root_tree},
        urls={VALIDATION: URLS[QDB]},
        runs={VALIDATION: validation_runs},
    )


def test_of_two_pipelines_of_one_repository_the_one_buildkite_does_not_run_is_refused():
    w = two_pipelines_of_quasardb(validation_runs=False)
    assert refused(w) == (
        f"quasardb-validation cannot give a snapshot at refs/heads/master {sha(QDB, 'master')} of "
        f"{URLS[QDB]}: Buildkite's configuration of quasardb-validation does not run "
        ".buildkite/pipeline.py into a pipeline upload. It is a snapshot dependency of "
        f"qdb-api-python ({BUILD}). Way out: quasardb-validation adopts: the first step of its "
        "Buildkite configuration runs .buildkite/pipeline.py | buildkite-agent pipeline upload; "
        + way_out(PYTHON, VALIDATION)
    )
    assert calls(w, "read_files") == [("read_files", URLS[QDB], sha(QDB, "master"))], (
        "quasardb-build is read; the validation pipeline costs no read"
    )


def test_the_pipeline_buildkite_does_not_run_can_be_a_latest_successful_dependency():
    w = two_pipelines_of_quasardb(validation_runs=False, latest=(VALIDATION,))
    p = plan_of(w)
    assert set(p.nodes) == {PYTHON, QDB}
    assert p.nodes[PYTHON].key is None, "a root with a latest successful dependency"
    assert VALIDATION not in w.pipelines.calls


def test_two_pipelines_of_one_repository_that_can_give_a_snapshot_read_it_once():
    w = two_pipelines_of_quasardb(validation_runs=True)
    p = plan_of(w)
    assert p.nodes[VALIDATION].commit == p.nodes[QDB].commit
    assert len(calls(w, "read_files")) == 1
    assert calls(w, "commits_of") == [
        ("commits_of", URLS[QDB], ("refs/heads/master", "refs/heads/main"))
    ]


# ---------------------------------------------------------------------------
# Latest successful dependencies
# ---------------------------------------------------------------------------

GO = "qdb-api-go"


def test_a_root_with_a_latest_successful_dependency_is_planned_without_a_key():
    trees = {(PYTHON, "master"): tree(EDGES[PYTHON], latest=(GO,))}
    w = world(ALL_MASTER, trees=trees)
    p = plan_of(w, rebuild=REBUILD_ONE)
    assert GO not in p.nodes and GO not in w.pipelines.calls
    assert p.nodes[PYTHON].key is None
    assert p.nodes[PYTHON].deps == (REST, QDB)
    assert p.nodes[QDB].key == snapshot_key(QDB, sha(QDB, "master"), {}), (
        "its snapshot dependencies get refs, commits, keys and labels as for any root"
    )
    assert labels(p)[QDB] == "master" and refs(p)[QDB] == "refs/heads/master"
    assert decode_plan(encode_plan(p)) == p


def test_a_project_taken_as_latest_successful_by_the_root_and_as_snapshot_below():
    trees = {(PYTHON, "master"): tree((REST,), latest=(QDB,))}
    p = plan_of(world(ALL_MASTER, trees=trees))
    assert p.nodes[PYTHON].deps == (REST,), "the root's own entry of quasardb-build is not planned"
    assert QDB in p.nodes and p.nodes[DUCK].deps == (QDB,), "but qdb-duck's is"
    assert p.nodes[PYTHON].key is None
    assert p.nodes[QDB].key == snapshot_key(QDB, sha(QDB, "master"), {})


def test_a_project_taken_as_snapshot_by_the_root_and_as_latest_successful_below():
    trees = {(REST, "master"): tree((DUCK,), latest=(QDB,))}
    message = refused(world(ALL_MASTER, trees=trees))
    assert message.startswith(
        f"qdb-api-rest cannot give a snapshot at refs/heads/master {sha(REST, 'master')} of "
        f"{URLS[REST]}: its declaration holds a latest successful dependency"
    )
    assert f"{BUILD} downloads quasardb-build with build_id" in message


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------

PIN_BRANCH = "3.14.x-20990101-pinned"
RUNNER_BRANCH = "9.9.9-test-runner"
TEST_RUNNER = "refs/heads/" + RUNNER_BRANCH


def test_a_pin_of_the_root_applies_to_the_whole_chain():
    branches = {
        PYTHON: ["3.14.x", PIN_BRANCH],
        REST: ["3.14.x"],
        DUCK: ["3.14.x"],
        QDB: ["3.14.x", RUNNER_BRANCH],
    }
    root_tree = tree(EDGES[PYTHON], pins={QDB: TEST_RUNNER})
    w = world(branches, trees={(PYTHON, PIN_BRANCH): root_tree}, times={"3.14.x": 300})
    p = plan_of(w, PIN_BRANCH)
    assert refs(p) == heads(python=PIN_BRANCH, rest="3.14.x", duck="3.14.x", qdb=RUNNER_BRANCH)
    assert p.nodes[QDB].commit == sha(QDB, RUNNER_BRANCH)
    assert labels(p) == {
        PYTHON: PIN_BRANCH,
        REST: "snapshot/" + PIN_BRANCH,
        DUCK: "snapshot/" + PIN_BRANCH,
        QDB: "snapshot/" + PIN_BRANCH,
    }, "3.14.x's builds use 3.14.x's quasardb-build, not the root's pin"
    assert [c for c in calls(w, "commits_of") if c[1] == URLS[QDB]] == [
        ("commits_of", URLS[QDB], (TEST_RUNNER,))
    ], "a project with a pin is asked for its pin only"


def test_a_pin_to_a_branch():
    branches = {**ALL_MASTER, PYTHON: ["master", "feature"], DUCK: ["master", "duck-fix"]}
    root_tree = tree(EDGES[PYTHON] + (DUCK,), pins={DUCK: "refs/heads/duck-fix"})
    w = world(branches, trees={(PYTHON, "feature"): root_tree}, times={"master": 1})
    p = plan_of(w, "feature")
    duck = p.nodes[DUCK]
    assert (duck.ref, duck.commit, duck.deps) == (
        "refs/heads/duck-fix",
        sha(DUCK, "duck-fix"),
        (QDB,),
    )
    assert labels(p) == {
        PYTHON: "feature",
        REST: "snapshot/feature",
        DUCK: "snapshot/feature",
        QDB: "master",
    }
    assert [c for c in calls(w, "commits_of") if c[1] == URLS[DUCK]] == [
        ("commits_of", URLS[DUCK], ("refs/heads/duck-fix",))
    ]


def test_a_pin_to_a_tag():
    tag = "refs/tags/v3.14.3"
    branches = {**ALL_MASTER, DUCK: ["master", tag]}
    root_tree = tree(EDGES[PYTHON] + (DUCK,), pins={DUCK: tag})
    p = plan_of(world(branches, trees={(PYTHON, "master"): root_tree}))
    duck = p.nodes[DUCK]
    assert (duck.ref, duck.commit) == (tag, sha(DUCK, tag)), "the commit the tag names"
    assert duck.key == snapshot_key(DUCK, sha(DUCK, tag), {QDB: p.nodes[QDB].key})


@pytest.mark.parametrize("pin", ["refs/heads/nope", "refs/tags/nope"])
def test_a_pin_to_a_ref_that_does_not_exist_fails(pin):
    trees = {(REST, "master"): tree(EDGES[REST], pins={DUCK: pin})}
    assert refused(world(ALL_MASTER, trees=trees)) == (
        f"qdb-duck cannot be planned: its pin {pin} (in {BUILD} of qdb-api-rest at "
        f"{sha(REST, 'master')}) is not a ref of {URLS[DUCK]}."
    )


def test_a_pin_to_a_commit_that_cannot_give_a_snapshot_is_refused():
    branches = {**ALL_MASTER, QDB: ["master", RUNNER_BRANCH]}
    trees = {
        (PYTHON, "master"): tree(EDGES[PYTHON], pins={QDB: TEST_RUNNER}),
        (QDB, RUNNER_BRANCH): tree(adopted=False),
    }
    message = refused(world(branches, trees=trees))
    assert message.startswith(
        f"quasardb-build cannot give a snapshot at {TEST_RUNNER} "
        f"{sha(QDB, RUNNER_BRANCH)} of {URLS[QDB]}: .buildkite/pipeline.py does not import"
    )


def test_two_different_pins_of_one_project_fail_and_name_both():
    branches = {**ALL_MASTER, QDB: ["master", "a", "b"]}
    trees = {
        (PYTHON, "master"): tree(EDGES[PYTHON], pins={QDB: "refs/heads/a"}),
        (REST, "master"): tree(EDGES[REST], pins={QDB: "refs/heads/b"}),
    }
    assert refused(world(branches, trees=trees)) == (
        "quasardb-build has different pins in this chain: "
        f"refs/heads/a (in {BUILD} of qdb-api-python at {sha(PYTHON, 'master')}); "
        f"refs/heads/b (in {BUILD} of qdb-api-rest at {sha(REST, 'master')})"
    )


def test_a_branch_and_a_tag_of_one_project_are_two_different_pins():
    branches = {**ALL_MASTER, QDB: ["master", "refs/tags/v1"]}
    trees = {
        (PYTHON, "master"): tree(EDGES[PYTHON], pins={QDB: "refs/heads/master"}),
        (REST, "master"): tree(EDGES[REST], pins={QDB: "refs/tags/v1"}),
    }
    assert "quasardb-build has different pins" in refused(world(branches, trees=trees))


def test_one_pin_written_in_two_places_is_one_pin():
    branches = {**ALL_MASTER, QDB: ["master", "a"]}
    trees = {
        (PYTHON, "master"): tree(EDGES[PYTHON], pins={QDB: "refs/heads/a"}),
        (REST, "master"): tree(EDGES[REST], pins={QDB: "refs/heads/a"}),
    }
    assert refs(plan_of(world(branches, trees=trees)))[QDB] == "refs/heads/a"


def test_a_pin_in_a_project_that_cannot_give_a_snapshot_is_never_read():
    branches = {**ALL_MASTER, QDB: ["master", "x", "y"]}
    trees = {
        (PYTHON, "master"): tree(EDGES[PYTHON], pins={QDB: "refs/heads/x"}),
        (DUCK, "master"): tree(EDGES[DUCK], pins={QDB: "refs/heads/y"}, adopted=False),
    }
    message = refused(world(branches, trees=trees))
    assert message.startswith("qdb-duck cannot give a snapshot"), "not a conflict of two pins"


def test_a_pin_declared_below_moves_a_project_the_walk_reached_first():
    branches = {**ALL_MASTER, QDB: ["master", "qdb-fix"]}
    trees = {(REST, "master"): tree(EDGES[REST], pins={QDB: "refs/heads/qdb-fix"})}
    w = world(branches, trees=trees)
    p = plan_of(w)
    assert refs(p)[QDB] == "refs/heads/qdb-fix", (
        "qdb-api-python reaches quasardb-build before qdb-api-rest"
    )
    reads = calls(w, "read_files")
    assert len(reads) == len(set(reads)), "the second walk reads nothing twice"
    asked = [(c[1], ref) for c in calls(w, "commits_of") for ref in c[2]]
    assert len(asked) == len(set(asked)), "no repository is asked for a ref twice"


def test_a_pin_declared_below_moves_a_project_to_a_commit_that_gives_a_snapshot():
    branches = {**ALL_MASTER, QDB: ["master", "qdb-fix"]}
    trees = {
        (QDB, "master"): tree(adopted=False),
        (REST, "master"): tree(EDGES[REST], pins={QDB: "refs/heads/qdb-fix"}),
    }
    p = plan_of(world(branches, trees=trees))
    assert refs(p)[QDB] == "refs/heads/qdb-fix", "refused at master in the first walk only"


def test_a_pin_moves_its_project_to_a_commit_with_other_dependencies():
    branches = {**ALL_MASTER, DUCK: ["master", "lean"]}
    trees = {
        (PYTHON, "master"): tree((DUCK,), pins={DUCK: "refs/heads/lean"}),
        (DUCK, "lean"): tree(()),
    }
    p = plan_of(world(branches, trees=trees))
    assert set(p.nodes) == {PYTHON, DUCK}, "quasardb-build is no longer in the tree"


def test_pins_that_never_settle_fail():
    branches = {**ALL_MASTER, REST: ["master", "y"], DUCK: ["master", "x"]}
    trees = {
        (PYTHON, "master"): tree((DUCK, REST)),
        (REST, "master"): tree((DUCK,), pins={DUCK: "refs/heads/x"}),
        (REST, "y"): tree((DUCK,)),
        (DUCK, "master"): tree(()),
        (DUCK, "x"): tree((REST,), pins={REST: "refs/heads/y"}),
    }
    assert "the pins of this chain never settle" in refused(world(branches, trees=trees))


@pytest.mark.parametrize(
    "pin", ["3.14.x", "master", "refs/heads/a b", "refs/heads/-x", "refs/x", "refs/pull/1/head"]
)
def test_a_pin_that_is_not_a_full_ref_fails(pin):
    trees = {(PYTHON, "master"): tree(EDGES[PYTHON], pins={QDB: pin})}
    assert refused(world(ALL_MASTER, trees=trees)) == (
        f"quasardb-build has the pin {pin!r} in {BUILD} of qdb-api-python at "
        f"{sha(PYTHON, 'master')}: a pin is refs/heads/<branch> or refs/tags/<tag>"
    )


def test_a_pin_that_is_not_a_full_ref_is_shown_short_on_one_line_of_ascii():
    pin = "refs/heads/" + "x" * 100_000 + "\nforged: " + chr(0xE9)
    trees = {(PYTHON, "master"): tree(EDGES[PYTHON], pins={QDB: pin})}
    message = refused(world(ALL_MASTER, trees=trees))
    assert len(message) < 400 and all(" " <= c <= "~" for c in message), message[:400]


# ---------------------------------------------------------------------------
# Labels: a node runs under its own branch only as the build its own pipeline
# would make for that branch, else under snapshot/<root branch>
# ---------------------------------------------------------------------------


def labels_of(branches, trees=None, root_branch="master", times=None) -> dict[str, str]:
    w = world(branches, trees=trees, times=times or {"master": 1})
    return labels(plan_of(w, root_branch))


def test_labels_all_on_master_without_pins():
    assert labels_of(ALL_MASTER) == dict.fromkeys(EDGES, "master")


def test_labels_a_node_without_the_roots_branch_over_everything_on_master():
    branches = {**ALL_MASTER, PYTHON: ["master", "feature"]}
    assert labels_of(branches, root_branch="feature") == {
        PYTHON: "feature",
        REST: "master",
        DUCK: "master",
        QDB: "master",
    }


def test_labels_a_node_without_the_roots_branch_over_one_on_it():
    branches = {**ALL_MASTER, PYTHON: ["master", "feature"], DUCK: ["master", "feature"]}
    assert labels_of(branches, root_branch="feature") == {
        PYTHON: "feature",
        REST: "snapshot/feature",
        DUCK: "snapshot/feature",
        QDB: "master",
    }, "qdb-duck's own build of feature takes quasardb-build from its own line, unknown here"


def test_labels_a_node_on_the_roots_branch_over_dependencies_of_the_roots_line():
    # The root's line is 3.14.x; qdb-api-rest's feature branch has only master
    # as a line, so its own build of feature takes qdb-duck and quasardb-build
    # from master, and the chain takes them from 3.14.x.
    branches = {
        PYTHON: ["master", "3.14.x", "feature"],
        REST: ["master", "feature"],
        DUCK: ["master", "3.14.x"],
        QDB: ["master", "3.14.x"],
    }
    times = {"3.14.x": 200, "master": 100}
    assert labels_of(branches, root_branch="feature", times=times) == {
        PYTHON: "feature",
        REST: "snapshot/feature",
        DUCK: "3.14.x",
        QDB: "3.14.x",
    }


def test_labels_a_node_of_the_roots_repository_on_its_branch_runs_as_itself_over_its_line():
    # Its line is the root's: one repository, one branch, one commit.
    docs = "qdb-api-python-docs"
    branches = {PYTHON: ["master", "3.14.x", "feature"], QDB: ["master", "3.14.x"]}
    trees = {(PYTHON, "feature"): tree((docs, QDB))}
    times = {"3.14.x": 200, "master": 100}
    w = world(branches, trees, times=times, urls={docs: URLS[PYTHON]})
    assert labels(plan_of(w, "feature")) == {PYTHON: "feature", docs: "feature", QDB: "3.14.x"}


DUCK_FIX = "refs/heads/duck-fix"
DUCK_PINNABLE = {**ALL_MASTER, DUCK: ["master", "duck-fix"]}


def test_labels_a_root_on_master_that_pins_a_node_to_a_feature_branch():
    trees = {(PYTHON, "master"): tree(EDGES[PYTHON] + (DUCK,), pins={DUCK: DUCK_FIX})}
    assert labels_of(DUCK_PINNABLE, trees) == {
        PYTHON: "master",
        REST: "snapshot/master",
        DUCK: "snapshot/master",
        QDB: "master",
    }, "the pinned node, and qdb-api-rest between it and the root, are not master's builds"


def test_labels_a_node_that_writes_the_pin_itself_runs_as_itself():
    trees = {(REST, "master"): tree(EDGES[REST], pins={DUCK: DUCK_FIX})}
    assert labels_of(DUCK_PINNABLE, trees) == {
        PYTHON: "master",
        REST: "master",
        DUCK: "snapshot/master",
        QDB: "master",
    }


def test_labels_a_pin_written_inside_and_outside_a_nodes_tree_is_written_inside():
    trees = {
        (PYTHON, "master"): tree(EDGES[PYTHON] + (DUCK,), pins={DUCK: DUCK_FIX}),
        (REST, "master"): tree(EDGES[REST], pins={DUCK: DUCK_FIX}),
    }
    assert labels_of(DUCK_PINNABLE, trees)[REST] == "master"


def test_labels_a_pin_written_inside_a_nodes_tree_by_a_writer_that_sorts_first():
    # qdb-api-python -> qdb-api-rest -> qdb-duck, qdb-api-python -> quasardb-build
    # -> qdb-duck. qdb-api-rest writes the pin of qdb-duck inside its tree,
    # quasardb-build outside it, and qdb-api-rest sorts first.
    trees = {
        (REST, "master"): tree((DUCK,), pins={DUCK: DUCK_FIX}),
        (QDB, "master"): tree((DUCK,), pins={DUCK: DUCK_FIX}),
        (DUCK, "master"): tree(()),
        (DUCK, "duck-fix"): tree(()),
    }
    assert labels_of(DUCK_PINNABLE, trees) == {
        PYTHON: "master",
        REST: "master",
        DUCK: "snapshot/master",
        QDB: "master",
    }


def test_labels_a_pin_from_outside_below_a_node_that_reaches_it_only_through_another():
    # quasardb-build is below qdb-api-rest only through qdb-duck.
    branches = {**ALL_MASTER, QDB: ["master", "qdb-fix"]}
    trees = {
        (PYTHON, "master"): tree(EDGES[PYTHON], pins={QDB: "refs/heads/qdb-fix"}),
        (REST, "master"): tree((DUCK,)),
    }
    assert labels_of(branches, trees) == {
        PYTHON: "master",
        REST: "snapshot/master",
        DUCK: "snapshot/master",
        QDB: "snapshot/master",
    }


LINE_PINNABLE = {**ALL_MASTER, DUCK: ["master", "3.14.x"], QDB: ["master", "3.14.x"]}
DUCK_ON_LINE = tree(EDGES[PYTHON] + (DUCK,), pins={DUCK: "refs/heads/3.14.x"})


def test_labels_a_node_pinned_to_a_release_line_over_dependencies_on_master():
    assert labels_of(LINE_PINNABLE, {(PYTHON, "master"): DUCK_ON_LINE}) == {
        PYTHON: "master",
        REST: "snapshot/master",
        DUCK: "snapshot/master",
        QDB: "master",
    }, "3.14.x's own build of qdb-duck uses 3.14.x's quasardb-build"


def test_labels_a_node_pinned_to_a_release_line_that_pins_what_is_below_it_to_it():
    trees = {
        (PYTHON, "master"): DUCK_ON_LINE,
        (DUCK, "3.14.x"): tree(EDGES[DUCK], pins={QDB: "refs/heads/3.14.x"}),
    }
    assert labels_of(LINE_PINNABLE, trees) == {
        PYTHON: "master",
        REST: "snapshot/master",
        DUCK: "3.14.x",
        QDB: "3.14.x",
    }


def test_labels_a_node_pinned_to_a_release_line_that_pins_what_is_below_it_elsewhere():
    # 3.14.x's own build of qdb-duck writes the same pin.
    branches = {**ALL_MASTER, DUCK: ["master", "3.14.x"], QDB: ["master", "qdb-fix"]}
    trees = {
        (PYTHON, "master"): DUCK_ON_LINE,
        (DUCK, "3.14.x"): tree(EDGES[DUCK], pins={QDB: "refs/heads/qdb-fix"}),
    }
    assert labels_of(branches, trees) == {
        PYTHON: "master",
        REST: "snapshot/master",
        DUCK: "3.14.x",
        QDB: "snapshot/master",
    }


def test_labels_a_node_at_a_tag_never_runs_as_itself():
    tag = "refs/tags/v3.14.3"
    branches = {**ALL_MASTER, DUCK: ["master", tag]}
    by_root = {(PYTHON, "master"): tree(EDGES[PYTHON] + (DUCK,), pins={DUCK: tag})}
    by_rest = {(REST, "master"): tree(EDGES[REST], pins={DUCK: tag})}
    assert labels_of(branches, by_root) == {
        PYTHON: "master",
        REST: "snapshot/master",
        DUCK: "snapshot/master",
        QDB: "master",
    }
    assert labels_of(branches, by_rest) == {
        PYTHON: "master",
        REST: "master",
        DUCK: "snapshot/master",
        QDB: "master",
    }


def test_labels_a_root_on_a_release_line_over_a_node_that_has_only_master():
    branches = {PYTHON: ["3.14.x"], REST: ["3.14.x"], DUCK: ["master"], QDB: ["master"]}
    assert labels_of(branches, root_branch="3.14.x") == {
        PYTHON: "3.14.x",
        REST: "3.14.x",
        DUCK: "master",
        QDB: "master",
    }, "qdb-api-rest's own 3.14.x build takes qdb-duck from master too"


def test_labels_err_to_not_as_itself_for_a_line_node_over_a_dependency_with_only_master():
    # qdb-api-rest's own 3.14.x build would be the same build; the rule does
    # not look that far, and a pointer that moves late costs less than one
    # that moves to a build its line never made.
    branches = {
        PYTHON: ["master", "3.14.x", "fix"],
        REST: ["master", "3.14.x"],
        DUCK: ["master"],
        QDB: ["master"],
    }
    times = {"master": 100, "3.14.x": 200}
    assert labels_of(branches, root_branch="fix", times=times) == {
        PYTHON: "fix",
        REST: "snapshot/fix",
        DUCK: "master",
        QDB: "master",
    }


# ---------------------------------------------------------------------------
# Failures of the sources
# ---------------------------------------------------------------------------


def test_an_unknown_pipeline_fails_the_plan_and_names_who_needs_it():
    trees = {(PYTHON, "master"): tree(EDGES[PYTHON] + ("no-such-pipeline",))}
    assert refused(world(ALL_MASTER, trees=trees)) == (
        "project no-such-pipeline: Buildkite has no pipeline no-such-pipeline (404). "
        f"It is a snapshot dependency of qdb-api-python ({BUILD})."
    )


def test_a_buildkite_error_other_than_a_404_fails_the_job_and_is_not_read_as_an_answer():
    w = world(ALL_MASTER)

    def pipelines(slug: str) -> PipelineFacts:
        if slug == DUCK:
            raise BuildkiteError("pipeline qdb-duck: Buildkite answered 401: nope")
        return w.pipelines(slug)

    r = root("master")
    own = read_declaration(w.git.trees[(URLS[PYTHON], r.commit)], PYTHON)
    with pytest.raises(BuildkiteError, match="answered 401") as e:
        make_plan(r, own, w.git, pipelines, T0)
    assert not isinstance(e.value, PlanError), "not a refusal of qdb-duck"


def test_a_read_that_fails_fails_the_plan():
    w = world(ALL_MASTER)
    del w.git.trees[(URLS[DUCK], sha(DUCK, "master"))]
    with pytest.raises(GitError, match="no commit"):
        plan_of(w)


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


def test_the_plan_reads_back_as_it_was_made():
    branches = {**ALL_MASTER, PYTHON: ["master", "feature"]}
    w = world(branches, times={"master": 1})
    p = plan_of(w, "feature", rebuild=REBUILD_ALL)
    assert decode_plan(encode_plan(p)) == p


def test_a_plan_over_the_size_limit_fails_when_written_and_says_how_large():
    many = [f"pipeline-{i:04d}" for i in range(600)]
    refs_of = {PYTHON: ["master"], **{p: ["master"] for p in many}}
    urls = {p: f"https://github.com/bureau14/{p}.git" for p in many}
    p = plan_of(world(refs_of, trees={(PYTHON, "master"): tree(many)}, urls=urls))
    assert len(p.nodes) == 601
    with pytest.raises(PlanError, match=r"the plan is [0-9]+ bytes, more than the 102400 bytes"):
        encode_plan(p)


# ---------------------------------------------------------------------------
# Over the real git reader
# ---------------------------------------------------------------------------


def test_a_chain_planned_over_local_repositories_with_the_real_reader(isolated_git, tmp_path):
    """The diamond in four local repositories.

    quasardb has the root's feature branch; qdb-api-rest pins qdb-duck to an
    annotated tag, which the reader peels.
    """
    urls = {}
    for project in (PYTHON, REST, DUCK, QDB):
        r = new_repository(tmp_path / project)
        files = tree(EDGES[project], pins={DUCK: "refs/tags/v1"} if project == REST else None)
        commit(r, files, "first", T)
        urls[project] = served(r)
    duck_tagged = git(tmp_path / DUCK, "rev-parse", "HEAD")
    git(tmp_path / DUCK, "tag", "-a", "-m", "release", "v1", date=T)
    commit(tmp_path / DUCK, {"x": "after the tag"}, "later", T + 50)
    git(tmp_path / PYTHON, "checkout", "-q", "-b", "feature")
    commit(tmp_path / PYTHON, {"x": "1"}, "on feature", T + 100)
    git(tmp_path / QDB, "checkout", "-q", "-b", "feature")
    qdb_feature = commit(tmp_path / QDB, {"x": "1"}, "on feature", T + 100)
    checkout = clone(urls[PYTHON], tmp_path / "checkout", "feature")
    write(checkout, {".buildkite/snapshot-pipeline.yml": template(["written-by-the-job"])})

    real = RealGit(cwd=str(checkout), protocols=("file",))
    head = real.head_commit()
    own = read_declaration(real.checkout_files(DIRECTORY, counts), PYTHON)
    pipelines = FakePipelines({p: PipelineFacts(p, u, True) for p, u in urls.items()})
    r = Root(PYTHON, "feature", head, PLAN_ID, 512, "https://ci.example/qdb-api-python/512")
    p = make_plan(r, own, real, pipelines, T0)

    assert refs(p) == {
        PYTHON: "refs/heads/feature",
        REST: "refs/heads/master",
        DUCK: "refs/tags/v1",
        QDB: "refs/heads/feature",
    }
    assert p.nodes[DUCK].commit == duck_tagged, "the commit the annotated tag names"
    assert labels(p) == {
        PYTHON: "feature",
        REST: "snapshot/feature",
        DUCK: "snapshot/feature",
        QDB: "feature",
    }
    k_qdb = snapshot_key(QDB, qdb_feature, {})
    k_duck = snapshot_key(DUCK, duck_tagged, {QDB: k_qdb})
    rest = git(tmp_path / REST, "rev-parse", "master")
    assert p.nodes[REST].key == snapshot_key(REST, rest, {DUCK: k_duck, QDB: k_qdb})
