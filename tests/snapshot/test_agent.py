from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path

import pytest
from fakes import RecordingAgent

from lib.snapshot.agent import (
    META_DATA_LIMIT,
    AgentError,
    MetaDataAbsent,
    Outcome,
    RealAgent,
    build_option,
    strip_one_newline,
)

ROOT_BUILD = "01890000-aaaa-7000-8000-000000000001"


class Script:
    """A runner that answers from a list and records every call with its input."""

    def __init__(self, *answers: Outcome) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, args, stdin):
        self.calls.append((list(args), stdin))
        return self.answers.pop(0)


OK = Outcome(0, "", "")
ABSENT = Outcome(100, "", "")
NO_KEY = Outcome(1, "", 'fatal: failed to get meta-data: 404 Not Found: No key "k" found')
NO_BUILD = Outcome(1, "", "fatal: 404 Not Found: Could not find build")


# ---------------------------------------------------------------------------
# meta-data
# ---------------------------------------------------------------------------


def test_meta_data_get_present_is_one_call():
    run = Script(Outcome(0, "built\n", ""))
    assert RealAgent(run).meta_data_get("snapshot-mode") == "built"
    assert run.calls == [(["buildkite-agent", "meta-data", "get", "snapshot-mode"], None)]


@pytest.mark.parametrize(
    "printed, value",
    [("abc\n", "abc"), ("abc\n\n", "abc\n"), ("  padded  \n", "  padded  "), ("abc", "abc")],
)
def test_exactly_one_trailing_newline_is_stripped(printed, value):
    # Measured: every value comes back as its exact bytes plus one newline,
    # also one that itself ends in a newline (experiment 34, P1).
    assert strip_one_newline(printed) == value
    assert RealAgent(Script(Outcome(0, printed, ""))).meta_data_get("k") == value


def test_meta_data_get_of_an_absent_key_is_an_error_of_its_own_kind():
    run = Script(NO_KEY, ABSENT)
    with pytest.raises(MetaDataAbsent, match="meta-data k is not set"):
        RealAgent(run).meta_data_get("k")
    assert [c[0][1:3] for c in run.calls] == [["meta-data", "get"], ["meta-data", "exists"]]


def test_meta_data_get_failure_is_an_error_not_absent():
    run = Script(Outcome(1, "", "boom"), NO_BUILD)
    with pytest.raises(AgentError, match="boom") as raised:
        RealAgent(run).meta_data_get("k")
    assert not isinstance(raised.value, MetaDataAbsent)


def test_meta_data_get_failure_with_the_key_present_is_an_error_not_absent():
    with pytest.raises(AgentError, match="boom") as raised:
        RealAgent(Script(Outcome(1, "", "boom"), OK)).meta_data_get("k")
    assert not isinstance(raised.value, MetaDataAbsent)


def test_meta_data_get_from_another_build():
    run = Script(Outcome(0, '{"schema":1}\n', ""))
    assert RealAgent(run).meta_data_get("snapshot-plan", ROOT_BUILD) == '{"schema":1}'
    assert run.calls == [
        (["buildkite-agent", "meta-data", "get", "snapshot-plan", "--build", ROOT_BUILD], None)
    ]


def test_an_absent_key_of_another_build_names_the_build():
    run = Script(NO_KEY, ABSENT)
    with pytest.raises(MetaDataAbsent, match=f"not set on build {ROOT_BUILD}"):
        RealAgent(run).meta_data_get("snapshot-plan", ROOT_BUILD)
    assert run.calls[1][0][-2:] == ["--build", ROOT_BUILD]


@pytest.mark.parametrize("call", ["meta_data_get", "meta_data_exists"])
def test_an_empty_build_id_is_refused_before_any_call(call):
    run = Script()
    with pytest.raises(AgentError, match="empty build id"):
        getattr(RealAgent(run), call)("snapshot-plan", "")
    assert run.calls == []


def test_build_option():
    assert build_option(None) == []
    assert build_option(ROOT_BUILD) == ["--build", ROOT_BUILD]


def test_meta_data_exists():
    run = Script(OK, ABSENT)
    a = RealAgent(run)
    assert a.meta_data_exists("snapshot-plan") is True
    assert a.meta_data_exists("snapshot-plan", ROOT_BUILD) is False
    assert run.calls == [
        (["buildkite-agent", "meta-data", "exists", "snapshot-plan"], None),
        (["buildkite-agent", "meta-data", "exists", "snapshot-plan", "--build", ROOT_BUILD], None),
    ]


def test_meta_data_exists_of_a_build_that_cannot_be_read_is_an_error():
    with pytest.raises(AgentError, match="Could not find build"):
        RealAgent(Script(NO_BUILD)).meta_data_exists("k", ROOT_BUILD)


def test_meta_data_set_passes_the_value_on_stdin():
    run = Script(OK)
    RealAgent(run).meta_data_set("snapshot-mode", "built")
    assert run.calls == [(["buildkite-agent", "meta-data", "set", "snapshot-mode"], "built")]


def test_meta_data_set_up_to_the_limit_and_not_beyond():
    run = Script(OK)
    RealAgent(run).meta_data_set("snapshot-plan", "x" * META_DATA_LIMIT)
    assert len(run.calls) == 1
    with pytest.raises(AgentError, match="102401 bytes, more than the agent's limit of 102400"):
        RealAgent(Script()).meta_data_set("snapshot-plan", "x" * (META_DATA_LIMIT + 1))
    # The limit is in bytes: two characters of two bytes each over 102399 ASCII ones.
    with pytest.raises(AgentError, match="102403 bytes"):
        RealAgent(Script()).meta_data_set("k", "x" * (META_DATA_LIMIT - 1) + chr(0xE9) * 2)


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_meta_data_set_refuses_a_blank_value_as_the_agent_does(value):
    with pytest.raises(AgentError, match="empty or blank"):
        RealAgent(Script()).meta_data_set("k", value)


