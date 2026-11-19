"""The job of a gate step: its gate option and the variables the agent sets.

They are read once, when the step starts, and checked for the forms a record
and a plan hold, so that the decisions of the gate steps read the ``Job`` and
never the environment.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from ..errors import ArtifactsError
from ..forms import BRANCH_PREFIX, is_build_id, is_https_url, is_slug
from ..options import command_exit_status, job_build, job_variable
from ..pointer import Build
from .shown import shown_value


@dataclass(frozen=True)
class Job:
    """What a gate step knows of its job: its option and the variables the agent sets.

    ``kind`` is ``plan``, ``resume`` or ``leave``; ``pipeline`` the path of the
    generated pipeline that the option of ``plan`` and ``resume`` names.
    ``plan_id`` is ``SNAPSHOT_PLAN`` (the root build's uuid) of a chain node,
    None for a root; ``needs`` and ``rebuild`` are ``SNAPSHOT_NEEDS`` and
    ``SNAPSHOT_REBUILD`` as given.
    """

    kind: str
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


def checked_build(b: Build) -> Build:
    """This job's build, when its id and URL have the forms a record holds."""
    if not is_build_id(b.id):
        raise ArtifactsError(f"BUILDKITE_BUILD_ID {shown_value(b.id)} is not a build uuid")
    if not is_https_url(b.url):
        raise ArtifactsError(f"BUILDKITE_BUILD_URL {shown_value(b.url)} is not an https URL")
    return b


def job_of(kind: str, pipeline: str | None) -> Job:
    """The job of a gate step of ``kind``, from the variables of its environment."""
    project = job_variable("BUILDKITE_PIPELINE_SLUG", kind)
    if not is_slug(project):
        raise ArtifactsError(f"BUILDKITE_PIPELINE_SLUG {shown_value(project)} is not a slug")
    return Job(
        kind=kind,
        pipeline=pipeline,
        project=project,
        build=checked_build(job_build(kind)),
        branch=os.environ.get("BUILDKITE_BRANCH", ""),
        command=os.environ.get("BUILDKITE_COMMAND"),
        exit_status=command_exit_status(),
        plan_id=os.environ.get("SNAPSHOT_PLAN"),
        needs=os.environ.get("SNAPSHOT_NEEDS"),
        rebuild=os.environ.get("SNAPSHOT_REBUILD"),
    )


def own_ref(job: Job) -> str:
    """The build's own ref, as the generator writes it from ``BUILDKITE_BRANCH``."""
    return BRANCH_PREFIX + job.branch
