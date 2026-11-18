"""The plugin's configuration as written in a document, read by its structure."""

from __future__ import annotations

import pytest
import yaml

from lib.snapshot.configuration import (
    EntryError,
    agent_key,
    download_entries,
    is_plugin_key,
    key_names,
    misnamed_plugins,
    names_plugin_another_way,
    plugin_block,
)

QDB = "quasardb-build"
DUCK = "qdb-duck"
PLUGIN = "bureau14/qdb-artifacts#master"

PLUGIN_KEY, ANOTHER_WAY, NOT_THE_PLUGIN = "the plugin", "another way", "not the plugin"


@pytest.mark.parametrize(
    "key, kind",
    [
        ("bureau14/qdb-artifacts#master", PLUGIN_KEY),
        ("bureau14/qdb-artifacts", PLUGIN_KEY),
        ("bureau14/qdb-artifacts#" + "a" * 40, PLUGIN_KEY),
        ("bureau14/qdb-artifacts#feature#x", PLUGIN_KEY),
        ("bureau14/qdb-artifacts-buildkite-plugin#master", ANOTHER_WAY),
        ("github.com/bureau14/qdb-artifacts#master", ANOTHER_WAY),
        ("https://github.com/bureau14/qdb-artifacts-buildkite-plugin.git#v1", ANOTHER_WAY),
        ("Bureau14/QDB-Artifacts#master", ANOTHER_WAY),
        ("bureau14/qdb-artifacts-extra#master", ANOTHER_WAY),
        (" bureau14/qdb-artifacts#master", ANOTHER_WAY),
        ("bureau14/qdb-test-report#master", NOT_THE_PLUGIN),
        ("bureau14/qdb_artifacts#master", NOT_THE_PLUGIN),
        ("x#bureau14/qdb-artifacts", NOT_THE_PLUGIN),
        (1, NOT_THE_PLUGIN),
    ],
)
def test_plugin_key(key, kind):
    assert is_plugin_key(key) is (kind == PLUGIN_KEY)
    assert names_plugin_another_way(key) is (kind == ANOTHER_WAY)


@pytest.mark.parametrize(
    "key, name",
    [
        ("project_id", "PROJECT_ID"),
        ("project-id", "PROJECT_ID"),
        ("Project-Id", "PROJECT_ID"),
        ("PROJECT_ID", "PROJECT_ID"),
        ("project id", "PROJECT_ID"),
        ("project\tid", "PROJECT_ID"),
        ("project\nid", "PROJECT_ID"),
        ("project\fid", "PROJECT_ID"),
        ("project\rid", "PROJECT_ID"),
        ("project\vid", "PROJECT\vID"),
        ("project\x85id", "PROJECT\x85ID"),
        ("project\xa0id", "PROJECT\xa0ID"),
        ("project" + chr(0x2028) + "id", "PROJECT" + chr(0x2028) + "ID"),
        ("project" + chr(0x3000) + "id", "PROJECT" + chr(0x3000) + "ID"),
        ("project\x1cid", "PROJECT\x1cID"),
        ("trail ", "TRAIL_"),
        ("a.b", "A.B"),
        ("a/b", "A/B"),
    ],
)
def test_an_option_key_is_named_as_the_agent_names_it(key, name):
    # Each pair as buildkite-agent 3.118.1 named the key in a plugin's
    # environment: white space is Go's \s (no \v, ASCII only).
    assert agent_key(key) == name