# ---------------------------------------------------------------------------
# Steps, uploads, annotations
# ---------------------------------------------------------------------------


def test_step_outcome_reads_standard_output_only():
    run = Script(Outcome(0, "soft_failed\n", "WARN something"), Outcome(0, "", "WARN x"))
    a = RealAgent(run)
    assert a.step_outcome("build") == "soft_failed"
    assert a.step_outcome("not-finished") == ""
    assert run.calls[0][0] == ["buildkite-agent", "step", "get", "outcome", "--step", "build"]


def test_pipeline_upload_and_annotate_pass_the_document_on_stdin():
    run = Script(OK, OK)
    a = RealAgent(run)
    a.pipeline_upload("steps: []\n")
    a.annotate("**x**", "error", "snapshot")
    assert run.calls == [
        (["buildkite-agent", "pipeline", "upload"], "steps: []\n"),
        (["buildkite-agent", "annotate", "--style", "error", "--context", "snapshot"], "**x**"),
    ]


def test_upload_failure_raises():
    with pytest.raises(AgentError, match="pipeline upload failed \\(2\\): bad"):
        RealAgent(Script(Outcome(2, "", "bad"))).pipeline_upload("x")


def test_a_missing_agent_binary_is_an_agent_error():
    with pytest.raises(AgentError, match="failed \\(127\\)"):
        RealAgent(binary="/nonexistent/buildkite-agent").meta_data_get("k")


def test_annotate_rejects_unknown_style():
    with pytest.raises(ValueError, match="unknown style"):
        RealAgent(Script()).annotate("x", "loud", "c")


# ---------------------------------------------------------------------------
# A real process: a stand-in buildkite-agent
# ---------------------------------------------------------------------------

STAND_IN = """\
#!/bin/sh
# A stand-in for buildkite-agent: logs its arguments and standard input.
echo "$*" >> "$LOG"
case "$1 $2" in
  "meta-data get") [ "$3" = known ] && echo "value" && exit 0; echo "No key" >&2; exit 1 ;;
  "meta-data exists") [ "$3" = known ] && exit 0; exit 100 ;;
  *) cat >> "$LOG"; echo >> "$LOG" ;;
esac
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in is a POSIX shell script")
def test_the_real_agent_through_a_process(tmp_path, monkeypatch):
    binary = tmp_path / "buildkite-agent"
    binary.write_text(STAND_IN)
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "log"
    monkeypatch.setenv("LOG", str(log))
    a = RealAgent(binary=str(binary))
    assert a.meta_data_get("known", ROOT_BUILD) == "value"
    assert a.meta_data_exists("unknown") is False
    with pytest.raises(MetaDataAbsent):
        a.meta_data_get("unknown")
    a.meta_data_set("snapshot-mode", "built")
    assert log.read_text().splitlines() == [
        f"meta-data get known --build {ROOT_BUILD}",
        "meta-data exists unknown",
        "meta-data get unknown",
        "meta-data exists unknown",
        "meta-data set snapshot-mode",
        "built",
    ]


# A child interpreter that sends a text through run_process to a process that
# echoes it, and prints what came back.
ECHO_THROUGH_RUN_PROCESS = """\
import sys
sys.path.insert(0, sys.argv[1])
from lib.snapshot.agent import run_process
echo = "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"
print(ascii(run_process([sys.executable, "-c", echo], "caf" + chr(0xE9)).stdout))
"""


@pytest.mark.skipif(sys.platform == "win32", reason="the C locale is POSIX")
def test_the_agent_is_spoken_to_in_utf8_whatever_the_locale():
    # Under the C locale without Python's UTF-8 mode, text pipes are ASCII.
    root = str(Path(__file__).resolve().parents[2])
    child = [sys.executable, "-I", "-B", "-X", "utf8=0", "-c", ECHO_THROUGH_RUN_PROCESS, root]
    env = {"LC_ALL": "C", "LANG": "C"}
    p = subprocess.run(child, env=env, capture_output=True, text=True, check=False)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == "'caf\\xe9'"


# ---------------------------------------------------------------------------
# The fake
# ---------------------------------------------------------------------------


def test_recording_agent_behaves_as_the_agent():
    a = RecordingAgent(
        meta_data={"a": "1"}, builds={ROOT_BUILD: {"snapshot-plan": "{}"}}, outcomes={"s": "passed"}
    )
    a.meta_data_set("b", "2")
    a.pipeline_upload("doc")
    a.annotate("body", "info", "ctx")
    assert (a.meta_data_get("a"), a.meta_data_get("b")) == ("1", "2")
    assert a.meta_data_get("snapshot-plan", ROOT_BUILD) == "{}"
    assert a.meta_data_exists("a") and not a.meta_data_exists("c")
    assert not a.meta_data_exists("a", ROOT_BUILD)
    with pytest.raises(MetaDataAbsent):
        a.meta_data_get("c")
    with pytest.raises(AgentError, match="empty build id"):
        a.meta_data_get("snapshot-plan", "")
    with pytest.raises(AgentError, match="no build"):
        a.meta_data_get("snapshot-plan", "other")
    with pytest.raises(AgentError, match="limit"):
        a.meta_data_set("big", "x" * (META_DATA_LIMIT + 1))
    with pytest.raises(ValueError, match="unknown style"):
        a.annotate("x", "loud", "c")
    assert (a.step_outcome("s"), a.step_outcome("t")) == ("passed", "")
    assert a.uploads == ["doc"] and a.annotations == [("body", "info", "ctx")]
