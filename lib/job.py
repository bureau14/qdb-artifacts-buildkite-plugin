"""A job and its build, as the agent's variables name them. Read once, when a hook starts."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from .forms import branch_ref, tag_ref


@dataclass(frozen=True)
class Build:
    id: str
    number: int
    url: str


@dataclass(frozen=True)
class Job:
    """``branch`` is the name the build runs under (BUILDKITE_BRANCH), a branch's or a tag's.

    The last four are set by the plan step on a build it starts for a dependency:
    ``root_build`` is the id of the build that made the plan (SNAPSHOT_PLAN), ``needs`` the
    folders asked of this build (SNAPSHOT_NEEDS), ``writes_latest_files`` is false only when
    SNAPSHOT_BRANCH_BUILD says ``false``. ``rebuild`` is SNAPSHOT_REBUILD as a person set it.
    """

    project: str
    repository: str
    branch: str
    tag: str | None
    build: Build
    command: str | None
    exit_status: int
    root_build: str | None = None
    needs: str | None = None
    rebuild: str | None = None
    writes_latest_files: bool = True


def job_of(env: Mapping[str, str]) -> Job:
    return Job(
        project=env["BUILDKITE_PIPELINE_SLUG"],
        repository=env.get("BUILDKITE_REPO", ""),
        branch=env.get("BUILDKITE_BRANCH", ""),
        tag=env.get("BUILDKITE_TAG") or None,
        build=Build(
            env["BUILDKITE_BUILD_ID"],
            int(env["BUILDKITE_BUILD_NUMBER"]),
            env["BUILDKITE_BUILD_URL"],
        ),
        command=env.get("BUILDKITE_COMMAND"),
        exit_status=int(env.get("BUILDKITE_COMMAND_EXIT_STATUS") or "0"),
        root_build=env.get("SNAPSHOT_PLAN"),
        needs=env.get("SNAPSHOT_NEEDS"),
        rebuild=env.get("SNAPSHOT_REBUILD"),
        writes_latest_files=env.get("SNAPSHOT_BRANCH_BUILD") != "false",
    )


def is_started_for_dependency(job: Job) -> bool:
    return job.root_build is not None


def job_ref(job: Job) -> str | None:
    """The full ref of the job's build, as a download entry writes ``git_ref``; None if unknown."""
    if job.tag:
        return job.tag if job.tag.startswith("refs/") else tag_ref(job.tag)
    if job.branch:
        return job.branch if job.branch.startswith("refs/") else branch_ref(job.branch)
    return None


def checkout_commit(cwd: str = ".") -> str:
    """``git rev-parse HEAD`` of a checkout: by default the job's."""
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()
