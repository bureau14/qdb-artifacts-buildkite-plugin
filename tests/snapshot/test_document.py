"""The generated pipeline as data: loading and dumping, what the plan step finds, what it writes."""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from plans import DUCK, PLAN_ID, PYTHON, QDB, REST, plan, sha

from lib.pointer import Build, Pointer
from lib.snapshot.configuration import download_entries
from lib.snapshot.document import (
    DocumentError,
    Rewrite,
    dump_document,
    dump_written,
    escaped,
    load_document,
    needed_variants,
    planned,
    produced_variants,
    promote_keys,
    promotions,
    resume_pipeline,
    resume_step,
    snapshot_dependencies,
    trigger_steps,
    with_promote_keys,
)

FIXTURES = Path(__file__).parent / "fixtures"
GO = "qdb-api-go"
PLUGIN = "bureau14/qdb-artifacts#master"
COMMIT = "c" * 40
KEY = "v1-" + "d" * 32
MASTER = "refs/heads/master"
LINUX, ARM, DOCS = "linux-core2-release", "linux-aarch64-release", "docs"


def pointer(n: int, ref: str = MASTER, project: str = REST) -> Pointer:
    """The pointer of build ``n``; the plan step writes only its build and its ref."""
    build = Build(f"01890000-aaaa-7000-8000-{n:012d}", n, f"https://ci.example/b/{n}")
    return Pointer(project, KEY, LINUX, ref, "b" * 40, build, "2025-11-17T12:00:00.000Z")


def entry(project: str | None, variant: str, **more) -> dict:
    found = {"variant": variant, "git_ref": MASTER, "files": ["*.tar.zst"], **more}
    return {"project_id": project, **found} if project else found


def artifacts(**blocks) -> dict:
    return {PLUGIN: blocks}


def build_step(key: str | None, variant: str, downloads: list, label: str = "build") -> dict:
    step = {
        "label": f"{label} {variant}",
        "command": "make",
        "agents": {"queue": "q"},
        "plugins": [
            {"docker#v5.13.0": {"image": "x"}},
            artifacts(
                download={"clean": True, "projects": downloads},
                upload={"files": ["artifacts/*"], "git_ref": MASTER, "variant": variant},
                promote={"git_ref": MASTER, "variant": variant},
            ),
        ],
    }
    return {"key": key, **step} if key else step


def pipeline() -> dict:
    """qdb-api-rest: two builds (one in a group), docs uploaded only, a test of its own build."""
    qdb_linux = entry(QDB, LINUX)
    return {
        "steps": [
            {
                "group": "Linux",
                "steps": [
                    build_step("build-linux", LINUX, [qdb_linux, entry(DUCK, LINUX)]),
                ],
            },
            build_step(None, ARM, [entry(QDB, ARM), entry(DUCK, ARM)]),
            {
                "key": "docs",
                "command": "make docs",
                "plugins": [
                    artifacts(upload={"files": ["d/*"], "git_ref": MASTER, "variant": DOCS})
                ],
            },
            {
                "key": "test-linux",
                "depends_on": ["build-linux"],
                "command": "make test",
                "plugins": [
                    artifacts(
                        download={
                            "projects": [
                                entry(None, LINUX),
                                entry(GO, LINUX, build_id="LATEST_SUCCESSFUL"),
                                qdb_linux,
                            ]
                        }
                    )
                ],
            },
            {"key": "report", "depends_on": ["build-linux"], "command": "true"},
        ]
    }


DEPENDENCIES = {
    (QDB, LINUX): pointer(1, project=QDB),
    (QDB, ARM): pointer(2, project=QDB),
    (DUCK, LINUX): pointer(3, "refs/heads/snapshot/feature", DUCK),
    (DUCK, ARM): pointer(4, "refs/heads/snapshot/feature", DUCK),
}


def rewrite(key=KEY, existing=None, dependencies=None) -> Rewrite:
    deps = DEPENDENCIES if dependencies is None else dependencies
    return Rewrite(REST, key, COMMIT, deps, existing or {})


def steps_of(doc) -> list:
    """The leaf steps, groups descended."""
    out = []
    for s in doc["steps"]:
        out.extend(s["steps"] if isinstance(s, dict) and "group" in s else [s])
    return out


