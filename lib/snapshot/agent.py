"""The Buildkite agent: meta-data, step outcomes, pipeline uploads and annotations of this
build, through the ``buildkite-agent`` command of the job."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

from ..errors import ArtifactsError

AGENT = "buildkite-agent"


def agent(args: Sequence[str], stdin: str | None = None) -> str:
    """What ``buildkite-agent`` prints for ``args``; a failed command is an error. Text goes
    both ways as UTF-8, as the agent reads and writes it, whatever the locale."""
    command = [AGENT, *args]
    p = subprocess.run(command, input=stdin, capture_output=True, encoding="utf-8", check=False)
    if p.returncode != 0:
        raise ArtifactsError(f"{' '.join(command)} failed ({p.returncode}): {p.stderr.strip()}")
    return p.stdout


class RealAgent:
    def meta_data_get(self, key: str, build: str | None = None, default: str | None = None) -> str:
        """The value of ``key`` on this build, or on the build whose id is ``build``. A key
        that is not set gives ``default``; without one it is an error, as a build that cannot
        be read always is."""
        where = [] if build is None else ["--build", build]
        fallback = [] if default is None else ["--default", default]
        return agent(["meta-data", "get", key, *where, *fallback]).removesuffix("\n")

    def meta_data_set(self, key: str, value: str) -> None:
        agent(["meta-data", "set", key], value)

    def step_outcome(self, key: str) -> str:
        """``passed``, ``hard_failed``, ``soft_failed`` or ``neutral``; empty while the step
        has not finished."""
        return agent(["step", "get", "outcome", "--step", key]).strip()

    def pipeline_upload(self, document: str) -> None:
        agent(["pipeline", "upload"], document)

    def annotate(self, body: str, style: str, context: str) -> None:
        agent(["annotate", "--style", style, "--context", context], body)
