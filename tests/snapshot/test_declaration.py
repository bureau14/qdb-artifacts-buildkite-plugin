from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from lib.snapshot.declaration import (
    FILE_LIMIT,
    LATEST_SUCCESSFUL,
    SNAPSHOT,
    Declaration,
    Entry,
    InvalidEntry,
    StepDownloads,
    UnparsableFile,
    Violation,
    declared_entries,
    declared_pins,
    declared_projects,
    has_snapshot,
    is_template_path,
    mentions_plugin,
    pipeline_downloads,
    read_declaration,
    rule_violations,
    violation_message,
)

FIXTURES = Path(__file__).parent / "fixtures"

REST = "qdb-api-rest"
QDB = "quasardb-build"
DUCK = "qdb-duck"
GO = "qdb-api-go"
MASTER = "refs/heads/master"

# Step templates in the shape the repositories commit: placeholders in plain
# and quoted scalars, the plugin as one entry of a plugins list.
BUILD_TEMPLATE = """\
label: ":hammer: build ({slug})"
key: build-{slug}
commands:
  - bash scripts/cicd/20.build.sh
plugins:
  - bureau14/qdb-artifacts#master:
      download:
        clean: true
        projects:
        - project_id: "quasardb-build"
          output-dir: "qdb"
          extract: true
          files:
            - "*-c-api.tar.zst!*"
        - project_id: "qdb-duck"
          output-dir: "duck"
          files: ["artifacts/qdb-duck-*-libduckdb.tar.zst!*"]
      upload:
        files: ["artifacts/*-rest.*"]
      promote: {}
  - bureau14/qdb-test-report#master:
      title: "Test report {slug}"
      job:
        variant: {slug}
timeout_in_minutes: 60
agents:
  queue: "{queue}"
"""

LINT_TEMPLATE = """\
label: ":golangci-lint: lint"
key: lint
plugins:
  - bureau14/qdb-artifacts#master:
      download:
        projects:
        - project_id: "quasardb-build"
          files: ["*-c-api.tar.zst!*"]
        - files: ["artifacts/own-*.tar.zst"]
        - project_id: "qdb-api-rest"
          files: ["artifacts/*-rest.*"]
"""


def templates(**files: str) -> dict[str, str]:
    """Files under .buildkite/steps/, by bare name."""
    return {f".buildkite/steps/{name}.yml": text for name, text in files.items()}


def read(files, own=REST):
    return read_declaration(files, own)


def entries(files, own=REST):
    reading = read(files, own)
    assert reading.errors == ()
    return list(reading.declaration.entries)


# ---------------------------------------------------------------------------
# The reading rule
# ---------------------------------------------------------------------------


def test_reads_the_projects_of_other_pipelines_from_templates_with_placeholders():
    files = templates(_build=BUILD_TEMPLATE, _lint=LINT_TEMPLATE)
    assert entries(files) == [Entry(DUCK), Entry(QDB)]


def test_own_project_entries_and_entries_without_a_project_id_are_dropped():
    assert entries(templates(_lint=LINT_TEMPLATE), own=REST) == [Entry(QDB)]
    assert entries(templates(_lint=LINT_TEMPLATE), own=QDB) == [Entry(REST)]


@pytest.mark.parametrize("project_id", ["~", "''", "null"])
def test_a_null_or_empty_project_id_is_the_own_project(project_id):
    text = LINT_TEMPLATE.replace('"qdb-api-rest"', project_id)
    assert entries(templates(_lint=text)) == [Entry(QDB)]


@pytest.mark.parametrize(
    "path, counts",
    [
        (".buildkite/steps/_build.yml", True),
        (".buildkite/steps/_build.yaml", True),
        (".buildkite/pipeline.yml", True),
        (".buildkite/a/b/c.yml", True),
        (".buildkite/tools/qdb_pipeline/steps/x.yml", False),
        (".buildkite/snapshot-pipeline.yml", True),
        (".buildkite/steps/_build.yml.bak", False),
        (".buildkite/steps/_build.YML", False),
        (".buildkite/steps/_build.json", False),
        (".buildkite/pipeline.py", False),
        ("docker/compose.yml", False),
        ("buildkite/steps/_build.yml", False),
        ("x/.buildkite/steps/_build.yml", False),
    ],
)
def test_which_paths_count(path, counts):
    assert is_template_path(path) is counts
    reading = read({path: BUILD_TEMPLATE})
    assert bool(reading.declaration.entries) is counts