def config(step) -> dict:
    """The artifacts plugin configuration of a step, under whatever key it now has."""
    [found] = [v for p in step["plugins"] for k, v in p.items() if k.startswith("bureau14/")]
    return found


# ---------------------------------------------------------------------------
# Loading and dumping
# ---------------------------------------------------------------------------


def test_a_document_the_safe_loader_refuses_is_an_error_with_the_parsers_message():
    with pytest.raises(DocumentError) as e:
        load_document("steps:\n  - label: [\n")
    assert str(e.value).startswith("the generated pipeline does not load: ")
    assert "line 3, column 1" in str(e.value)
    with pytest.raises(DocumentError, match="does not load"):
        load_document("steps: !!python/object:os.system x\n")


def test_the_dump_is_the_runners():
    # The runner prints PyYAML's dump (buildkite-sdk 0.8.0: yaml.dump(to_dict())).
    doc = pipeline()
    assert dump_document(doc) == yaml.dump(doc)


SHARED = """\
steps:
- key: a
  plugins:
  - bureau14/qdb-artifacts#master: &config
      download:
        projects:
        - &qdb
          {git_ref: refs/heads/master, project_id: quasardb-build, variant: linux-core2-release}
- key: b
  plugins:
  - bureau14/qdb-artifacts#master: *config
- key: c
  plugins:
  - bureau14/qdb-artifacts#master:
      download:
        projects: [*qdb]
"""


def test_objects_the_document_shares_stay_shared_after_load_and_dump():
    doc = load_document(SHARED)
    a, b, c = (config(s) for s in doc["steps"])
    assert a is b and c["download"]["projects"][0] is a["download"]["projects"][0]
    again = load_document(dump_document(doc))
    a2, b2, c2 = (config(s) for s in again["steps"])
    assert a2 is b2 and c2["download"]["projects"][0] is a2["download"]["projects"][0]
    assert again == doc


@pytest.mark.parametrize("name", sorted(p.name for p in FIXTURES.glob("*.yml")))
def test_a_generated_pipeline_reads_back_as_it_was_after_a_dump(name):
    doc = load_document((FIXTURES / name).read_text(encoding="utf-8"))
    assert load_document(dump_document(doc)) == doc


def test_a_written_value_has_every_dollar_doubled():
    assert escaped("refs/heads/a$b$$c") == "refs/heads/a$$b$$$$c"
    assert escaped("a\\b $c\\") == "a\\b $$c\\"


@pytest.mark.parametrize("text", ["a\\$b", "\\$", "a\\\\$b"])
def test_a_value_with_a_backslash_before_a_dollar_cannot_be_written(text):
    # The upload reads \$ as an escaped $, as it reads $$ (buildkite-agent
    # 3.118.1), so the $$ written after a backslash would not arrive as $.
    with pytest.raises(DocumentError, match="backslash before a"):
        escaped(text)
    with pytest.raises(DocumentError, match="backslash before a"):
        resume_step(f"run {text}", COMMIT, ".buildkite/snapshot-pipeline.yml", [])


# ---------------------------------------------------------------------------
# What the plan step finds
# ---------------------------------------------------------------------------


def test_the_snapshot_downloads_are_the_entries_of_other_projects_without_a_build():
    doc = pipeline()
    found = snapshot_dependencies(doc, REST)
    assert all(p == e["project_id"] for p, e in found)
    assert [(e["project_id"], e["variant"]) for _, e in found] == [
        (QDB, LINUX),
        (DUCK, LINUX),
        (QDB, ARM),
        (DUCK, ARM),
    ], "the entry shared by two steps once; the own and the latest successful ones not"
    assert len(download_entries(doc)) == 6


def test_the_variants_each_snapshot_dependency_is_asked_for():
    assert needed_variants(pipeline(), REST, {}) == {
        QDB: frozenset({LINUX, ARM}),
        DUCK: frozenset({LINUX, ARM}),
    }


