"""The Buildkite agent, as an interface (specification sections 4 and 7).

The plan step and the gate steps talk to Buildkite only through an ``Agent``:

- ``meta_data_get(key, build=None)``: the value of ``key`` on this build, or
  on the build whose id is ``build``, without the one newline the agent adds;
  ``MetaDataAbsent`` when the key is not set;
- ``meta_data_set(key, value)``: set a key of this build, the value passed on
  standard input;
- ``step_outcome(key)``: the outcome of a step of this build (``passed``,
  ``hard_failed``, ``soft_failed``, ``neutral``), empty while it has not
  finished;
- ``pipeline_upload(document)``: upload a pipeline document (YAML or JSON) into
  this build, with Buildkite's usual interpolation;
- ``annotate(body, style, context)``: create or replace an annotation.

``RealAgent`` runs ``buildkite-agent``, which refuses what it refuses with its
own message.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Callable, Protocol

# ``buildkite-agent meta-data exists`` exits with this status when the key is
# not set; it exits 1 when the build cannot be read.
META_DATA_ABSENT = 100


class AgentError(RuntimeError):
    """A ``buildkite-agent`` call failed."""


class MetaDataAbsent(AgentError):
    """``meta_data_get`` was asked for a key that is not set."""


class Agent(Protocol):
    """What the jobs may do through the agent. See the module docstring."""

    def meta_data_get(self, key: str, build: str | None = None) -> str: ...

    def meta_data_set(self, key: str, value: str) -> None: ...

    def step_outcome(self, key: str) -> str: ...

    def pipeline_upload(self, document: str) -> None: ...

    def annotate(self, body: str, style: str, context: str) -> None: ...


def build_option(build: str | None) -> list[str]:
    """The ``--build`` option that names another build; none for this build."""
    return [] if build is None else ["--build", build]


def strip_one_newline(text: str) -> str:
    """``text`` without the one newline ``meta-data get`` adds to every value."""
    return text.removesuffix("\n")


# ---------------------------------------------------------------------------
# Running commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Outcome:
    """The result of one command."""

    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[Sequence[str], "str | None"], Outcome]


def run_process(args: Sequence[str], stdin: str | None) -> Outcome:
    """Run ``args`` with ``stdin`` as input; never raises on exit status.

    Standard output and standard error are captured apart, so that a failure
    can be reported in one line and an output is never mixed with warnings.
    Text goes both ways as UTF-8, as the agent reads and writes it, whatever
    the locale.
    """
    p = subprocess.run(list(args), input=stdin, capture_output=True, encoding="utf-8", check=False)
    return Outcome(p.returncode, p.stdout, p.stderr)


def failure(o: Outcome, args: Sequence[str]) -> AgentError:
    return AgentError(f"{' '.join(args)} failed ({o.returncode}): {o.stderr.strip()}")


def checked(o: Outcome, args: Sequence[str]) -> str:
    """The standard output of a command that exited 0; ``AgentError`` otherwise."""
    if o.returncode != 0:
        raise failure(o, args)
    return o.stdout


# ---------------------------------------------------------------------------
# The real agent
# ---------------------------------------------------------------------------


AGENT = "buildkite-agent"


class RealAgent:
    """``Agent`` over the ``buildkite-agent`` command of the job, found on its PATH.

    ``run`` executes one command; tests may replace it.
    """

    def __init__(self, run: Runner = run_process) -> None:
        self._run = run

    def _agent(self, args: Sequence[str], stdin: str | None = None) -> tuple[Outcome, list[str]]:
        command = [AGENT, *args]
        return self._run(command, stdin), command

    def _absent(self, key: str, build: str | None) -> bool:
        """True only when ``meta-data exists`` says the key is not set; never raises."""
        o, _ = self._agent(["meta-data", "exists", key, *build_option(build)])
        return o.returncode == META_DATA_ABSENT

    def meta_data_get(self, key: str, build: str | None = None) -> str:
        o, command = self._agent(["meta-data", "get", key, *build_option(build)])
        if o.returncode == 0:
            return strip_one_newline(o.stdout)
        # A failed get exits 1 for a missing key and for a build that cannot
        # be read alike; exists tells them apart.
        if self._absent(key, build):
            where = f" on build {build}" if build else ""
            raise MetaDataAbsent(f"meta-data {key} is not set{where}")
        raise failure(o, command)

    def meta_data_set(self, key: str, value: str) -> None:
        o, command = self._agent(["meta-data", "set", key], value)
        checked(o, command)

    def step_outcome(self, key: str) -> str:
        o, command = self._agent(["step", "get", "outcome", "--step", key])
        return checked(o, command).strip()

    def pipeline_upload(self, document: str) -> None:
        o, command = self._agent(["pipeline", "upload"], document)
        checked(o, command)

    def annotate(self, body: str, style: str, context: str) -> None:
        o, command = self._agent(["annotate", "--style", style, "--context", context], body)
        checked(o, command)