def test_a_file_that_does_not_name_the_plugin_is_never_parsed():
    files = {".buildkite/broken.yml": "steps: [\n", **templates(_lint=LINT_TEMPLATE)}
    assert entries(files) == [Entry(QDB)]


@pytest.mark.parametrize(
    "text, mentions",
    [
        ("Bureau14/QDB-Artifacts#master", True),
        (b"x: BUREAU14/QDB-ARTIFACTS", True),
        ("bureau14/qdb_artifacts", False),
        ("bureau14/qdb-art" + chr(0x130) + "facts", False),
    ],
)
def test_a_file_that_names_the_plugin_in_any_ascii_case_is_parsed(text, mentions):
    # A key that names the plugin another way is an error, which only a file
    # that is parsed can show.
    assert mentions_plugin(text) is mentions


def test_a_file_that_names_the_plugin_and_does_not_parse_is_an_error_value():
    broken = "# bureau14/qdb-artifacts\nsteps: [\n"
    reading = read({".buildkite/steps/broken.yml": broken, **templates(_lint=LINT_TEMPLATE)})
    assert reading.declaration.entries == (Entry(QDB),)
    [error] = reading.errors
    assert isinstance(error, UnparsableFile)
    assert error.path == ".buildkite/steps/broken.yml"
    assert "not valid YAML" in error.detail and "line 3" in error.detail


@pytest.mark.parametrize(
    "text",
    [
        "# bureau14/qdb-artifacts\na: 1\n---\nb: 2\n",
        "# bureau14/qdb-artifacts\na: 2001-13-45\n",
        "# bureau14/qdb-artifacts\n" + "[" * 5000 + "]" * 5000,
        "# bureau14/qdb-artifacts\na: !!python/object:os.system x\n",
    ],
    ids=["two documents", "bad date", "deep nesting", "python tag"],
)
def test_every_parse_failure_is_a_value(text):
    [error] = read({".buildkite/x.yml": text}).errors
    assert isinstance(error, UnparsableFile)


def padded(text: str, size: int) -> bytes:
    """``text`` with a comment that makes it ``size`` bytes long."""
    data = text.encode("utf-8") + b"#"
    return data + b"x" * (size - len(data) - 1) + b"\n"


def test_a_template_of_the_size_limit_is_read():
    data = padded(LINT_TEMPLATE, FILE_LIMIT)
    assert len(data) == FILE_LIMIT
    assert entries({".buildkite/steps/_lint.yml": data}) == [Entry(QDB)]


def test_a_template_over_the_size_limit_does_not_parse_and_is_not_decoded():
    # Parsing YAML takes about 160 times the size of the text in memory.
    data = b"# bureau14/qdb-artifacts\n" + b"\xff" * FILE_LIMIT
    [error] = read({".buildkite/x.yml": data}).errors
    assert error == UnparsableFile(
        ".buildkite/x.yml",
        f"is {FILE_LIMIT + 25} bytes, more than the 1048576 bytes the reader parses",
    )
    assert read({".buildkite/x.yml": b"\xff" * (FILE_LIMIT + 1)}).errors == (), (
        "a file that does not name the plugin is not read, whatever its size"
    )


def test_content_may_be_bytes_and_must_then_be_utf8():
    files = {p: text.encode("utf-8") for p, text in templates(_build=BUILD_TEMPLATE).items()}
    assert entries(files) == [Entry(DUCK), Entry(QDB)]
    [error] = read({".buildkite/x.yml": b"# bureau14/qdb-artifacts \xff\n"}).errors
    assert isinstance(error, UnparsableFile) and "not UTF-8" in error.detail


def test_every_key_the_agent_reads_as_an_option_is_read_as_that_option():
    text = """\
- bureau14/qdb-artifacts#master:
    Download:
      PROJECTS:
      - {Project-Id: quasardb-build, GIT REF: refs/heads/x}
      - {"project  id": qdb-duck}
- bureau14/qdb-artifacts#master:
    download_:
      " projects":
      - {project__id: qdb-api-go, Build-Id: LATEST_SUCCESSFUL}
"""
    assert entries({".buildkite/x.yml": text}) == [
        Entry(GO, LATEST_SUCCESSFUL),
        Entry(DUCK),
        Entry(QDB, SNAPSHOT, "refs/heads/x"),
    ]


