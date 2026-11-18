"""The texts a gate step shows on the build page."""

from __future__ import annotations

from dataclasses import replace

import pytest
from plans import DUCK, PLAN_ID, PYTHON, QDB, REST, T0, key, plan, sha

from lib.pointer import Build
from lib.snapshot.annotation import (
    PLAN_REFUSED,
    STEP_ERROR,
    caused_annotation,
    failure_annotation,
    fenced,
    message_annotation,
    plan_annotation,
    refusal_annotation,
    reused_annotation,
)
from lib.snapshot.records import (
    GENERATOR_FAILED,
    HARD_FAILED,
    NOT_DECLARED,
    RULE,
    Cause,
    Failed,
    Origin,
    caused_record,
    compose_record,
    reason_record,
    shared_record,
)

LINUX, ARM = "linux-core2-release", "linux-aarch64-release"


def build(project: str, n: int) -> Build:
    return Build(f"01890000-aaaa-7000-8000-{n:012d}", n, f"https://ci.example/{project}/{n}")


def origin(project: str, n: int) -> Origin:
    return Origin(project, key(project), PLAN_ID, build(project, n))


def short(project: str) -> str:
    return sha(project)[:12]


# ---------------------------------------------------------------------------
# A fenced block
# ---------------------------------------------------------------------------


def test_a_block_is_fenced_longer_than_any_run_of_backticks_in_it():
    assert fenced(["a", "b"]) == "```\na\nb\n```"
    assert fenced(["x ```` y", "``"]) == "`````\nx ```` y\n``\n`````"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def node_line(project: str, depth: int, label: str = "master") -> str:
    pad = "  " * depth
    return f"{pad}{project} @ {short(project)} refs/heads/master {key(project)} runs as {label}"


def test_the_plan_of_the_root_is_its_tree_with_commits_keys_and_labels():
    assert plan_annotation(plan(), PYTHON) == "\n".join(
        [
            "Snapshot plan of `qdb-api-python` build 512",
            "",
            "```",
            "branch master, rebuild none",
            node_line(PYTHON, 0),
            node_line(REST, 1),
            node_line(DUCK, 2),
            node_line(QDB, 3),
            f"    {QDB} (above)",
            f"  {QDB} (above)",
            "```",
        ]
    )


def test_a_chain_node_shows_its_own_tree_of_the_roots_plan():
    text = plan_annotation(plan(), DUCK)
    assert (
        text.splitlines()[0]
        == "Snapshot plan of `qdb-duck`, in the chain of `qdb-api-python` build 512"
    )
    assert text.splitlines()[4:6] == [node_line(DUCK, 0), node_line(QDB, 1)]


def test_a_root_without_a_snapshot_has_no_key_in_the_plan():
    line = plan_annotation(plan(root_has_snapshot=False), PYTHON).splitlines()[4]
    assert line == f"{PYTHON} @ {short(PYTHON)} refs/heads/master no snapshot runs as master"


def test_a_branch_is_shown_in_ascii_and_cannot_close_the_block():
    branch = "x```" + chr(0xE9)
    p = plan(label="snapshot/" + branch)
    p = replace(p, context=branch)
    lines = plan_annotation(p, QDB).splitlines()
    assert lines[2] == "````" and lines[-1] == "````"
    assert lines[3] == "branch x```\\xe9, rebuild none"
    assert lines[4].endswith("runs as snapshot/x```\\xe9")
    "\n".join(lines).encode("ascii")


def test_a_reused_build_shows_the_builds_that_hold_its_variants():
    builds = {
        LINUX: Build("01890000-aaaa-7000-8000-000000000033", 33, "https://ci.example/d/33"),
        ARM: Build("01890000-aaaa-7000-8000-000000000031", 31, "https://ci.example/d/31"),
    }
    text = reused_annotation(plan(), DUCK, builds)
    assert text.splitlines()[0] == (
        "`qdb-duck` is reused: every variant it was asked for exists, so nothing is built"
    )
    assert text.splitlines()[2:6] == [
        "```",
        "linux-aarch64-release in build 31 https://ci.example/d/31",
        "linux-core2-release in build 33 https://ci.example/d/33",
        "```",
    ]
    assert text.endswith(plan_annotation(plan(), DUCK))