@pytest.mark.parametrize(
    "existing, needs",
    [
        ({ARM: pointer(9)}, {QDB: {LINUX}, DUCK: {LINUX}}),
        ({LINUX: pointer(9)}, {QDB: {LINUX, ARM}, DUCK: {ARM}}),
        ({LINUX: pointer(9), ARM: pointer(8)}, {QDB: {LINUX}}),
        ({DOCS: pointer(9)}, {QDB: {LINUX, ARM}, DUCK: {LINUX, ARM}}),
    ],
    ids=["arm exists", "linux exists", "both exist", "docs exists, not in D"],
)
def test_a_step_that_completion_replaces_needs_nothing(existing, needs):
    # Its downloads are gone from the uploaded pipeline (section 7, step 9).
    # The entry of quasardb-build linux that build-linux shares with
    # test-linux stays needed as long as test-linux is kept.
    found = needed_variants(pipeline(), REST, existing)
    assert found == {p: frozenset(v) for p, v in needs.items()}


@pytest.mark.parametrize(
    "existing",
    [{}, {ARM: pointer(9)}, {LINUX: pointer(9)}, {LINUX: pointer(9), ARM: pointer(8)}],
    ids=["nothing exists", "arm exists", "linux exists", "both exist"],
)
def test_the_needed_variants_are_the_builds_the_rewrite_takes(existing):
    doc = pipeline()
    needs = needed_variants(doc, REST, existing)
    deps = {(p, v): DEPENDENCIES[(p, v)] for p in needs for v in needs[p]}
    out = planned(doc, rewrite(existing=existing, dependencies=deps))
    assert snapshot_dependencies(out, REST) == [], "every snapshot entry kept names its build"
    written = {e["build_id"] for e in download_entries(out) if e.get("project_id") in (QDB, DUCK)}
    assert written == {p.build.id for p in deps.values()}, "and every build handed in is used"


@pytest.mark.parametrize(
    "variant, detail",
    [(None, "names no variant"), ("linux$x", "is not a variant"), (["x"], "names no variant")],
)
def test_a_snapshot_download_without_a_variant_of_its_form_is_an_error(variant, detail):
    # The agent hands a list over as other variables than the variant's.
    doc = pipeline()
    config(steps_of(doc)[0])["download"]["projects"][0]["variant"] = variant
    with pytest.raises(DocumentError, match=detail):
        needed_variants(doc, REST, {})


def test_a_download_block_given_twice_with_different_values_is_an_error():
    doc = pipeline()
    config(steps_of(doc)[0])["Download"] = {"clean": False}
    with pytest.raises(DocumentError, match="download is given twice with different values"):
        needed_variants(doc, REST, {})
    with pytest.raises(DocumentError, match="download is given twice with different values"):
        planned(doc, rewrite())


@pytest.mark.parametrize("value, name", [(7, "7"), (True, "true")])
def test_a_variant_is_the_text_the_plugin_receives(value, name):
    doc = pipeline()
    config(steps_of(doc)[0])["download"]["projects"][0]["variant"] = value
    assert name in needed_variants(doc, REST, {})[QDB]


def test_a_variant_with_a_variable_is_left_as_written_where_nothing_is_written_for_it():
    # Its form matters where the plan step names a pointer or a build by it.
    doc = pipeline()
    config(steps_of(doc)[2])["upload"]["variant"] = "docs-$$ARCH"
    config(steps_of(doc)[3])["download"]["projects"][0]["variant"] = "linux-$$ARCH"
    for r in (rewrite(key=None), rewrite(), rewrite(existing={LINUX: pointer(9)})):
        out = planned(doc, r)
        assert config(steps_of(out)[2])["upload"]["variant"] == "docs-$$ARCH"
        assert config(steps_of(out)[3])["download"]["projects"][0]["variant"] == "linux-$$ARCH"


def test_a_promoted_variant_not_of_the_form_of_one_is_an_error_only_with_a_key():
    # D names the pointers; without a key no pointer is written.
    doc = pipeline()
    config(steps_of(doc)[1])["promote"]["variant"] = "linux-$$ARCH"
    assert config(steps_of(planned(doc, rewrite(key=None)))[1])["promote"]["variant"] == (
        "linux-$$ARCH"
    )
    with pytest.raises(DocumentError, match="is not a variant"):
        planned(doc, rewrite())


def test_the_steps_that_promote_and_the_set_d():
    doc = pipeline()
    found = promotions(doc, REST)
    assert sorted(found) == [ARM, LINUX], "D: what the pipeline promotes; docs is only uploaded"
    assert found[LINUX] is steps_of(doc)[0]
    assert found[ARM] is steps_of(doc)[1]