def test_a_key_the_agent_does_not_read_as_an_option_is_not_read_as_one():
    text = """\
bureau14/qdb-artifacts#master:
  download:
    projects:
    - {"project\\vid": qdb-duck, "project_id_": qdb-api-go, "files": [x]}
"""
    assert entries({".buildkite/x.yml": text}) == []


@pytest.mark.parametrize(
    "key", ["Bureau14/QDB-Artifacts#master", "bureau14/qdb-artifacts-buildkite-plugin#v1"]
)
def test_a_key_that_names_the_plugin_another_way_is_an_error_value(key):
    text = f"steps:\n  - plugins:\n      - {key}:\n          download: {{projects: []}}\n"
    reading = read({".buildkite/x.yml": text})
    assert reading.declaration.entries == ()
    [error] = reading.errors
    assert isinstance(error, InvalidEntry)
    assert error.detail == (
        f"the plugin is named {key!r}: the agent runs it for that key, and the reading rule reads "
        "only bureau14/qdb-artifacts or bureau14/qdb-artifacts#<ref>"
    )


def test_entries_are_found_at_any_depth_and_in_either_plugins_form():
    text = """\
steps:
  - group: g
    steps:
      - key: a
        plugins:
          bureau14/qdb-artifacts#master:
            download: {projects: [{project_id: quasardb-build}]}
  - key: b
    plugins:
      - other/plugin#v1:
          download: {projects: [{project_id: not-the-artifacts-plugin}]}
      - bureau14/qdb-artifacts:
          download: {projects: [{project_id: qdb-duck}]}
"""
    assert entries({".buildkite/pipeline.yml": text}) == [Entry(DUCK), Entry(QDB)]


def test_odd_shapes_give_no_entries():
    text = """\
- bureau14/qdb-artifacts#master
- bureau14/qdb-artifacts#master: ~
- bureau14/qdb-artifacts#master: {download: [x]}
- bureau14/qdb-artifacts#master: {download: {projects: quasardb-build}}
- bureau14/qdb-artifacts#master: {download: {projects: [quasardb-build, 3, [x]]}}
"""
    assert entries({".buildkite/x.yml": text}) == []
    assert entries({".buildkite/x.yml": "# bureau14/qdb-artifacts\n"}) == []
    assert entries({".buildkite/x.yml": "bureau14/qdb-artifacts\n"}) == []


ALIASED = """\
common: &common
  bureau14/qdb-artifacts#master:
    download: {projects: [{project_id: quasardb-build}]}
loop: &loop [*loop, *common]
steps:
  - plugins: [*common]
  - plugins: [*common]
"""


def nested_aliases(depth: int) -> str:
    """Anchors a0..a<depth>, each a list of ten aliases of the one below: 10**depth leaves."""
    rows = ["a0: &a0 [1]"]
    rows += [f"a{i}: &a{i} [" + ", ".join([f"*a{i - 1}"] * 10) + "]" for i in range(1, depth + 1)]
    return "\n".join(rows) + "\n"


@pytest.mark.parametrize(
    "text, line",
    [
        (ALIASED, 1),
        ("# bureau14/qdb-artifacts\nsteps:\n  - &one {key: a}\n", 3),
        ("# bureau14/qdb-artifacts\nbase: {a: 1}\nstep: {<<: *undefined}\n", 3),
        (
            nested_aliases(5)
            + "bureau14/qdb-artifacts#master: {download: {projects: [{project_id: *a5}]}}\n",
            1,
        ),
    ],
    ids=["aliases", "an anchor alone", "a merge key", "aliases that expand"],
)
def test_a_template_with_an_anchor_or_alias_does_not_parse(text, line):
    # PyYAML keeps an alias as a shared reference, so a few hundred bytes can
    # stand for a tree that no message, comparison or walk can afford.
    reading = read({".buildkite/x.yml": text})
    assert reading.declaration.entries == ()
    [error] = reading.errors
    assert isinstance(error, UnparsableFile)
    assert error.detail.startswith(f"holds an anchor or alias at line {line}, column ")
    assert len(error.detail) < 200