# ---------------------------------------------------------------------------
# A refusal or a plan error
# ---------------------------------------------------------------------------


def test_a_message_is_shown_line_by_line_in_a_fenced_block():
    message = "qdb-duck cannot give a snapshot: .buildkite/steps/_build.yml\nsecond line"
    assert message_annotation("The plan of `qdb-api-python` failed", message) == (
        "The plan of `qdb-api-python` failed\n\n"
        "```\nqdb-duck cannot give a snapshot: .buildkite/steps/_build.yml\nsecond line\n```"
    )


@pytest.mark.parametrize(
    "kind, heading",
    [
        (GENERATOR_FAILED, "The pipeline generator of `qdb-duck` build 33 failed"),
        (
            RULE,
            "The generated pipeline of `qdb-duck` build 33 breaks the rule of its templates",
        ),
        (NOT_DECLARED, "`qdb-duck` build 33 was asked for a variant it does not promote"),
        (PLAN_REFUSED, "The snapshot plan of `qdb-duck` build 33 cannot be made"),
        (STEP_ERROR, "The snapshot step of `qdb-duck` build 33 failed"),
    ],
)
def test_a_refusal_names_what_of_the_build_failed_above_its_message(kind, heading):
    text = refusal_annotation(kind, DUCK, build(DUCK, 33), "first\nsecond")
    assert text == f"{heading}\n\n```\nfirst\nsecond\n```"


# ---------------------------------------------------------------------------
# The failure annotation of section 9
# ---------------------------------------------------------------------------


def closing(pipeline: str, number: int) -> str:
    return (
        f"To retry: retry the failed job in `{pipeline}` `{number}`. When it passes, start a new "
        "build of `qdb-api-python`. Everything that passed is reused."
    )


def test_a_dependency_that_failed_down_the_chain():
    qdb = compose_record(
        origin(QDB, 7), T0, None, [Failed(LINUX, "build-linux", HARD_FAILED)], (), None
    )
    duck = caused_record(origin(DUCK, 33), [Cause(QDB, key(QDB), (LINUX,))], T0)
    own = caused_record(origin(REST, 40), [Cause(DUCK, key(DUCK), (LINUX,))], T0)
    text = failure_annotation(plan(), own, {key(QDB): qdb, key(DUCK): duck})
    assert text == "\n".join(
        [
            "Snapshot dependency failed",
            "",
            "```",
            "qdb-api-rest build 40 was not built, because:",
            f"  qdb-duck @ {short(DUCK)} ({key(DUCK)}) has no linux-core2-release.",
            "    FAILED in qdb-duck build 33: https://ci.example/qdb-duck/33",
            (
                f"      caused by quasardb-build @ {short(QDB)} ({key(QDB)}), which has no "
                "linux-core2-release."
            ),
            "        FAILED in quasardb-build build 7: https://ci.example/quasardb-build/7",
            "          linux-core2-release: step build-linux: hard_failed",
            "```",
            "",
            closing(QDB, 7),
        ]
    )


def test_a_dependency_that_left_no_record():
    own = caused_record(origin(REST, 40), [Cause(DUCK, key(DUCK), (LINUX, ARM))], T0)
    lines = failure_annotation(plan(), own, {}).splitlines()
    assert lines[4:6] == [
        (
            f"  qdb-duck @ {short(DUCK)} ({key(DUCK)}) has no linux-aarch64-release, "
            "linux-core2-release."
        ),
        "    it left no record",
    ]
    assert lines[-1] == (
        "To retry: start a new build of `qdb-api-python` when the cause is fixed. Everything "
        "that passed is reused."
    )