def test_a_promote_block_of_another_project_or_without_a_variant_promotes_nothing_here():
    doc = pipeline()
    config(steps_of(doc)[0])["promote"]["project_id"] = DUCK
    del config(steps_of(doc)[1])["promote"]["variant"]
    assert promotions(doc, REST) == {}


def test_a_variant_promoted_by_two_steps_is_an_error_that_names_both():
    doc = pipeline()
    config(steps_of(doc)[1])["promote"]["variant"] = LINUX
    with pytest.raises(DocumentError) as e:
        promotions(doc, REST)
    assert str(e.value) == (
        "linux-core2-release is promoted by two steps, build-linux and build "
        "linux-aarch64-release: the first to pass would write its pointer while the other still "
        "uploads"
    )


def test_a_step_is_named_short_in_ascii_in_a_message():
    doc = pipeline()
    config(steps_of(doc)[1])["promote"]["variant"] = LINUX
    steps_of(doc)[1]["label"] = "caf" + chr(0xE9) + " " + "x" * 2000
    with pytest.raises(DocumentError) as e:
        promotions(doc, REST)
    assert "and caf\\xe9 xxx" in str(e.value) and str(e.value).isascii()
    assert len(str(e.value)) < 600


def test_a_variant_promoted_by_two_steps_is_refused_only_in_a_pipeline_with_a_snapshot():
    doc = pipeline()
    config(steps_of(doc)[1])["promote"]["variant"] = LINUX
    out = planned(doc, rewrite(key=None))
    assert config(steps_of(out)[1])["promote"]["variant"] == LINUX
    with pytest.raises(DocumentError, match="promoted by two steps"):
        planned(doc, rewrite())


def test_a_step_that_promotes_at_two_places_is_two_steps_that_promote_one_variant():
    # Buildkite runs a step for each place an alias puts it.
    doc = pipeline()
    doc["steps"].append({"group": "again", "steps": [steps_of(doc)[1]]})
    with pytest.raises(DocumentError) as e:
        promotions(doc, REST)
    assert str(e.value) == (
        "linux-aarch64-release is promoted by the step build linux-aarch64-release at two "
        "places, steps 2 and 6: the first to pass would write its pointer while the other still "
        "uploads"
    )
    with pytest.raises(DocumentError, match="at two places"):
        planned(doc, rewrite())


def test_without_a_key_no_step_gets_a_key():
    # Only the leave step of a build with a key asks for outcomes by key, and
    # a key given to a step at two places would be one key of two steps.
    doc = pipeline()
    doc["steps"].append({"group": "again", "steps": [steps_of(doc)[1]]})
    out = planned(doc, rewrite(key=None))
    assert "key" not in steps_of(out)[1] and "key" not in steps_of(out)[5]


def test_a_promoting_step_without_a_key_gets_one_counted_in_document_order():
    doc = pipeline()
    second = build_step(None, "linux-haswell-release", [])
    doc["steps"].append({"group": "more", "steps": [second]})
    keyed = with_promote_keys(doc, REST)
    assert [s.get("key") for s in steps_of(keyed)] == [
        "build-linux",
        "snapshot-promote-1",
        "docs",
        "test-linux",
        "report",
        "snapshot-promote-2",
    ], "only steps that promote, also inside a group"
    assert "key" not in steps_of(doc)[1], "the document handed in is not changed"
    assert promote_keys(keyed, REST) == {
        LINUX: "build-linux",
        ARM: "snapshot-promote-1",
        "linux-haswell-release": "snapshot-promote-2",
    }
    with pytest.raises(DocumentError, match="has no key"):
        promote_keys(doc, REST)


def test_a_step_produces_what_its_upload_or_promote_block_names():
    doc = pipeline()
    linux, _, docs, test, _ = steps_of(doc)
    assert produced_variants(linux, REST) == {LINUX}
    assert produced_variants(docs, REST) == {DOCS}
    assert produced_variants(test, REST) == frozenset()
    config(linux)["upload"]["variant"] = "linux-debug"
    assert produced_variants(linux, REST) == {LINUX, "linux-debug"}


# ---------------------------------------------------------------------------
# What the plan step writes
# ---------------------------------------------------------------------------