def test_both_spellings_of_an_option_are_one_option():
    text = """\
bureau14/qdb-artifacts#master:
  download:
    projects:
    - {project-id: quasardb-build, git-ref: refs/heads/3.14.x}
    - {project_id: qdb-duck, project-id: qdb-duck, git_ref: x, git-ref: x}
"""
    assert entries({".buildkite/x.yml": text}) == [
        Entry(DUCK, SNAPSHOT, "x"),
        Entry(QDB, SNAPSHOT, "refs/heads/3.14.x"),
    ]


def test_an_option_written_two_ways_with_two_values_is_an_error_value():
    text = """\
bureau14/qdb-artifacts#master:
  download:
    projects:
    - {project_id: quasardb-build, Project-Id: qdb-duck}
    - {project_id: qdb-api-go, git_ref: refs/heads/a, "git ref": refs/heads/b}
"""
    reading = read({".buildkite/x.yml": text})
    assert reading.declaration.entries == ()
    assert [e.detail for e in reading.errors] == [
        (
            "project_id is given twice with different values: 'quasardb-build' as "
            "'project_id' and 'qdb-duck' as 'Project-Id'"
        ),
        (
            "git_ref is given twice with different values: 'refs/heads/a' as 'git_ref' and "
            "'refs/heads/b' as 'git ref'"
        ),
    ]
    assert all(isinstance(e, InvalidEntry) for e in reading.errors)


@pytest.mark.parametrize("block", ["download", "projects"])
def test_a_block_written_two_ways_with_two_values_is_an_error_value(block):
    one = {"projects": [{"project_id": QDB}]}
    other = {"projects": [{"project_id": DUCK}]}
    if block == "download":
        config = {"download": one, "Download": other}
    else:
        config = {"download": {"projects": one["projects"], "PROJECTS": other["projects"]}}
    text = yaml.safe_dump({"bureau14/qdb-artifacts#master": config})
    reading = read({".buildkite/x.yml": text})
    assert reading.declaration.entries == ()
    [error] = reading.errors
    assert error.detail.startswith(f"{block} is given twice with different values: ")


@pytest.mark.parametrize(
    "git_ref, pin",
    [
        ('"refs/heads/3.14.x"', "refs/heads/3.14.x"),
        ("refs/heads/3.14.x-20990101-pinned", "refs/heads/3.14.x-20990101-pinned"),
        ('"refs/heads/{line}"', None),
        ("{ref}", None),
        ('"$BUILDKITE_BRANCH"', None),
        ('"${REF}"', None),
        ('""', None),
        ("~", None),
        ("3.14", None),
        ("[a]", None),
    ],
)
def test_a_pin_is_a_literal_git_ref(git_ref, pin):
    old = 'project_id: "quasardb-build"'
    text = LINT_TEMPLATE.replace(old, f"{old}\n          git_ref: {git_ref}")
    assert entries(templates(_lint=text)) == [Entry(QDB, SNAPSHOT, pin)]


NOT_A_SLUG = "is not a pipeline slug (lowercase letters, digits and -)"


@pytest.mark.parametrize(
    "project_id, detail",
    [
        ('"quasardb-{flavour}"', f"project id 'quasardb-{{flavour}}' {NOT_A_SLUG}"),
        ('"$PROJECT"', f"project id '$PROJECT' {NOT_A_SLUG}"),
        ("Quasardb-Build", NOT_A_SLUG),
        ("quasardb_build", NOT_A_SLUG),
        ("-quasardb", NOT_A_SLUG),
        ('"qdb duck"', NOT_A_SLUG),
        ('"[click](https://evil.example)"', NOT_A_SLUG),
        ('"caf\\u00e9"', NOT_A_SLUG),
        ('"a\\nb"', NOT_A_SLUG),
        ("{slug}", "is not a string"),
        ("123", "is not a string"),
        ("[quasardb-build]", "is not a string"),
    ],
)
def test_a_project_id_that_is_not_a_literal_slug_is_an_error_value(project_id, detail):
    text = LINT_TEMPLATE.replace('"qdb-api-rest"', project_id)
    reading = read(templates(_lint=text))
    assert reading.declaration.entries == (Entry(QDB),)
    [error] = reading.errors
    assert isinstance(error, InvalidEntry)
    assert error.path == ".buildkite/steps/_lint.yml" and detail in error.detail


