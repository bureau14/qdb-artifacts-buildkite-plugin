"""The last step of a build with a plan step: record what failed.

It runs after every other step. When the plan step uploaded the pipeline (outcome ``built``), the
folders whose step did not pass and that are not made at the build's key go into one failure
record, written once, and onto the page. A job retried later promotes its folder itself: the
manifest decides, never the record.
"""

from __future__ import annotations

from ..job import Job
from .annotation import LAST_STEP_ERROR, failure_annotation
from .edge import (
    BUILT,
    OUTCOME,
    PROMOTES,
    Edge,
    create_record,
    made_only,
    manifests_at,
    own_planned,
    read_promotes,
    stored_plan,
)
from .failing import run_step
from .records import Origin, last_step_record, unpassed

# One annotation per context: a retried step replaces its own.
CONTEXT = "snapshot-last-step"


def record_failures(job: Job, edge: Edge) -> None:
    # No outcome: the plan step ended before it set one, and this step's command failed already.
    outcome = edge.agent.meta_data_get(OUTCOME, default="")
    if outcome != BUILT:
        edge.log(f"the plan step's outcome is {outcome or 'not set'}: nothing to record")
        return
    p = stored_plan(job, edge)
    key = own_planned(job, edge, p).key
    promotes = read_promotes(edge.agent.meta_data_get(PROMOTES))
    outcomes = {s: edge.agent.step_outcome(s) for s in sorted(set(promotes.values()))}
    made = made_only(manifests_at(edge, job.project, key, unpassed(promotes, outcomes)))
    origin = Origin(job.project, key, p.id, job.build)
    record = last_step_record(origin, promotes, outcomes, made, edge.clock())
    if record is None:
        edge.log(f"every step of {job.project} at {key} passed or is made")
        return
    create_record(edge, record)
    edge.agent.annotate(failure_annotation(p, record, {}), "error", CONTEXT)


def last_step(job: Job, edge: Edge) -> int:
    return run_step(job, edge, record_failures, CONTEXT, LAST_STEP_ERROR)
