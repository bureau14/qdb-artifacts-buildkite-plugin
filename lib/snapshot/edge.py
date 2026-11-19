"""What every gate step reads and changes (``Edge``), and the reads they share through it.

The edge is the store as a job opens it, ``buildkite-agent``, the build's own
checkout, the clock and the log, each behind the interface of its module: the
decisions of the gate steps are plain functions of what these return, and a
test hands them fakes. The plan step reaches further; ``enter.PlanEdge`` adds
its parts.

The steps keep no state but these marks on their own build, which leave reads
back:

    snapshot-mode                  reused, built or failed
    snapshot-promotes              {variant: step key} of the steps that promote

The reads here are those of more than one step: the pointers at a key, the
failure records of a key, the arrival of the chain's root at the gate, and the
plan of the build's chain. Each meta-data read here is of a value that is set before it is read,
so an absent value fails the step as any failed call does.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import layout, pointer
from ..clients import resolve_object_auth
from ..config import load_store_config
from ..objects import Store, destination_store
from ..pointer import Pointer
from .agent import Agent
from .git import Checkout
from .job import Job
from .plan import Node, Plan, decode_plan, planned_node
from .records import Cause, Origin, Record, chain_failure, decode_record, encode_record

# The marks on the build, and the meta-data a step reads.
MODE = "snapshot-mode"
REUSED, BUILT, FAILED = "reused", "built", "failed"
PROMOTES = "snapshot-promotes"
PLAN = "snapshot-plan"
ARRIVED = "snapshot-arrived"


@dataclass(frozen=True)
class Edge:
    """Everything a gate step reads or changes, each behind the interface of its module.

    ``store`` and ``prefix``: the destination's store, opened as a job opens
    it (``objects.destination_store``). ``agent``: ``buildkite-agent``.
    ``checkout``: the build's own checkout. ``clock``: epoch seconds.
    ``log``: one line to the job log.
    """

    store: Store
    prefix: str
    agent: Agent
    checkout: Checkout
    clock: Callable[[], float]
    log: Callable[[str], None]


def opened_store(ssm: Any) -> tuple[Store, str]:
    """The destination's store and its prefix, opened as a job opens them."""
    cfg = load_store_config(ssm)
    return destination_store(cfg, resolve_object_auth(ssm, cfg, "object-read-write"))


# ---------------------------------------------------------------------------
# Reading through the edge
# ---------------------------------------------------------------------------


def pointers_at(
    edge: Edge, project: str, key: str, variants: Iterable[str]
) -> dict[str, Pointer | None]:
    """The pointer of each variant of ``project`` at ``key``; None where no build holds it."""
    return {v: pointer.find(edge.store, edge.prefix, project, key, v) for v in sorted(variants)}


def held_only(held: Mapping[Any, Pointer | None]) -> dict[Any, Pointer]:
    """The pointers of ``held`` that a build holds."""
    return {k: found for k, found in held.items() if found is not None}


def stored_records(edge: Edge, project: str, key: str) -> list[Record]:
    """Every failure record of ``project`` at ``key``."""
    listed = edge.store.list(layout.failure_records(edge.prefix, project, key))
    found = (edge.store.get(where) for where, _ in listed)
    return [decode_record(s.body.decode("utf-8")) for s in found if s is not None]


def create_record(edge: Edge, r: Record) -> None:
    """Write ``r`` once: a record already at its place is left as it is."""
    where = layout.failure_record(edge.prefix, r.project, r.key, r.build.id)
    if edge.store.create(where, encode_record(r).encode("utf-8"), "application/json"):
        edge.log(f"wrote the failure record s3://{edge.store.bucket}/{where}")
    else:
        edge.log(f"s3://{edge.store.bucket}/{where} exists; left as it is")


def chain_records(edge: Edge, plan: Plan, causes: Iterable[Cause]) -> dict[str, Record]:
    """The record of this chain of every key the causes name, and the keys their records name."""
    found: dict[str, Record] = {}
    asked: set[str] = set()
    pending = list(causes)
    while pending:
        c = pending.pop()
        if c.key in asked:
            continue
        asked.add(c.key)
        r = chain_failure(stored_records(edge, c.project, c.key), plan.id)
        if r is not None:
            found[c.key] = r
            pending.extend(r.caused_by)
    return found


def root_arrival(job: Job, edge: Edge) -> int:
    """When the root build of this build's chain arrived at the gate, in epoch seconds.

    The runner sets ``snapshot-arrived`` in the first step of every gated
    build. A chain node reads it on its root build (``SNAPSHOT_PLAN``), as it
    reads the plan; a root is its own root.
    """
    return int(edge.agent.meta_data_get(ARRIVED, job.plan_id))


# ---------------------------------------------------------------------------
# The plan of a build
# ---------------------------------------------------------------------------


def chain_plan(job: Job, edge: Edge) -> Plan:
    """The plan of the build's chain, from the meta-data of its root build.

    A chain node names its root in ``SNAPSHOT_PLAN``; a root is its own root.
    """
    return decode_plan(edge.agent.meta_data_get(PLAN, job.plan_id))


def own_node(job: Job, edge: Edge, p: Plan) -> Node:
    """This build's node of the plan, which must plan it at the commit it checked out."""
    return planned_node(p, job.project, edge.checkout.head_commit())


def origin_of(job: Job, p: Plan, key: str) -> Origin:
    """What every record this build writes at ``key`` says of it."""
    return Origin(job.project, key, p.id, job.build)