@pytest.mark.parametrize(
    "entry",
    [
        "{project_id: [" + ", ".join(["quasardb-build"] * 20_000) + "]}",
        '{project_id: "' + "x" * 200_000 + '", project-id: y}',
        '{project_id: "caf\\u00e9\\nqdb-api-go cannot give a snapshot", project-id: y}',
    ],
    ids=["a long list", "a long string", "a line break and a letter that is not ascii"],
)
def test_a_foreign_value_in_a_detail_is_shown_short_on_one_line_of_ascii(entry):
    text = f"bureau14/qdb-artifacts#master: {{download: {{projects: [{entry}]}}}}\n"
    [error] = read({".buildkite/x.yml": text}).errors
    assert isinstance(error, InvalidEntry)
    assert len(error.detail) < 300 and "\n" not in error.detail, error.detail[:300]
    error.detail.encode("ascii")


@pytest.mark.parametrize(
    "build_id",
    [
        "build_id: LATEST_SUCCESSFUL",
        'build-id: "01890000-aaaa-7000-8000-000000000002"',
        'build_id: "{build}"',
        "build_id: ~",
    ],
)
def test_an_entry_that_names_its_build_is_a_latest_successful_dependency(build_id):
    old = 'project_id: "quasardb-build"'
    with_ref = f'{old}\n          git_ref: "refs/heads/3.14.x"\n          {build_id}'
    text = LINT_TEMPLATE.replace(old, with_ref)
    [latest] = entries(templates(_lint=text))
    assert latest == Entry(QDB, LATEST_SUCCESSFUL), "of its type, without a pin"
    assert latest.path == ".buildkite/steps/_lint.yml"
    d = read(templates(_lint=text, _build=BUILD_TEMPLATE)).declaration
    assert declared_projects(d) == {DUCK, QDB}, (
        "the other entry of quasardb-build is a snapshot one"
    )
    assert declared_projects(d, LATEST_SUCCESSFUL) == {QDB}
    assert declared_entries(d, LATEST_SUCCESSFUL) == (Entry(QDB, LATEST_SUCCESSFUL),)
    assert not has_snapshot(d), "a snapshot never contains a latest successful dependency"


def test_a_declaration_of_snapshot_dependencies_only_has_a_snapshot():
    assert has_snapshot(read(templates(_build=BUILD_TEMPLATE)).declaration)
    assert has_snapshot(Declaration())


def test_a_latest_successful_entry_is_read_as_any_other():
    text = LINT_TEMPLATE.replace('"qdb-api-rest"', '"x-{y}"\n          build_id: LATEST_SUCCESSFUL')
    reading = read(templates(_lint=text), own=DUCK)
    assert reading.declaration.entries == (Entry(QDB),)
    [error] = reading.errors
    assert isinstance(error, InvalidEntry) and NOT_A_SLUG in error.detail


def test_an_entry_keeps_the_first_file_it_was_found_in():
    reading = read(templates(b=LINT_TEMPLATE, a=LINT_TEMPLATE))
    assert [(e.project, e.path) for e in reading.declaration.entries] == [
        (QDB, ".buildkite/steps/a.yml")
    ]


def test_entries_are_kept_once_sorted_and_errors_once_in_order():
    bad = LINT_TEMPLATE.replace('"qdb-api-rest"', '"x-{y}"')
    files = templates(b=bad, a=bad, c=BUILD_TEMPLATE)
    reading = read(files)
    assert reading.declaration.entries == (Entry(DUCK), Entry(QDB))
    assert [e.path for e in reading.errors] == [
        ".buildkite/steps/a.yml",
        ".buildkite/steps/b.yml",
    ]


def test_declared_projects_and_pins():
    d = Declaration((Entry(DUCK), Entry(QDB), Entry(QDB, SNAPSHOT, "refs/heads/3.14.x")))
    assert declared_projects(d) == {DUCK, QDB}
    assert declared_pins(d, QDB) == {"refs/heads/3.14.x"}
    assert declared_pins(d, DUCK) == frozenset()
    assert declared_pins(d, "other") == frozenset()


