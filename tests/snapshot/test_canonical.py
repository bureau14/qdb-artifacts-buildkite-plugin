from __future__ import annotations

from lib.snapshot.canonical import canonical_json, snapshot_key

C1 = "1" * 40
C2 = "2" * 40
PLAN = "01890000-aaaa-7000-8000-000000000001"


def test_canonical_json_sorts_keys_and_has_no_whitespace():
    assert canonical_json({"b": 1, "a": [1, {"d": None, "c": True}]}) == (
        b'{"a":[1,{"c":true,"d":null}],"b":1}'
    )


def test_canonical_json_is_utf8_not_escaped():
    assert canonical_json({"k": chr(0xE9)}) == b'{"k":"\xc3\xa9"}'


# The two known keys below are the first pass's, with their expected values
# unchanged: the formula did not change, so a key computed by the first pass
# is the same.


def test_snapshot_key_without_dependencies():
    # sha256('{"commit":"111...1","deps":[],"project":"quasardb-build"}')
    assert snapshot_key("quasardb-build", C1, {}) == "v1-fdf8f768f22318b1f46a33c384f76a03"


def test_snapshot_key_sorts_dependencies_by_project():
    deps = {
        "quasardb-build": "v1-fdf8f768f22318b1f46a33c384f76a03",
        "qdb-duck": "v1-" + "a" * 32,
    }
    assert snapshot_key("qdb-api-rest", C2, deps) == "v1-a2075e366014c5cae592fcd00b345234"


def test_snapshot_key_changes_with_commit_and_with_any_dependency_key():
    base = snapshot_key("p", C1, {"d": "v1-x"})
    assert snapshot_key("p", C2, {"d": "v1-x"}) != base
    assert snapshot_key("p", C1, {"d": "v1-y"}) != base
    assert snapshot_key("q", C1, {"d": "v1-x"}) != base


def test_snapshot_key_of_a_forced_build_carries_the_plan_id():
    # sha256('{"commit":"111...1","deps":[],"project":"quasardb-build","rebuild":"<plan>"}')
    forced = snapshot_key("quasardb-build", C1, {}, rebuild=PLAN)
    assert forced == "v1-618c15b6206f127e0a83ffe93c5583d2"
    assert forced != snapshot_key("quasardb-build", C1, {})
    assert forced != snapshot_key("quasardb-build", C1, {}, rebuild=PLAN[:-1] + "f")


def test_snapshot_key_without_rebuild_is_the_first_pass_key():
    assert snapshot_key("quasardb-build", C1, {}, rebuild=None) == snapshot_key(
        "quasardb-build", C1, {}
    )
