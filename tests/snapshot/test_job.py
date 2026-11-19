"""The job of a gate step, read from the variables the agent sets."""

from __future__ import annotations

import pytest
from gates import BUILDS, LIB, ROOT_BUILD

from lib.errors import ArtifactsError
from lib.snapshot.job import Job, job_of

JOB_ENV = {
    "BUILDKITE_PIPELINE_SLUG": LIB,
    "BUILDKITE_STEP_KEY": "snapshot-enter",
    "BUILDKITE_BUILD_ID": BUILDS[LIB].id,
    "BUILDKITE_BUILD_NUMBER": "21",
    "BUILDKITE_BUILD_URL": BUILDS[LIB].url,
    "BUILDKITE_BRANCH": "snapshot/feature",
    "BUILDKITE_COMMAND": "make pipeline",
    "BUILDKITE_COMMAND_EXIT_STATUS": "3",
    "SNAPSHOT_PLAN": ROOT_BUILD.id,
    "SNAPSHOT_NEEDS": '["linux"]',
}


def test_the_job_is_read_from_the_variables_the_agent_sets(monkeypatch):
    for name, value in JOB_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SNAPSHOT_REBUILD", raising=False)

    job = job_of("plan", ".buildkite/snapshot-pipeline.yml")

    assert job == Job(
        kind="plan",
        pipeline=".buildkite/snapshot-pipeline.yml",
        step_key="snapshot-enter",
        project=LIB,
        build=BUILDS[LIB],
        branch="snapshot/feature",
        command="make pipeline",
        exit_status=3,
        plan_id=ROOT_BUILD.id,
        needs='["linux"]',
        rebuild=None,
    )


@pytest.mark.parametrize(
    "name, value, named",
    [
        ("BUILDKITE_BUILD_ID", "build-7", "BUILDKITE_BUILD_ID"),
        ("BUILDKITE_BUILD_URL", "http://ci.example/lib/21", "BUILDKITE_BUILD_URL"),
        ("BUILDKITE_PIPELINE_SLUG", "Lib", "BUILDKITE_PIPELINE_SLUG"),
    ],
)
def test_a_job_variable_of_the_wrong_form_is_refused(monkeypatch, name, value, named):
    for n, v in {**JOB_ENV, name: value}.items():
        monkeypatch.setenv(n, v)

    with pytest.raises(ArtifactsError, match=named):
        job_of("plan", "p.yml")