def test_a_snapshot_entry_gets_the_build_and_ref_of_its_pointer_and_strict_matching():
    doc = pipeline()
    duck = config(steps_of(doc)[0])["download"]["projects"][1]
    duck["git-ref"] = duck.pop("git_ref")
    duck["Require-Strict-Ref-Match"] = False
    out = planned(doc, rewrite())
    assert config(steps_of(out)[0])["download"]["projects"][1] == {
        "project_id": DUCK,
        "variant": LINUX,
        "files": ["*.tar.zst"],
        "build_id": pointer(3).build.id,
        "git_ref": "refs/heads/snapshot/feature",
        "require_strict_ref_match": True,
    }, "the ref and strict matching in any spelling are replaced"


def test_a_latest_successful_entry_and_an_own_entry_of_a_kept_step_are_left_as_generated():
    out = planned(pipeline(), rewrite())
    projects = config(steps_of(out)[3])["download"]["projects"]
    assert projects[0] == entry(None, LINUX)
    assert projects[1] == entry(GO, LINUX, build_id="LATEST_SUCCESSFUL")


def test_with_a_key_every_upload_and_promote_block_of_the_own_project_gets_it():
    doc = pipeline()
    config(steps_of(doc)[1])["upload"]["Snapshot"] = "v1-old"
    config(steps_of(doc)[2])["upload"]["project_id"] = DUCK
    out = planned(doc, rewrite())
    linux, arm, docs, _, _ = (config(s) if "plugins" in s else None for s in steps_of(out))
    assert linux["upload"]["snapshot"] == linux["promote"]["snapshot"] == KEY
    assert arm["upload"] == {
        "files": ["artifacts/*"],
        "git_ref": MASTER,
        "variant": ARM,
        "snapshot": KEY,
    }
    assert "snapshot" not in docs["upload"], "an upload of another project"


def test_without_a_key_no_block_gets_one():
    out = planned(pipeline(), rewrite(key=None))
    blocks = [b for s in steps_of(out) if "plugins" in s for b in config(s).values()]
    assert len(blocks) == 8 and all("snapshot" not in b for b in blocks)


def test_every_reference_to_the_plugin_names_the_plugin_commit():
    doc = pipeline()
    doc["steps"][2]["plugins"] = [{"bureau14/qdb-artifacts": {"upload": {"variant": DOCS}}}]
    out = planned(doc, rewrite())
    named = [k for s in steps_of(out) for p in s.get("plugins", []) for k in p]
    assert named == [
        "docker#v5.13.0",
        f"bureau14/qdb-artifacts#{COMMIT}",
        "docker#v5.13.0",
        f"bureau14/qdb-artifacts#{COMMIT}",
        f"bureau14/qdb-artifacts#{COMMIT}",
        f"bureau14/qdb-artifacts#{COMMIT}",
    ]


def test_two_references_to_the_plugin_in_one_mapping_are_an_error():
    doc = pipeline()
    plugins = doc["steps"][2]["plugins"][0]
    plugins["bureau14/qdb-artifacts#v2"] = {}
    with pytest.raises(DocumentError, match="two keys of one mapping name the plugin"):
        planned(doc, rewrite())


def test_every_value_written_has_its_dollars_doubled():
    deps = {**DEPENDENCIES, (QDB, LINUX): pointer(1, "refs/heads/snapshot/x/${TOKEN}")}
    out = planned(pipeline(), rewrite(dependencies=deps))
    assert config(steps_of(out)[0])["download"]["projects"][0]["git_ref"] == (
        "refs/heads/snapshot/x/$${TOKEN}"
    )


def test_a_snapshot_entry_without_a_build_handed_in_is_an_error():
    deps = {k: v for k, v in DEPENDENCIES.items() if k != (DUCK, ARM)}
    with pytest.raises(DocumentError, match="no build of qdb-duck linux-aarch64-release"):
        planned(pipeline(), rewrite(dependencies=deps))


def test_a_shared_entry_is_rewritten_once_and_stays_shared():
    out = planned(pipeline(), rewrite())
    in_build = config(steps_of(out)[0])["download"]["projects"][0]
    in_test = config(steps_of(out)[3])["download"]["projects"][2]
    assert in_build is in_test and in_build["build_id"] == pointer(1).build.id
    again = load_document(dump_document(out))
    assert (
        config(steps_of(again)[0])["download"]["projects"][0]
        is (config(steps_of(again)[3])["download"]["projects"][2])
    )