# ---------------------------------------------------------------------------
# The steps of a generated pipeline
# ---------------------------------------------------------------------------


def fixture(name: str):
    return yaml.safe_load((FIXTURES / name).read_text())


DL = {"download": {"projects": [{"project_id": QDB, "git_ref": MASTER}]}}


def test_pipeline_downloads_names_steps_by_key_then_label_then_position():
    doc = {
        "steps": [
            {"key": "k", "label": "L", "plugins": [{"bureau14/qdb-artifacts#x": DL}]},
            "wait",
            {"label": "only a label", "plugins": [{"bureau14/qdb-artifacts#x": DL}]},
            {"command": "x"},
            {"group": "g", "steps": [{"plugins": [{"bureau14/qdb-artifacts#x": DL}]}]},
            {"identifier": "old", "plugins": [{"bureau14/qdb-artifacts#x": DL}]},
        ]
    }
    assert [sd.step for sd in pipeline_downloads(doc)] == ["k", "only a label", "step 5", "old"]


def test_pipeline_downloads_accepts_a_bare_list_and_nothing_else():
    steps = [{"key": "a", "plugins": [{"bureau14/qdb-artifacts#x": DL}]}]
    entry = {"project_id": QDB, "git_ref": MASTER}
    assert pipeline_downloads(steps) == [StepDownloads("a", (entry,))]
    assert pipeline_downloads({"steps": steps}) == pipeline_downloads(steps)
    assert pipeline_downloads({"env": {}}) == []
    assert pipeline_downloads("steps") == []


def test_pipeline_downloads_on_a_real_pipeline_with_groups():
    found = pipeline_downloads(fixture("qdb-duck.yml"))
    assert [sd.step for sd in found] == [
        "build-linux-haswell-release",
        "build-freebsd-haswell-release",
    ]
    assert {e["project_id"] for sd in found for e in sd.entries} == {QDB}


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------

TEST_BRANCH = "refs/heads/feature"

# Fixture -> (its own project, the projects of other pipelines it downloads).
FIXTURE_PROJECTS = {
    "qdb-api-go.yml": ("qdb-api-go", {QDB}),
    "qdb-api-python.yml": ("qdb-api-python", {REST, QDB}),
    "qdb-api-rest.yml": (REST, {QDB, DUCK}),
    "qdb-duck.yml": (DUCK, {QDB}),
    "quasardb.yml": (QDB, set()),
}


def declaration_of(*projects: str, pins=()) -> Declaration:
    plain = tuple(Entry(p) for p in projects)
    return Declaration(plain + tuple(Entry(p, SNAPSHOT, r) for p, r in pins))


@pytest.mark.parametrize("name", sorted(FIXTURE_PROJECTS))
def test_real_pipelines_keep_the_rule_with_their_own_declaration(name):
    own, projects = FIXTURE_PROJECTS[name]
    downloads = pipeline_downloads(fixture(name))
    assert rule_violations(downloads, declaration_of(*projects), own, TEST_BRANCH) == []


@pytest.mark.parametrize("name", ["qdb-api-python.yml", "qdb-api-rest.yml"])
def test_real_pipelines_break_the_rule_without_a_declared_project(name):
    own, projects = FIXTURE_PROJECTS[name]
    downloads = pipeline_downloads(fixture(name))
    found = rule_violations(downloads, declaration_of(*(projects - {QDB})), own, TEST_BRANCH)
    assert found and {v.project for v in found} == {QDB}
    assert all("no committed template declares" in v.reason for v in found)


def test_real_pipelines_break_the_rule_on_another_ref():
    own, projects = FIXTURE_PROJECTS["qdb-duck.yml"]
    downloads = pipeline_downloads(fixture("qdb-duck.yml"))
    found = rule_violations(downloads, declaration_of(*projects), own, MASTER)
    assert [v.step for v in found] == [
        "build-linux-haswell-release",
        "build-freebsd-haswell-release",
    ]


def one_step(*entries) -> list[StepDownloads]:
    return [StepDownloads("build", tuple(entries))]