@pytest.mark.parametrize(
    "key, value, names",
    [
        ("project_id", "a", {"PROJECT_ID"}),
        ("project  id", "a", {"PROJECT__ID", "PROJECT_ID"}),
        ("project__id", "a", {"PROJECT__ID", "PROJECT_ID"}),
        (" lead", "a", {"_LEAD", "LEAD"}),
        ("a--b", "a", {"A__B", "A_B"}),
        ("-x-", "a", {"_X_", "X_"}),
        ("project_id_", "a", {"PROJECT_ID_"}),
        ("download_", {"projects": []}, {"DOWNLOAD_", "DOWNLOAD"}),
        ("_projects", [{"project_id": "a"}], {"_PROJECTS", "PROJECTS"}),
        ("projects ", [{"project_id": "a"}], {"PROJECTS_", "PROJECTS"}),
        ("-", {"download": {}}, {"_", ""}),
        (7, "a", set()),
    ],
)
def test_the_agent_also_names_a_key_with_every_run_of_underscores_made_one(key, value, names):
    # The agent joins the names on the way with "_"; when the whole name holds
    # a run of "_", it also sets the name with every run made one. So a key's
    # "_" at its start joins the one before it, and at its end the one after
    # it when options follow.
    assert key_names(key, value) == frozenset(names)


@pytest.mark.parametrize(
    "config, entries",
    [
        ({"download": {"projects": {"0": {"project_id": DUCK}}}}, []),
        ({"download": {"projects_0": {"project_id": DUCK}}}, []),
        ({"download_projects_0_project_id": DUCK}, []),
        ({"download": {"-": {"projects": [{"project_id": DUCK}]}}}, []),
        (
            {"download": {"projects": [{"project_id": QDB}], "projects_0_project_id": DUCK}},
            [{"project_id": QDB}],
        ),
    ],
    ids=[
        "projects as a mapping",
        "an entry under a key with _",
        "an option of an entry at the top",
        "a key of separators only",
        "an option beside the entries",
    ],
)
def test_a_value_at_another_path_than_the_structure_is_not_read(config, entries):
    # The agent names a variable by the whole path to a value, so each of
    # these reaches the plugin as an option of its first download entry
    # (buildkite-agent 3.118.1). No template writes one.
    doc = {PLUGIN: config}
    assert download_entries(doc) == entries
    assert misnamed_plugins(doc) == []


@pytest.mark.parametrize("block", ["download", "upload", "promote"])
def test_a_block_given_twice_with_different_values_is_an_error(block):
    config = {block: {"variant": "v"}, block.upper(): {"git_ref": "r"}}
    with pytest.raises(EntryError) as e:
        plugin_block(config, block)
    assert str(e.value) == (
        f"{block} is given twice with different values: {{'variant': 'v'}} as {block!r} and "
        f"{{'git_ref': 'r'}} as {block.upper()!r}"
    )


@pytest.mark.parametrize(
    "config, option",
    [
        ({"download": {"projects": []}, "Download": {"clean": True}}, "download"),
        ({"download": {"projects": [{"project_id": QDB}], "PROJECTS": []}}, "projects"),
    ],
)
def test_download_entries_that_cannot_be_read_are_an_error(config, option):
    with pytest.raises(EntryError, match=f"^{option} is given twice with different values: "):
        download_entries({"steps": [{"plugins": [{PLUGIN: config}]}]})


def test_one_value_given_two_ways_is_one_option():
    config = {"download": {"projects": [{"project_id": "a", "project-id": "a", "variant": "v"}]}}
    assert download_entries({PLUGIN: config}) == [config["download"]["projects"][0]]


ALIASED = """\
common: &common
  bureau14/qdb-artifacts#master:
    download: {projects: [{project_id: quasardb-build}]}
loop: &loop [*loop, *common]
steps:
  - plugins: [*common]
  - plugins: [*common]
"""


def test_aliases_in_a_parsed_document_neither_loop_nor_repeat():
    assert download_entries(yaml.safe_load(ALIASED)) == [{"project_id": QDB}]


def test_one_entry_reached_many_ways_is_found_once():
    # As aliases parse: n plugin keys share one configuration whose list holds
    # the one entry n times; n * n entries if each way counted.
    entry = {"project_id": QDB}
    config = {"download": {"projects": [entry] * 300}}
    doc = {"steps": [{"plugins": {f"bureau14/qdb-artifacts#k{i}": config for i in range(300)}}]}
    assert download_entries(doc) == [entry]
