"""leave: what the step that leaves the gate records (specification section 7).

It runs in the post-command of the step that carries the plugin's option
``leave``, after every other step of the build. It reads no pipeline document,
so it imports none of the modules that do, and its step installs no PyYAML.
"""

from __future__ import annotations

import json
import time

from ..clients import aws_clients
from ..errors import ArtifactsError
from ..forms import is_variant
from ..log import log
from .agent import RealAgent
from .annotation import failure_annotation
from .edge import (
    BUILT,
    MODE,
    PLAN,
    PROMOTES,
    Edge,
    chain_plan,
    create_record,
    meta_data,
    opened_store,
    origin_of,
    own_node,
    own_plan,
    pointers_at,
)
from .failing import EDGE_ERRORS, gated
from .git import RealGit
from .job import Job, is_chain_node, job_of
from .plan import Plan
from .records import is_step_key, leave_record, unpassed
from .shown import shown_value

# One annotation per context: a retried leave replaces its own.
LEAVE_CONTEXT = "snapshot-leave"


def step_promotes(edge: Edge) -> dict[str, str]:
    """``snapshot-promotes``: the step key of each variant the build makes."""
    text = meta_data(edge, PROMOTES)
    try:
        promotes = json.loads(text) if text is not None else None
    except ValueError:
        promotes = None
    if not isinstance(promotes, dict) or not all(
        is_variant(v) and isinstance(s, str) and is_step_key(s) for v, s in promotes.items()
    ):
        raise ArtifactsError(
            f"{PROMOTES} is {shown_value(text)}, not the steps of the variants a built build makes"
        )
    return promotes


def leave_plan(job: Job, edge: Edge) -> Plan:
    """The plan of this build's chain: its root's, or its own as a root."""
    if is_chain_node(job):
        return chain_plan(job, edge)
    p = own_plan(job, edge)
    if p is None:
        raise ArtifactsError(f"the mode is {BUILT}, and this root build holds no {PLAN}")
    return p


def leave_decisions(job: Job, edge: Edge) -> None:
    mode = meta_data(edge, MODE)
    if mode != BUILT:
        edge.log(f"the mode is {shown_value(mode)}: nothing to record")
        return
    p = leave_plan(job, edge)
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