def test_the_document_handed_in_is_not_changed():
    doc = pipeline()
    before = copy.deepcopy(doc)
    planned(doc, rewrite(existing={LINUX: pointer(9)}))
    assert doc == before


# ---------------------------------------------------------------------------
# Completion: the steps of variants that exist are replaced
# ---------------------------------------------------------------------------


def test_a_step_whose_variants_all_exist_is_replaced_in_its_place():
    # The replacement keeps what decides whether and when the step runs, so
    # the build flows as generated (section 8).
    doc = pipeline()
    steps_of(doc)[0]["if"] = "build.branch != 'x'"
    steps_of(doc)[0]["depends_on"] = ["lint"]
    steps_of(doc)[0]["allow_dependency_failure"] = True
    steps_of(doc)[0]["branches"] = "master 3.14.x"
    out = planned(doc, rewrite(existing={LINUX: pointer(9)}))
    assert out["steps"][0]["group"] == "Linux"
    assert out["steps"][0]["steps"][0] == {
        "label": "build linux-core2-release (exists in build 9)",
        "key": "build-linux",
        "depends_on": ["lint"],
        "allow_dependency_failure": True,
        "if": "build.branch != 'x'",
        "branches": "master 3.14.x",
        "command": (
            'echo "linux-core2-release of qdb-api-rest exists in build 9 https://ci.example/b/9"'
        ),
    }
    assert config(steps_of(out)[1])["promote"]["variant"] == ARM, "the other build is kept"


def test_a_replaced_step_is_out_of_the_promotions_and_keeps_its_given_key():
    out = planned(pipeline(), rewrite(existing={ARM: pointer(9)}))
    assert steps_of(out)[1]["key"] == "snapshot-promote-1"
    assert "plugins" not in steps_of(out)[1]
    assert promote_keys(out, REST) == {LINUX: "build-linux"}


def test_a_step_with_a_variant_that_does_not_exist_or_is_not_promoted_is_kept():
    doc = pipeline()
    config(steps_of(doc)[0])["upload"]["variant"] = DOCS
    out = planned(doc, rewrite(existing={LINUX: pointer(9), DOCS: pointer(9)}))
    assert "plugins" in steps_of(out)[0], "docs is uploaded, not promoted: not in D"
    assert "plugins" in steps_of(out)[2]


def test_a_step_that_produces_nothing_is_kept():
    out = planned(pipeline(), rewrite(existing={LINUX: pointer(9), ARM: pointer(8)}))
    assert steps_of(out)[4] == {"key": "report", "depends_on": ["build-linux"], "command": "true"}


def test_a_replaced_step_of_variants_in_two_builds_names_both():
    doc = pipeline()
    config(steps_of(doc)[0])["upload"]["variant"] = ARM
    out = planned(doc, rewrite(existing={LINUX: pointer(9), ARM: pointer(7)}))
    replaced = steps_of(out)[0]
    assert replaced["label"] == "build linux-core2-release (exists in builds 7, 9)"
    assert replaced["command"].splitlines() == [
        'echo "linux-aarch64-release of qdb-api-rest exists in build 7 https://ci.example/b/7"',
        'echo "linux-core2-release of qdb-api-rest exists in build 9 https://ci.example/b/9"',
    ]


def test_an_own_download_of_a_variant_whose_uploading_steps_were_all_replaced_gets_its_build():
    out = planned(pipeline(), rewrite(existing={LINUX: pointer(9, "refs/heads/feature")}))
    own = config(steps_of(out)[3])["download"]["projects"][0]
    assert own == {
        "variant": LINUX,
        "files": ["*.tar.zst"],
        "build_id": pointer(9).build.id,
        "git_ref": "refs/heads/feature",
        "require_strict_ref_match": True,
    }


def test_an_own_download_of_a_variant_still_uploaded_is_left_as_generated():
    doc = pipeline()
    doc["steps"].append(build_step("rebuild-linux", LINUX, []))
    config(doc["steps"][-1])["promote"]["variant"] = "linux-again"
    out = planned(doc, rewrite(existing={LINUX: pointer(9)}))
    assert config(steps_of(out)[3])["download"]["projects"][0] == entry(None, LINUX)


