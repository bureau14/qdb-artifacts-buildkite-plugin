"""leave: what the step that leaves the gate records (specification section 7).

It runs in the post-command of the step that carries the plugin's option
``leave``, after every other step of the build. It reads no pipeline document,
so it imports none of the modules that do, and its step installs no PyYAML.
"""

from __future__ import annotations

import json
import time

from ..clients import aws_clients
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
    opened_store,
    origin_of,
    own_node,
    pointers_at,
)
from .failing import EDGE_ERRORS, gated
from .git import RealGit
from .job import Job, job_of
from .records import leave_record, unpassed
from .shown import shown_value

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


def step_promotes(edge: Edge) -> dict[str, str]:
    """``snapshot-promotes``: the step key of each variant the build makes."""
    return json.loads(edge.agent.meta_data_get(PROMOTES))


def leave_decisions(job: Job, edge: Edge) -> None:
    mode = build_mode(edge)
    if mode != BUILT:
        edge.log(f"the mode is {shown_value(mode)}: nothing to record")
        return
    p = chain_plan(job, edge)
    node = own_node(job, edge, p)
    if node.key is None:
        edge.log(f"{job.project} has no snapshot: nothing to record")
        return
    promotes = step_promotes(edge)
    outcomes = {s: edge.agent.step_outcome(s) for s in sorted(set(promotes.values()))}
    held = pointers_at(edge, job.project, node.key, unpassed(promotes, outcomes))
    present = [v for v, found in held.items() if found is not None]
    record = leave_record(origin_of(job, p, node.key), promotes, outcomes, present, edge.clock())
    if record is None:
        edge.log(f"every variant of {job.project} at {node.key} passed or exists")
        return
    create_record(edge, record)
    edge.agent.annotate(failure_annotation(p, record, {}), "error", LEAVE_CONTEXT)


def leave(job: Job, edge: Edge) -> int:
    """After every other step of the build: record what failed.

    When the mode is built and the build has a key: the variants of
    ``snapshot-promotes`` whose step did not pass (its outcome empty or not
    ``passed``) and that have no pointer at ``K`` go into one failure record,
    written once, and an annotation. A job retried later writes its pointer
    itself; the pointer decides, never the record. Leave sets no mode: its
    command failed the step already when no plan step set one.
    """
    return gated(job, edge, leave_decisions, EDGE_ERRORS, LEAVE_CONTEXT, mark=False)


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
    return leave(job_of("leave", None), real_edge())