def test_a_declared_pin_is_allowed_and_another_ref_is_not():
    d = declaration_of(DUCK, pins=[(QDB, "refs/heads/3.14.x")])
    at_pin = {"project_id": QDB, "git_ref": "refs/heads/3.14.x"}
    own_ref = {"project_id": QDB, "git-ref": MASTER}
    assert rule_violations(one_step(at_pin, own_ref), d, REST, MASTER) == []
    other = {"project_id": QDB, "git_ref": "refs/heads/x"}
    [v] = rule_violations(one_step(other), d, REST, MASTER)
    assert v == Violation(
        "build",
        QDB,
        "downloads quasardb-build at 'refs/heads/x', which is neither the build's own ref "
        "refs/heads/master nor a pin declared for quasardb-build (declared: refs/heads/3.14.x)",
    )
    assert violation_message(v).startswith("step build: downloads quasardb-build at")


@pytest.mark.parametrize("ref", [None, "", 3.14, {"ref": None}, ["refs/heads/master"]])
def test_a_missing_or_odd_git_ref_is_a_violation(ref):
    entry = {"project_id": QDB} if ref is None else {"project_id": QDB, "git_ref": ref}
    [v] = rule_violations(one_step(entry), declaration_of(QDB), REST, MASTER)
    assert v.project == QDB and "neither the build's own ref" in v.reason
    if ref is None:
        assert "at no git ref" in v.reason


@pytest.mark.parametrize("option", ["build_id", "build-id"])
def test_a_latest_successful_entry_must_be_declared_as_one_at_any_ref(option):
    d = Declaration((Entry(GO, LATEST_SUCCESSFUL), Entry(QDB)))
    named = {"project_id": GO, option: "LATEST_SUCCESSFUL", "git_ref": "refs/heads/x"}
    assert rule_violations(one_step(named), d, REST, MASTER) == []
    [v] = rule_violations(one_step(named), declaration_of(GO), REST, MASTER)
    assert v.reason == (
        "downloads from qdb-api-go as a latest successful dependency, "
        "which no committed template declares as a latest successful dependency"
    )


def test_a_snapshot_entry_of_a_project_declared_latest_successful_breaks_the_rule():
    d = Declaration((Entry(GO, LATEST_SUCCESSFUL),))
    [v] = rule_violations(one_step({"project_id": GO, "git_ref": MASTER}), d, REST, MASTER)
    assert "as a snapshot dependency, which no committed template declares as a snapshot" in (
        v.reason
    )


def test_own_project_entries_are_not_checked():
    own = [
        {"files": ["x"]},
        {"project_id": REST, "git_ref": "refs/heads/anything"},
        {"project_id": None},
        {"project_id": ""},
    ]
    assert rule_violations(one_step(*own), Declaration(), REST, MASTER) == []


def test_unreadable_entries_are_violations():
    found = rule_violations(
        one_step(
            {"project_id": QDB, "project-id": DUCK, "git_ref": MASTER},
            {"project_id": 7, "git_ref": MASTER},
            {"project_id": QDB, "git_ref": MASTER, "git-ref": "refs/heads/x"},
        ),
        declaration_of(QDB, DUCK),
        REST,
        MASTER,
    )
    assert [(v.project, v.reason) for v in found] == [
        (
            "'quasardb-build' / 'qdb-duck'",
            (
                "project_id is given twice with different values: 'quasardb-build' as "
                "'project_id' and 'qdb-duck' as 'project-id'"
            ),
        ),
        ("7", "project id 7 is not a string"),
        (
            QDB,
            (
                "downloads from quasardb-build, but its git_ref is given twice with different "
                "values: 'refs/heads/master' as 'git_ref' and 'refs/heads/x' as 'git-ref'"
            ),
        ),
    ]


def test_a_plugin_named_another_way_in_a_generated_pipeline_is_a_violation():
    doc = {
        "steps": [
            {"key": "a", "plugins": [{"Bureau14/qdb-artifacts#master": DL}]},
            {"key": "c", "plugins": [{"bureau14/qdb-artifacts#master": DL}]},
        ]
    }
    [v] = rule_violations(pipeline_downloads(doc), declaration_of(QDB), REST, MASTER)
    assert (v.step, v.project) == ("a", None)
    assert violation_message(v) == (
        "step a: the plugin is named 'Bureau14/qdb-artifacts#master': the agent runs it for that "
        "key, and the reading rule reads only bureau14/qdb-artifacts or "
        "bureau14/qdb-artifacts#<ref>"
    )