def test_existing_variants_without_a_key_are_refused():
    with pytest.raises(ValueError, match="key"):
        rewrite(key=None, existing={LINUX: pointer(9)})


def test_a_rewrite_of_its_own_output_changes_nothing():
    r = rewrite(existing={LINUX: pointer(9)})
    once = planned(pipeline(), r)
    assert planned(once, r) == once


def test_a_rewrite_with_nothing_to_write_changes_no_value():
    doc = load_document((FIXTURES / "quasardb.yml").read_text(encoding="utf-8"))
    at_commit = load_document(dump_document(doc).replace("#master", f"#{COMMIT}"))
    nothing = Rewrite("quasardb-build", None, COMMIT, {}, {})
    assert planned(at_commit, nothing) == at_commit


@pytest.mark.parametrize(
    "name, own",
    [
        ("qdb-api-go.yml", GO),
        ("qdb-api-python.yml", PYTHON),
        ("qdb-api-rest.yml", REST),
        ("qdb-duck.yml", DUCK),
        ("quasardb.yml", "quasardb-build"),
    ],
)
def test_a_generated_pipeline_is_planned_with_every_value_it_needs(name, own):
    doc = load_document((FIXTURES / name).read_text(encoding="utf-8"))
    needs = needed_variants(doc, own, {})
    deps = {(p, v): pointer(i) for i, (p, v) in enumerate((p, v) for p in needs for v in needs[p])}
    out = planned(doc, Rewrite(own, KEY, COMMIT, deps, {}))
    assert snapshot_dependencies(out, own) == [], "every snapshot entry now names its build"
    assert sorted(promote_keys(out, own)) == sorted(promotions(doc, own))
    assert load_document(dump_document(out)) == out


# ---------------------------------------------------------------------------
# Trigger and resume
# ---------------------------------------------------------------------------


def chain_plan():
    """The diamond on a feature branch; qdb-duck runs under snapshot/feature."""
    p = plan(label="feature")
    return replace(p, nodes={**p.nodes, DUCK: replace(p.nodes[DUCK], label="snapshot/feature")})


def test_a_trigger_step_builds_one_dependency_of_the_plan():
    [step] = trigger_steps(chain_plan(), {QDB: [ARM, LINUX]})
    assert step == {
        "label": f":link: quasardb-build @ {sha(QDB)[:12]}",
        "key": "snapshot-dep-quasardb-build",
        "trigger": QDB,
        "soft_fail": True,
        "build": {
            "branch": "feature",
            "commit": sha(QDB),
            "message": "snapshot dependency of qdb-api-python build 512",
            "env": {
                "SNAPSHOT_PLAN": PLAN_ID,
                "SNAPSHOT_NEEDS": '["linux-aarch64-release", "linux-core2-release"]',
            },
        },
    }
    assert ":" not in step["build"]["message"]


def test_a_trigger_step_waits_for_the_triggers_of_dependencies_in_its_tree():
    triggers = trigger_steps(chain_plan(), {DUCK: [LINUX], QDB: [LINUX]})
    assert [(t["key"], t.get("depends_on")) for t in triggers] == [
        ("snapshot-dep-quasardb-build", None),
        ("snapshot-dep-qdb-duck", ["snapshot-dep-quasardb-build"]),
    ], "dependencies first"
    assert triggers[1]["build"]["branch"] == "snapshot/feature"


def test_a_trigger_step_hands_down_the_variants_sorted_and_once():
    [step] = trigger_steps(chain_plan(), {QDB: [LINUX, ARM, LINUX]})
    assert step["build"]["env"]["SNAPSHOT_NEEDS"] == (
        '["linux-aarch64-release", "linux-core2-release"]'
    )


def test_a_trigger_step_does_not_wait_for_a_dependency_outside_its_tree():
    # qdb-api-rest and qdb-duck are both below the root, neither below the other.
    p = chain_plan()
    apart = {
        PYTHON: replace(p.nodes[PYTHON], deps=(REST, DUCK)),
        REST: replace(p.nodes[REST], deps=()),
        DUCK: replace(p.nodes[DUCK], deps=()),
    }
    p = replace(p, nodes={k: v for k, v in {**p.nodes, **apart}.items() if k != QDB})
    triggers = trigger_steps(p, {DUCK: [LINUX], REST: [LINUX]})
    assert [(t["key"], t.get("depends_on")) for t in triggers] == [
        ("snapshot-dep-qdb-api-rest", None),
        ("snapshot-dep-qdb-duck", None),
    ]


