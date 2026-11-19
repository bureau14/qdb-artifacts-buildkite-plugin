"""The job of a gate step: its gate option and the variables the agent sets.

They are read once, when the step starts, so that the decisions of the gate
steps read the ``Job`` and never the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..options import command_exit_status, job_build
from ..pointer import Build


@dataclass(frozen=True)
class Job:
    """What a gate step knows of its job: its option and the variables the agent sets.

    ``pipeline`` is the path of the generated pipeline that the option of
    ``plan`` and ``resume`` names. ``plan_id`` is ``SNAPSHOT_PLAN`` (the root
    build's uuid) of a chain node, None for a root; ``needs`` and ``rebuild``
    are ``SNAPSHOT_NEEDS`` and ``SNAPSHOT_REBUILD`` as given.
    """

    pipeline: str | None
    project: str
    build: Build
    branch: str
    command: str | None
    exit_status: int
    plan_id: str | None
    needs: str | None
    rebuild: str | None


def is_chain_node(job: Job) -> bool:
    """A chain node has ``SNAPSHOT_PLAN`` in its environment, also an empty one."""
    return job.plan_id is not None


def job_of(pipeline: str | None) -> Job:
    """The job of a gate step whose option names ``pipeline``, from the variables of its
    environment."""
    return Job(
        pipeline=pipeline,
        project=os.environ["BUILDKITE_PIPELINE_SLUG"],
        build=job_build(),
        branch=os.environ.get("BUILDKITE_BRANCH", ""),
        command=os.environ.get("BUILDKITE_COMMAND"),
        exit_status=command_exit_status(),
        plan_id=os.environ.get("SNAPSHOT_PLAN"),
        needs=os.environ.get("SNAPSHOT_NEEDS"),
        rebuild=os.environ.get("SNAPSHOT_REBUILD"),
    )
