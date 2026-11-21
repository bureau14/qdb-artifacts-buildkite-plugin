"""leave: what the step that leaves the gate records (specification section 7).

It runs in the post-command of the step that carries the plugin's option
``leave``, after every other step of the build. It reads no pipeline document,
so it imports none of the modules that do, and its step installs no PyYAML.
"""

from __future__ import annotations

import json
import time

from ..clients import aws_clients
from ..layout import Folder, folder_of
from ..log import log
from .agent import MetaDataAbsent, RealAgent
from .annotation import failure_annotation
from .edge import (
    BUILT,
    MODE,
    PROMOTES,
    Edge,
    chain_plan,
    create_record,
    made_only,
    manifests_at,
    opened_store,
    origin_of,
    own_planned,
)
from .failing import gated
from .git import RealGit
from .job import Job, job_of
from .records import leave_record, unpassed

# One annotation per context: a retried leave replaces its own.
LEAVE_CONTEXT = "snapshot-leave"


def build_mode(edge: Edge) -> str | None:
    """``snapshot-mode``; None when no plan step set it.

    Absence is an answer here: an enter that ended before it set a mode (its
    hook failed, it was cancelled or timed out) leaves none, and leave's
    command has failed the step already.
    """
    try:
        return edge.agent.meta_data_get(MODE)
    except MetaDataAbsent:
        return None


def step_promotes(edge: Edge) -> dict[Folder, str]:
    """``snapshot-promotes``: the Buildkite key of the step that promotes each folder."""
    listed = json.loads(edge.agent.meta_data_get(PROMOTES))
    return {folder_of(e): e["step_key"] for e in listed}


def leave_decisions(job: Job, edge: Edge) -> None:
    mode = build_mode(edge)
    if mode != BUILT:
        edge.log(f"the mode is {mode}: nothing to record")
        return
    p = chain_plan(job, edge)
    own = own_planned(job, edge, p)
    promotes = step_promotes(edge)
    outcomes = {s: edge.agent.step_outcome(s) for s in sorted(set(promotes.values()))}
    made = made_only(manifests_at(edge, job.project, own.key, unpassed(promotes, outcomes)))
    record = leave_record(origin_of(job, p, own.key), promotes, outcomes, made, edge.clock())
    if record is None:
        edge.log(f"every step of {job.project} at {own.key} passed or is made")
        return
    create_record(edge, record)
    edge.agent.annotate(failure_annotation(p, record, {}), "error", LEAVE_CONTEXT)


def leave(job: Job, edge: Edge) -> int:
    """After every other step of the build: record what failed.

    When the mode is built: the folders of
    ``snapshot-promotes`` whose step did not pass (its outcome empty or not
    ``passed``) and that are not made at ``K`` go into one failure record,
    written once, and an annotation. A job retried later promotes the folder
    itself; the manifest decides, never the record. Leave sets no mode: its
    command failed the step already when no plan step set one.
    """
    return gated(job, edge, leave_decisions, LEAVE_CONTEXT, mark=False)


# ---------------------------------------------------------------------------
# On an agent
# ---------------------------------------------------------------------------


def real_edge() -> Edge:
    """The edge of a leave step on an agent: its store, its agent, its checkout."""
    _, ssm = aws_clients()
    store, prefix = opened_store(ssm)
    return Edge(
        store=store,
        prefix=prefix,
        agent=RealAgent(),
        checkout=RealGit(log=log),
        clock=time.time,
        log=log,
    )


def run() -> int:
    """The gate command of the step that carries ``leave``, on an agent."""
    return leave(job_of(None), real_edge())