def test_a_trigger_step_has_every_dollar_of_its_values_doubled():
    p = chain_plan()
    p = replace(p, nodes={**p.nodes, QDB: replace(p.nodes[QDB], label="x/${TOKEN}")})
    [step] = trigger_steps(p, {QDB: [LINUX]})
    assert step["build"]["branch"] == "x/$${TOKEN}"


def test_a_dependency_that_is_not_in_the_plan_cannot_be_triggered():
    with pytest.raises(DocumentError, match="no-such-pipeline is not a node of plan"):
        trigger_steps(chain_plan(), {"no-such-pipeline": [LINUX]})


COMMAND = "python3 -m venv .env\n.env/bin/python3 .buildkite/pipeline.py generate > $OUT"


def test_the_resume_step_waits_for_every_trigger_and_runs_the_enter_steps_command():
    step = resume_step(COMMAND, COMMIT, ".buildkite/snapshot-pipeline.yml", ["snapshot-dep-a"])
    assert step == {
        "label": ":camera_with_flash: snapshot resume",
        "key": "snapshot-resume",
        "priority": 100,
        "timeout_in_minutes": 15,
        "depends_on": [{"step": "snapshot-dep-a", "allow_failure": True}],
        "command": COMMAND.replace("$OUT", "$$OUT"),
        "plugins": [
            {f"bureau14/qdb-artifacts#{COMMIT}": {"resume": ".buildkite/snapshot-pipeline.yml"}}
        ],
    }


def test_the_resume_pipeline_is_the_triggers_and_the_resume_step():
    doc = resume_pipeline(
        chain_plan(), {DUCK: [LINUX], QDB: [LINUX]}, COMMAND, COMMIT, ".buildkite/p.yml"
    )
    assert [s["key"] for s in doc["steps"]] == [
        "snapshot-dep-quasardb-build",
        "snapshot-dep-qdb-duck",
        "snapshot-resume",
    ]
    assert [d["step"] for d in doc["steps"][2]["depends_on"]] == [
        "snapshot-dep-quasardb-build",
        "snapshot-dep-qdb-duck",
    ]
    assert load_document(dump_written(doc)) == doc


def yaml_nodes(node) -> Iterator:
    """Every node of a composed YAML document, keys and values."""
    yield node
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            yield from yaml_nodes(k)
            yield from yaml_nodes(v)
    elif isinstance(node, yaml.SequenceNode):
        for v in node.value:
            yield from yaml_nodes(v)


def string_styles(text: str) -> set:
    """The styles of every string scalar of a YAML text, keys and values."""
    return {
        n.style
        for n in yaml_nodes(yaml.compose(text))
        if isinstance(n, yaml.ScalarNode) and n.tag == "tag:yaml.org,2002:str"
    }


def test_the_resume_pipeline_is_written_with_every_string_quoted():
    # The agent reads YAML 1.2, in which some strings PyYAML writes plain are
    # numbers: a commit of digits with a leading 0, or of digits, an e and
    # digits, and a branch named 1e3 (measured on buildkite-agent 3.118.1).
    p = chain_plan()
    numbers = {QDB: ("0" + "1" * 39, "1e3"), DUCK: ("0e" + "2" * 38, "snapshot/feature")}
    nodes = {k: replace(p.nodes[k], commit=c, label=b) for k, (c, b) in numbers.items()}
    p = replace(p, nodes={**p.nodes, **nodes})
    doc = resume_pipeline(p, {DUCK: [LINUX], QDB: [LINUX]}, COMMAND, COMMIT, ".buildkite/p.yml")
    text = dump_written(doc)
    assert string_styles(text) == {'"'}
    assert f'"commit": "0{"1" * 39}"' in text and '"branch": "1e3"' in text
    assert load_document(text) == doc
    assert isinstance(yaml.safe_load(text)["steps"][0]["soft_fail"], bool)
