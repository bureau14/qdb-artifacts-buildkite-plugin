"""leave over the fakes of the planning package.

The chain and its gate steps are those of ``gates.py``.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from gates import (
    APP,
    BUILDS,
    CORE,
    LINUX,
    MACOS,
    MASTER,
    PLAN,
    ROOT_BUILD,
    Gate,
    commit,
    key,
    pointer_objects,
)

from lib.pointer import Pointer
from lib.snapshot.plan import encode_plan
from lib.snapshot.records import HARD_FAILED, Failed


def leaving(tmp_path, project: str, outcomes: dict, objects=None, mode="built") -> Gate:
    gate = Gate(tmp_path, project, kind="leave", objects=objects)
    gate.agent.outcomes.update(outcomes)
    if mode is not None:
        gate.agent.meta_data["snapshot-mode"] = mode
    gate.agent.meta_data["snapshot-promotes"] = json.dumps(
        {LINUX: "build-linux", MACOS: "build-macos"}
    )
    if project == APP:
        gate.agent.meta_data["snapshot-plan"] = encode_plan(PLAN)
    return gate


def test_leave_records_the_variants_whose_step_did_not_pass_and_that_have_no_pointer(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED, "build-macos": "passed"})

    assert gate.run() == 0

    (record,) = gate.records()
    assert record.failed == (Failed(LINUX, "build-linux", HARD_FAILED),)
    assert (record.plan, record.build) == (ROOT_BUILD.id, BUILDS[CORE])
    body, style = gate.annotation("snapshot-leave")
    assert style == "error" and body.startswith("Snapshot failed")


def test_leave_records_a_step_that_did_not_finish_with_an_empty_outcome(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-macos": "passed"})

    gate.run()

    (record,) = gate.records()
    assert record.failed == (Failed(LINUX, "build-linux", ""),)


def test_leave_records_nothing_when_every_step_passed(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-linux": "passed", "build-macos": "passed"})

    gate.run()

    assert gate.records() == [] and gate.agent.annotations == []
    assert gate.store.calls == []


@pytest.mark.parametrize(
    "outcome", [HARD_FAILED, "soft_failed", ""], ids=["hard-failed", "soft-failed", "running-again"]
)
def test_leave_records_nothing_for_a_variant_whose_pointer_a_retried_job_wrote(tmp_path, outcome):
    objects = pointer_objects(
        Pointer(
            CORE, key(CORE), LINUX, MASTER, commit(CORE), BUILDS[CORE], "2025-11-17T12:00:00.000Z"
        )
    )
    gate = leaving(tmp_path, CORE, {"build-linux": outcome, "build-macos": "passed"}, objects)

    gate.run()

    assert gate.records() == []


@pytest.mark.parametrize("mode", ["reused", "failed", None])
def test_leave_records_nothing_unless_the_build_was_built(tmp_path, mode):
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED}, mode=mode)

    assert gate.run() == 0

    assert gate.store.calls == []


def test_leave_of_a_root_without_a_snapshot_records_nothing(tmp_path):
    gate = leaving(tmp_path, APP, {"build-linux": HARD_FAILED})
    no_key = dataclasses.replace(
        PLAN, nodes={**PLAN.nodes, APP: dataclasses.replace(PLAN.nodes[APP], key=None)}
    )
    gate.agent.meta_data["snapshot-plan"] = encode_plan(no_key)

    gate.run()

    assert gate.store.calls == []


def test_a_retried_leave_leaves_its_record_as_it_is(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED, "build-macos": "passed"})
    gate.run()
    first = dict(gate.store.objects)
    gate.agent.outcomes["build-macos"] = HARD_FAILED

    gate.run()

    assert gate.store.objects == first
    assert gate.lines[-1].endswith("exists; left as it is")


def test_an_error_of_the_store_fails_leave_and_leaves_the_mode(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED})
    gate.store.refused.append("_snapshot/v1/failures/")

    assert "AccessDenied" in gate.fails()
    assert gate.mode == "built"
    assert gate.annotation("snapshot-leave")[0].startswith(
        "The snapshot step of `core` build 31 failed"
    )