@pytest.mark.parametrize(
    "config, option",
    [
        ({**DL, "Download": {"clean": True}}, "download"),
        ({"download": {**DL["download"], "PROJECTS": []}}, "projects"),
        ({**DL, "upload": {"variant": "v"}, "Upload": {"files": ["x"]}}, "upload"),
        ({**DL, "promote": {"variant": "v"}, "PROMOTE": {"git_ref": MASTER}}, "promote"),
    ],
)
def test_a_block_written_two_ways_with_two_values_in_a_generated_pipeline_is_a_violation(
    config, option
):
    doc = {"steps": [{"key": "a", "plugins": [{"bureau14/qdb-artifacts#master": config}]}]}
    [v] = rule_violations(pipeline_downloads(doc), declaration_of(QDB), REST, MASTER)
    assert (v.step, v.project) == ("a", None)
    assert v.reason.startswith(f"{option} is given twice with different values: ")


@pytest.mark.parametrize(
    "config, where",
    [
        ({"upload": {"variant": "v", "snapshot": "v1-" + "0" * 32}}, "upload.snapshot"),
        ({"promote": {"variant": "v", "Snapshot": "v1-" + "0" * 32}}, "promote.Snapshot"),
        ({"promote": {"project_id": DUCK, "variant": "v", "snapshot": "x"}}, "promote.snapshot"),
    ],
    ids=["upload", "promote in another spelling", "promote of another project"],
)
def test_a_snapshot_option_in_a_generated_pipeline_is_a_violation(config, where):
    # Only the plan step writes it, at the key of the build: a promote would
    # write a pointer at any key it is given.
    doc = {"steps": [{"key": "a", "plugins": [{"bureau14/qdb-artifacts#master": config}]}]}
    [v] = rule_violations(pipeline_downloads(doc), declaration_of(QDB), REST, MASTER)
    assert (v.step, v.project) == ("a", None)
    assert v.reason.startswith(f"{where} is given in the generated pipeline")


def test_a_step_is_named_short_in_ascii_in_a_message():
    # A step's label comes from a template.
    label = "build caf" + chr(0xE9) + " " + "x" * 2000
    doc = {"steps": [{"label": label, "plugins": [{"bureau14/qdb-artifacts#master": DL}]}]}
    [v] = rule_violations(pipeline_downloads(doc), Declaration(), REST, MASTER)
    message = violation_message(v)
    assert message.startswith("step build caf\\xe9 xxx") and message.isascii()
    assert len(message) < 600


def test_the_declaration_may_hold_more_and_violations_are_reported_once():
    entry = {"project_id": "qdb-other", "git_ref": MASTER}
    downloads = [StepDownloads("a", (entry, entry)), StepDownloads("b", (entry,))]
    found = rule_violations(downloads, declaration_of(QDB, DUCK, "unused"), REST, MASTER)
    assert [(v.step, v.project) for v in found] == [("a", "qdb-other"), ("b", "qdb-other")]


def substituted(template: str, **values: str) -> str:
    for k, v in values.items():
        template = template.replace("{" + k + "}", v)
    return template


def test_the_reader_and_the_rule_agree_on_a_pipeline_built_from_the_templates():
    # A generator that loads the templates, fills the placeholders and sets
    # the build's own ref keeps the rule; one that adds a download of another
    # pipeline in code breaks it, and the step is named.
    reading = read(templates(_build=BUILD_TEMPLATE, _lint=LINT_TEMPLATE))
    step = yaml.safe_load(substituted(BUILD_TEMPLATE, slug="linux-haswell", queue="q"))
    for e in step["plugins"][0]["bureau14/qdb-artifacts#master"]["download"]["projects"]:
        e["git_ref"] = MASTER
    doc = {"steps": [step]}
    assert rule_violations(pipeline_downloads(doc), reading.declaration, REST, MASTER) == []
    projects = step["plugins"][0]["bureau14/qdb-artifacts#master"]["download"]["projects"]
    projects.append({"project_id": "qdb-api-python", "git_ref": MASTER})
    [v] = rule_violations(pipeline_downloads(doc), reading.declaration, REST, MASTER)
    assert (v.step, v.project) == ("build-linux-haswell", "qdb-api-python")