def test_two_dependencies_that_failed_name_both_builds_to_retry():
    duck = reason_record(origin(DUCK, 33), GENERATOR_FAILED, T0)
    qdb = compose_record(origin(QDB, 7), T0, None, [Failed(ARM, "build-arm", "")], (), None)
    causes = [Cause(DUCK, key(DUCK), (LINUX,)), Cause(QDB, key(QDB), (ARM,))]
    own = caused_record(origin(REST, 40), causes, T0)
    lines = failure_annotation(plan(), own, {key(QDB): qdb, key(DUCK): duck}).splitlines()
    assert lines[5:7] == [
        "    FAILED in qdb-duck build 33: https://ci.example/qdb-duck/33",
        "      its pipeline generator failed",
    ]
    assert lines[9] == "      linux-aarch64-release: step build-arm: did not finish"
    assert lines[-1] == (
        "To retry: retry the failed jobs in `qdb-duck` `33` and `quasardb-build` `7`. When they "
        "pass, start a new build of `qdb-api-python`. Everything that passed is reused."
    )


def test_a_failure_shared_with_the_build_this_one_waited_behind():
    source = compose_record(
        origin(DUCK, 31), T0, None, [Failed(LINUX, "build-linux", HARD_FAILED)], (), None
    )
    own = shared_record(origin(DUCK, 33), source, {LINUX}, T0 + 60)
    assert failure_annotation(plan(), own, {}) == "\n".join(
        [
            "Snapshot failed in the build this one waited behind",
            "",
            "```",
            "qdb-duck build 33 was not built, because:",
            "  build 31 of the same snapshot failed while this build waited behind it.",
            "    FAILED in qdb-duck build 31: https://ci.example/qdb-duck/31",
            "      linux-core2-release: step build-linux: hard_failed",
            "```",
            "",
            closing(DUCK, 31),
        ]
    )


def test_the_builds_own_failure():
    own = compose_record(
        origin(DUCK, 33), T0, None, [Failed(LINUX, "build-linux", "soft_failed")], (), None
    )
    assert failure_annotation(plan(), own, {}).splitlines()[:6] == [
        "Snapshot failed",
        "",
        "```",
        "qdb-duck build 33 did not give its snapshot:",
        "  linux-core2-release: step build-linux: soft_failed",
        "```",
    ]


def test_a_record_reached_by_two_causes_is_shown_once():
    qdb = compose_record(
        origin(QDB, 7), T0, None, [Failed(LINUX, "build-linux", HARD_FAILED)], (), None
    )
    duck = caused_record(origin(DUCK, 33), [Cause(QDB, key(QDB), (LINUX,))], T0)
    causes = [Cause(DUCK, key(DUCK), (LINUX,)), Cause(QDB, key(QDB), (LINUX,))]
    own = caused_record(origin(REST, 40), causes, T0)
    text = failure_annotation(plan(), own, {key(QDB): qdb, key(DUCK): duck})
    assert text.splitlines()[3:13] == [
        "qdb-api-rest build 40 was not built, because:",
        f"  qdb-duck @ {short(DUCK)} ({key(DUCK)}) has no linux-core2-release.",
        "    FAILED in qdb-duck build 33: https://ci.example/qdb-duck/33",
        (
            f"      caused by quasardb-build @ {short(QDB)} ({key(QDB)}), which has no "
            "linux-core2-release."
        ),
        "        FAILED in quasardb-build build 7: https://ci.example/quasardb-build/7",
        "          linux-core2-release: step build-linux: hard_failed",
        f"  quasardb-build @ {short(QDB)} ({key(QDB)}) has no linux-core2-release.",
        "    its record is shown above",
        "```",
        "",
    ]
    assert text.splitlines()[-1] == closing(QDB, 7)


def test_a_build_named_twice_by_one_record_is_retried_once():
    # A record may hold a reason and failed entries: both name its build.
    duck = compose_record(
        origin(DUCK, 33), T0, GENERATOR_FAILED, [Failed(LINUX, "build-linux", "")], (), None
    )
    own = caused_record(origin(REST, 40), [Cause(DUCK, key(DUCK), (LINUX,))], T0)
    text = failure_annotation(plan(), own, {key(DUCK): duck})
    assert text.splitlines()[-1] == closing(DUCK, 33)


