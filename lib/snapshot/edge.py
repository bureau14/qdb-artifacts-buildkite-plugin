"""What every gate step reads and changes (``Edge``), and the reads they share through it.

The edge is the store as a job opens it, ``buildkite-agent``, the build's own
checkout, the clock and the log, each behind the interface of its module: the
decisions of the gate steps are plain functions of what these return, and a
test hands them fakes. The plan step reaches further; ``enter.PlanEdge`` adds
its parts.

The steps keep no state but these marks on their own build, which a retried
step and leave read back:

    snapshot-mode                  reused, built or failed
    snapshot-promotes              {variant: step key} of the steps that promote
    snapshot-uploaded-<step key>   this build's id, once the step has uploaded

The reads here are those of more than one step: meta-data of this build or of
another, the pointers at a key, the failure records of a key, the arrival at
the gate, and the plan of the build's chain.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .. import layout, pointer
from ..clients import resolve_object_auth
from ..config import load_store_config
from ..errors import ArtifactsError
from ..objects import Store, destination_store
from ..pointer import Pointer
from .agent import Agent, MetaDataAbsent
from .git import Checkout
from .job import Job
from .plan import Node, Plan, PlanError, decode_plan, planned_node
from .records import Cause, Origin, Record, RecordError, chain_failure, decode_record, encode_record
from .shown import shown_text, shown_value

# The marks on the build, and the meta-data a step reads.
MODE = "snapshot-mode"
REUSED, BUILT, FAILED = "reused", "built", "failed"
PROMOTES = "snapshot-promotes"
UPLOADED = "snapshot-uploaded-"
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


def meta_data(edge: Edge, key: str, build: str | None = None) -> str | None:
    """The value of ``key`` on this build, or on ``build``; None when it is not set."""
    try:
        return edge.agent.meta_data_get(key, build)
    except MetaDataAbsent:
        return None


def pointers_at(
    edge: Edge, project: str, key: str, variants: Iterable[str]
) -> dict[str, Pointer | None]:
    """The pointer of each variant of ``project`` at ``key``; None where no build holds it."""
    return {v: pointer.find(edge.store, edge.prefix, project, key, v) for v in sorted(variants)}


def held_only(held: Mapping[Any, Pointer | None]) -> dict[Any, Pointer]:
    """The pointers of ``held`` that a build holds."""
    return {k: found for k, found in held.items() if found is not None}


def decoded_record(edge: Edge, where: str, project: str, key: str) -> Record | None:
    """The record stored at ``where``; None, with a line in the log, when it is none of the key."""
    stored = edge.store.get(where)
    if stored is None:
        return None
    try:
        r = decode_record(stored.body.decode("utf-8"))
    except (RecordError, UnicodeDecodeError) as exc:
        edge.log(f"skipped s3://{edge.store.bucket}/{where}: {shown_text(str(exc))}")
        return None
    if (r.project, r.key) != (project, key):
        edge.log(f"skipped s3://{edge.store.bucket}/{where}: a record of {r.project} at {r.key}")
        return None
    return r


def stored_records(edge: Edge, project: str, key: str) -> list[Record]:
    """Every failure record of ``project`` at ``key`` that decodes."""
    listed = edge.store.list(layout.failure_records(edge.prefix, project, key))
    found = (decoded_record(edge, where, project, key) for where, _ in listed)
    return [r for r in found if r is not None]


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


def arrival(edge: Edge) -> int:
    """When this build arrived at the gate: ``snapshot-arrived``, epoch seconds the runner set."""
    value = meta_data(edge, ARRIVED)
    if value is None or not re.fullmatch(r"[0-9]+", value):
        raise ArtifactsError(
            f"{ARRIVED} is {shown_value(value)}, not epoch seconds: the runner sets it when it "
            "prints the gate"
        )
    return int(value)


# ---------------------------------------------------------------------------
# The plan of a build
# ---------------------------------------------------------------------------


def chain_plan(job: Job, edge: Edge) -> Plan:
    """The plan of a chain node's chain, read from the meta-data of its root build."""
    if not job.plan_id:
        raise ArtifactsError("SNAPSHOT_PLAN is empty: a chain node names the build of its plan")
    text = meta_data(edge, PLAN, job.plan_id)
    if text is None:
        raise ArtifactsError(f"build {shown_value(job.plan_id)} holds no {PLAN}")
    p = decode_plan(text)
    if p.id != job.plan_id:
        raise PlanError(f"build {job.plan_id} holds the plan {p.id}")
    return p


def own_plan(job: Job, edge: Edge) -> Plan | None:
    """The plan a root wrote on its own build; None before it wrote one."""
    text = meta_data(edge, PLAN)
    if text is None:
        return None
    p = decode_plan(text)
    if p.id != job.build.id or p.root.project != job.project:
        raise PlanError(f"this build holds the plan {p.id} of {p.root.project}")
    return p


def own_node(job: Job, edge: Edge, p: Plan) -> Node:
    """This build's node of the plan, which must plan it at the commit it checked out."""
    return planned_node(p, job.project, edge.checkout.head_commit())


def origin_of(job: Job, p: Plan, key: str) -> Origin:
    """What every record this build writes at ``key`` says of it."""
    return Origin(job.project, key, p.id, job.build)
