"""Failure records: the value, its JSON and its checks, and the decisions of section 7."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from plans import DUCK, PLAN_ID, QDB, REST, T0, key

from lib.pointer import Build
from lib.snapshot.records import (
    GENERATOR_FAILED,
    HARD_FAILED,
    NOT_DECLARED,
    REASONS,
    RULE,
    SHARED_AFTER,
    Cause,
    Failed,
    Origin,
    Record,
    RecordError,
    caused_record,
    chain_failure,
    compose_record,
    decode_record,
    encode_record,
    leave_record,
    reason_record,
    shared_failure,
    shared_record,
)

OWN_BUILD = Build("01890000-aaaa-7000-8000-00000000000a", 41, "https://ci.example/qdb-duck/41")
OTHER_BUILD = Build("01890000-aaaa-7000-8000-00000000000b", 40, "https://ci.example/qdb-duck/40")
OTHER_PLAN = "01890000-aaaa-7000-8000-0000000000ff"
OWN = Origin(DUCK, key(DUCK), PLAN_ID, OWN_BUILD)
LINUX, FREEBSD, ARM = "linux-core2-release", "freebsd-haswell-release", "linux-aarch64-release"


def failed_record(
    *failed: Failed, build: Build = OTHER_BUILD, at: float = T0, plan: str = PLAN_ID
) -> Record:
    return compose_record(Origin(DUCK, key(DUCK), plan, build), at, None, failed, (), None)


# ---------------------------------------------------------------------------
# The value and its JSON
# ---------------------------------------------------------------------------


def test_the_document_is_compact_json_with_sorted_keys():
    r = leave_record(OWN, {LINUX: "build-linux"}, {"build-linux": HARD_FAILED}, set(), T0)
    assert encode_record(r) == (
        '{"build":{"id":"01890000-aaaa-7000-8000-00000000000a","number":41,'
        '"pipeline":"qdb-duck","url":"https://ci.example/qdb-duck/41"},'
        '"caused_by":[],'
        '"failed":[{"outcome":"hard_failed","step":"build-linux","variant":"linux-core2-release"}],'
        f'"key":"{key(DUCK)}","plan":"{PLAN_ID}","project":"qdb-duck","reason":null,'
        '"recorded_at":"2025-11-17T12:00:00.000Z","recorded_at_epoch":1763380800,'
        '"schema":1,"shared_with":null}'
    )


def every_kind() -> list[Record]:
    source = failed_record(Failed(LINUX, "build-linux", HARD_FAILED))
    return [
        reason_record(OWN, GENERATOR_FAILED, T0),
        reason_record(OWN, RULE, T0 + 0.5),
        reason_record(OWN, NOT_DECLARED, T0),
        failed_record(Failed(LINUX, "build-linux", HARD_FAILED), Failed(ARM, "b", "")),
        caused_record(OWN, [Cause(QDB, key(QDB), (LINUX, ARM))], T0),
        shared_record(OWN, source, {LINUX}, T0 + 60),
    ]


@pytest.mark.parametrize("r", every_kind(), ids=lambda r: r.reason or "entries")
def test_a_record_reads_back_as_it_was_written(r):
    assert decode_record(encode_record(r)) == r


def document(**changes) -> dict:
    d = json.loads(encode_record(failed_record(Failed(LINUX, "build-linux", HARD_FAILED))))
    d.update(changes)
    return d


BUILD_DOC = {"id": OTHER_BUILD.id, "number": 40, "url": OTHER_BUILD.url, "pipeline": DUCK}


@pytest.mark.parametrize(
    "d, detail",
    [
        ([], "not a failure record of schema 1"),
        (document(schema=2), "not a failure record of schema 1"),
        (document(schema=True), "not a failure record of schema 1"),
        (document(schema=1.0), "not a failure record of schema 1"),
        (document(project="Qdb-Duck"), "record.project"),
        (document(key="v1-x"), "record.key"),
        (document(plan="$(x)"), "record.plan"),
        (document(build={**BUILD_DOC, "id": "x"}), "record.build.id"),
        (document(build={**BUILD_DOC, "number": "40"}), "record.build.number"),
        (document(build={**BUILD_DOC, "number": True}), "record.build.number"),
        (document(build={**BUILD_DOC, "number": -1}), "record.build.number"),
        (document(build={**BUILD_DOC, "url": "http://ci.example/40"}), "record.build.url"),
        (document(build={**BUILD_DOC, "pipeline": "a b"}), "record.build.pipeline"),
        (document(build={**BUILD_DOC, "pipeline": QDB}), "record.build.pipeline"),
        (document(reason="tired"), "record.reason"),
        (document(failed=[{"variant": "a/b", "step": "s", "outcome": ""}]), "variant"),
        (document(failed=[{"variant": "v", "step": "", "outcome": ""}]), "step"),
        (document(failed=[{"variant": "v", "step": "a b", "outcome": ""}]), "step"),
        (document(failed=[{"variant": "v", "step": "s", "outcome": "Failed!"}]), "outcome"),
        (document(failed={}), "record.failed"),
        (document(caused_by=[{"project": QDB, "key": key(QDB), "variants": []}]), "variants"),
        (document(caused_by=[{"project": QDB, "key": "x", "variants": [LINUX]}]), "key"),
        (document(shared_with={**BUILD_DOC, "url": "x"}), "record.shared_with.url"),
        (document(shared_with={**BUILD_DOC, "pipeline": QDB}), "record.shared_with.pipeline"),
        (document(recorded_at="yesterday"), "record.recorded_at"),
        (document(recorded_at_epoch=1.5), "record.recorded_at_epoch"),
        (document(failed=[]), "states no failure"),
    ],
)
def test_every_field_of_a_record_is_checked(d, detail):
    with pytest.raises(RecordError, match=detail) as e:
        decode_record(json.dumps(d))
    assert all(" " <= c <= "~" for c in str(e.value)), str(e.value)


def test_text_that_is_not_json_is_refused():
    with pytest.raises(RecordError, match="not JSON"):
        decode_record("{")
    with pytest.raises(RecordError, match="^record: not "):
        decode_record("[" * 100_000 + "]" * 100_000)


def test_a_record_that_states_no_failure_cannot_be_composed():
    # A record is written once and never replaced: one that no reader accepts
    # would stand in the bucket for good.
    source = failed_record(Failed(LINUX, "build-linux", HARD_FAILED))
    with pytest.raises(RecordError, match="states no failure"):
        shared_record(OWN, source, set(), T0)
    with pytest.raises(RecordError, match="states no failure"):
        shared_record(OWN, source, {ARM}, T0)
    with pytest.raises(RecordError, match="states no failure"):
        caused_record(OWN, [], T0)
    with pytest.raises(RecordError, match="states no failure"):
        compose_record(OWN, T0, None, (), (), None)


def test_the_reasons():
    assert REASONS == ("generator_failed", "rule", "not_declared")
    with pytest.raises(ValueError, match="reason"):
        reason_record(OWN, "tired", T0)


# ---------------------------------------------------------------------------
# Leave: the record of the variants whose step did not pass
# ---------------------------------------------------------------------------

PROMOTES = {LINUX: "build-linux", FREEBSD: "build-freebsd", ARM: "build-arm"}


@pytest.mark.parametrize("outcome", [HARD_FAILED, "soft_failed", "neutral", ""])
def test_a_variant_whose_step_did_not_pass_is_recorded_with_its_outcome(outcome):
    outcomes = {"build-linux": "passed", "build-freebsd": outcome, "build-arm": "passed"}
    r = leave_record(OWN, PROMOTES, outcomes, set(), T0)
    assert r is not None and r.failed == (Failed(FREEBSD, "build-freebsd", outcome),)
    assert (r.reason, r.caused_by, r.shared_with, r.plan) == (None, (), None, PLAN_ID)


def test_a_step_that_did_not_finish_has_the_empty_outcome():
    r = leave_record(OWN, PROMOTES, {"build-linux": "passed", "build-arm": "passed"}, set(), T0)
    assert r is not None and r.failed == (Failed(FREEBSD, "build-freebsd", ""),)


def test_a_variant_with_a_pointer_is_not_recorded_whatever_its_step_says():
    # A job retried later writes its pointer itself: the pointer decides.
    outcomes = {"build-linux": HARD_FAILED, "build-freebsd": HARD_FAILED, "build-arm": "passed"}
    r = leave_record(OWN, PROMOTES, outcomes, {LINUX}, T0)
    assert r is not None and [f.variant for f in r.failed] == [FREEBSD]


def test_nothing_is_recorded_when_every_step_passed_or_every_variant_exists():
    passed = dict.fromkeys(PROMOTES.values(), "passed")
    assert leave_record(OWN, PROMOTES, passed, set(), T0) is None
    assert leave_record(OWN, PROMOTES, {}, set(PROMOTES), T0) is None


def test_failed_entries_are_sorted_by_variant():
    r = leave_record(OWN, PROMOTES, {}, set(), T0)
    assert r is not None and [f.variant for f in r.failed] == sorted(PROMOTES)


# ---------------------------------------------------------------------------
# Step 8: a failure shared with a build of the same key that this build waited behind
# ---------------------------------------------------------------------------

ARRIVED = int(T0)


def test_a_record_after_the_arrival_with_every_wanted_variant_hard_failed_is_shared():
    r = failed_record(
        Failed(LINUX, "build-linux", HARD_FAILED),
        Failed(ARM, "build-arm", HARD_FAILED),
        at=ARRIVED + SHARED_AFTER + 1,
    )
    assert shared_failure([r], OWN_BUILD.id, ARRIVED, {LINUX, ARM}) == r
    assert shared_failure([r], OWN_BUILD.id, ARRIVED, {LINUX}) == r


def test_a_record_is_shared_only_when_recorded_more_than_5_seconds_after_the_arrival():
    assert SHARED_AFTER == 5
    for at in (ARRIVED - 100, ARRIVED, ARRIVED + SHARED_AFTER):
        r = failed_record(Failed(LINUX, "build-linux", HARD_FAILED), at=at)
        assert shared_failure([r], OWN_BUILD.id, ARRIVED, {LINUX}) is None, at


@pytest.mark.parametrize("after", [5.001, 5.5, 5.999])
def test_the_5_seconds_are_counted_to_the_millisecond_of_the_record(after):
    r = failed_record(Failed(LINUX, "build-linux", HARD_FAILED), at=ARRIVED + after)
    assert r.recorded_at_epoch == ARRIVED + 5, "the epoch member is the second"
    assert shared_failure([r], OWN_BUILD.id, ARRIVED, {LINUX}) == r


@pytest.mark.parametrize("outcome", ["soft_failed", "neutral", "", "passed"])
def test_any_outcome_but_hard_failed_is_not_shared(outcome):
    r = failed_record(
        Failed(LINUX, "build-linux", HARD_FAILED), Failed(ARM, "build-arm", outcome), at=T0 + 60
    )
    assert shared_failure([r], OWN_BUILD.id, ARRIVED, {LINUX, ARM}) is None


def test_a_record_that_does_not_name_every_wanted_variant_is_not_shared():
    r = failed_record(Failed(LINUX, "build-linux", HARD_FAILED), at=T0 + 60)
    assert shared_failure([r], OWN_BUILD.id, ARRIVED, {LINUX, ARM}) is None


def test_nothing_is_shared_when_nothing_is_wanted():
    r = failed_record(Failed(LINUX, "build-linux", HARD_FAILED), at=T0 + 60)
    assert shared_failure([r], OWN_BUILD.id, ARRIVED, set()) is None


def test_the_builds_own_record_is_not_shared_with_itself():
    r = failed_record(Failed(LINUX, "build-linux", HARD_FAILED), build=OWN_BUILD, at=T0 + 60)
    assert shared_failure([r], OWN_BUILD.id, ARRIVED, {LINUX}) is None


def test_the_most_recent_of_several_shared_failures_is_taken():
    older = failed_record(Failed(LINUX, "build-linux", HARD_FAILED), at=T0 + 60.75)
    third = replace(OTHER_BUILD, id="01890000-aaaa-7000-8000-00000000000c", number=39)
    newer = failed_record(Failed(LINUX, "build-linux", HARD_FAILED), build=third, at=T0 + 60.8)
    assert shared_failure([newer, older], OWN_BUILD.id, ARRIVED, {LINUX}) == newer
    assert shared_failure([older, newer], OWN_BUILD.id, ARRIVED, {LINUX}) == newer


def test_the_own_record_of_a_shared_failure_copies_the_entries_and_names_the_build():
    source = failed_record(
        Failed(LINUX, "build-linux", HARD_FAILED),
        Failed(FREEBSD, "build-freebsd", HARD_FAILED),
        at=T0 + 60,
    )
    own = shared_record(OWN, source, {LINUX}, T0 + 61)
    assert own.failed == (Failed(LINUX, "build-linux", HARD_FAILED),)
    assert (own.shared_with, own.build, own.plan) == (OTHER_BUILD, OWN_BUILD, PLAN_ID)
    assert (own.reason, own.caused_by) == (None, ())


# ---------------------------------------------------------------------------
# Step 10 and resume: a failure of a dependency in this chain
# ---------------------------------------------------------------------------


def test_a_record_of_the_dependency_that_carries_this_plan_is_the_chain_failure():
    r = failed_record(Failed(LINUX, "build-linux", HARD_FAILED))
    assert chain_failure([r], PLAN_ID) == r
    assert chain_failure([r], OTHER_PLAN) is None
    assert chain_failure([], PLAN_ID) is None


@pytest.mark.parametrize(
    "r",
    [
        reason_record(Origin(DUCK, key(DUCK), PLAN_ID, OTHER_BUILD), GENERATOR_FAILED, T0),
        caused_record(
            Origin(DUCK, key(DUCK), PLAN_ID, OTHER_BUILD), [Cause(QDB, key(QDB), (LINUX,))], T0
        ),
    ],
    ids=["a reason", "a cause"],
)
def test_every_kind_of_record_of_this_plan_is_a_chain_failure(r):
    # Built again in this chain it would fail the same way.
    assert chain_failure([r], PLAN_ID) == r


def test_the_most_recent_chain_failure_is_taken():
    older = failed_record(Failed(LINUX, "build-linux", HARD_FAILED), at=T0)
    newer = failed_record(Failed(LINUX, "build-linux", ""), build=OWN_BUILD, at=T0 + 1)
    assert chain_failure([newer, older], PLAN_ID) == newer
    assert chain_failure([older, newer], PLAN_ID) == newer


def test_the_own_record_of_a_chain_failure_names_its_causes_sorted():
    own = caused_record(
        Origin(REST, key(REST), PLAN_ID, OWN_BUILD),
        [Cause(QDB, key(QDB), (LINUX, ARM)), Cause(DUCK, key(DUCK), (LINUX,))],
        T0,
    )
    assert own.caused_by == (Cause(DUCK, key(DUCK), (LINUX,)), Cause(QDB, key(QDB), (ARM, LINUX)))
    assert (own.reason, own.failed, own.shared_with) == (None, (), None)