def ladder(levels: int) -> tuple[dict, list]:
    """Two records per level, each caused by both of the level below; the bottom two failed."""

    def name(level: int, side: str) -> str:
        return f"p{level}-{side}"

    def record(level: int, side: str):
        o = Origin(name(level, side), f"k-{level}-{side}", PLAN_ID, build(name(level, side), level))
        if level == levels - 1:
            return compose_record(o, T0, None, [Failed(LINUX, "build", HARD_FAILED)], (), None)
        below = [Cause(name(level + 1, s), f"k-{level + 1}-{s}", (LINUX,)) for s in "ab"]
        return caused_record(o, below, T0)

    records = {f"k-{n}-{s}": record(n, s) for n in range(levels) for s in "ab"}
    return records, [Cause(name(0, s), f"k-0-{s}", (LINUX,)) for s in "ab"]


def test_a_ladder_of_records_shows_each_record_once():
    records, top = ladder(12)
    own = caused_record(origin(REST, 40), top, T0)
    text = failure_annotation(plan(), own, records)
    assert text.count("FAILED in ") == len(records) == 24
    assert text.count("its record is shown above") == 2 * 12 - 2


# ---------------------------------------------------------------------------
# A build that dependencies of its chain left without what it needs
# ---------------------------------------------------------------------------


def test_the_causes_of_a_build_read_as_the_record_it_writes_of_them():
    qdb = compose_record(
        origin(QDB, 7), T0, None, [Failed(LINUX, "build-linux", HARD_FAILED)], (), None
    )
    duck = caused_record(origin(DUCK, 33), [Cause(QDB, key(QDB), (LINUX,))], T0)
    causes = [Cause(DUCK, key(DUCK), (LINUX,)), Cause(QDB, key(QDB), (LINUX,))]
    records = {key(QDB): qdb, key(DUCK): duck}
    own = caused_record(origin(REST, 40), causes, T0)

    text = caused_annotation(plan(), REST, build(REST, 40), causes, records)

    assert text == failure_annotation(plan(), own, records)


def test_a_root_without_a_snapshot_is_shown_its_causes_without_a_record_of_its_own():
    qdb = compose_record(
        origin(QDB, 7), T0, None, [Failed(LINUX, "build-linux", HARD_FAILED)], (), None
    )
    text = caused_annotation(
        plan(root_has_snapshot=False),
        PYTHON,
        build(PYTHON, 512),
        [Cause(QDB, key(QDB), (LINUX,))],
        {key(QDB): qdb},
    )
    assert text.splitlines()[:4] == [
        "Snapshot dependency failed",
        "",
        "```",
        "qdb-api-python build 512 was not built, because:",
    ]
    assert text.splitlines()[-1] == (
        "To retry: retry the failed job in `quasardb-build` `7`. When it passes, start a new "
        "build of `qdb-api-python`. Everything that passed is reused."
    )


def test_a_triggered_build_that_left_no_record_is_named_with_its_trigger_step():
    causes = [Cause(DUCK, key(DUCK), (ARM, LINUX))]

    lines = caused_annotation(plan(), REST, build(REST, 40), causes, {}).splitlines()

    assert lines[4:6] == [
        (
            f"  qdb-duck @ {short(DUCK)} ({key(DUCK)}) has no linux-aarch64-release, "
            "linux-core2-release."
        ),
        (
            "    the build of qdb-duck triggered by this build ended without "
            "linux-aarch64-release, linux-core2-release and left no record; see the step "
            f":link: qdb-duck @ {short(DUCK)}"
        ),
    ]
    assert lines[-1] == (
        "To retry: start a new build of `qdb-api-python` when the cause is fixed. Everything "
        "that passed is reused."
    )


def test_records_that_name_each_other_end():
    a = caused_record(origin(DUCK, 33), [Cause(QDB, key(QDB), (LINUX,))], T0)
    b = caused_record(origin(QDB, 7), [Cause(DUCK, key(DUCK), (LINUX,))], T0)
    own = caused_record(origin(REST, 40), [Cause(DUCK, key(DUCK), (LINUX,))], T0)
    lines = failure_annotation(plan(), own, {key(DUCK): a, key(QDB): b}).splitlines()
    assert lines[8:10] == [
        (
            f"          caused by qdb-duck @ {short(DUCK)} ({key(DUCK)}), which has no "
            "linux-core2-release."
        ),
        "            its record is shown above",
    ]
    assert len(lines) == 13
