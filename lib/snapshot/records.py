"""Failure records, and what the gate steps decide over them (specification sections 7 and 9).

A failure record says that one build of a key did not give some variants, and
why. Only a build that has a key writes one, and it carries the plan of its
chain. It is written once into the bucket and never changed; where it lives
and how it is read are the store's business. Here: the record as a value
(``Record``), its JSON (``encode_record``, ``decode_record``, which checks the
form of every field), and the decisions of section 7 over records the caller
has read:

- ``unpassed`` and ``leave_record``: the variants whose step did not pass,
  whose pointers the leave step reads, and its record of those that have no
  pointer;
- ``shared_failure`` and ``shared_record``: step 8, the failure of a build of
  the same key that this build waited behind at the gate;
- ``chain_failure`` and ``caused_record``: step 10 and resume, a dependency
  that failed earlier in this chain;
- ``reason_record``: steps 1, 3 and 6, a build that failed before it built.

A record never says whether a variant exists: the pointer does. A record only
saves a waiting build from compiling in vain, and lets a consumer name the
cause::

    {"schema": 1, "project": "...", "key": "v1-...", "plan": "<root build uuid>",
     "build": {"id": "...", "number": 0, "url": "https://...", "pipeline": "..."},
     "reason": "generator_failed" | "rule" | "not_declared" | null,
     "failed": [{"variant": "...", "step": "...", "outcome": "hard_failed"}],
     "caused_by": [{"project": "...", "key": "v1-...", "variants": ["..."]}],
     "shared_with": {"id": ..., "number": ..., "url": ..., "pipeline": ...} | null,
     "recorded_at": "<UTC ISO 8601>", "recorded_at_epoch": 0}

A build is ``pointer.Build``, as a pointer names it. Its ``pipeline`` in the
JSON is the record's project: the build that writes a record of a key, and a
build whose failure it shares, are builds of that key's project.
"""

from __future__ import annotations

import datetime
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Callable

from ..forms import is_build_id, is_https_url, is_iso_utc, is_key, is_slug, is_variant, iso_utc
from ..pointer import Build
from .canonical import canonical_json
from .members import member
from .shown import shown_text, shown_value

SCHEMA = 1

# Why a build failed before it built: its generator failed, its generated
# pipeline breaks the rule, or it was asked for a variant it does not promote.
GENERATOR_FAILED = "generator_failed"
RULE = "rule"
NOT_DECLARED = "not_declared"
REASONS = (GENERATOR_FAILED, RULE, NOT_DECLARED)

# The outcome of a step that failed the build; the only one another build
# shares (step 8). ``buildkite-agent step get outcome`` gives it.
HARD_FAILED = "hard_failed"
PASSED = "passed"

# A record counts as a failure this build waited behind only when it was
# recorded this many seconds after the build arrived at the gate: one recorded
# before belongs to a build that ended before this one could wait for it.
SHARED_AFTER = 5


class RecordError(ValueError):
    """A value or a text that is not a failure record of this schema."""


@dataclass(frozen=True)
class Failed:
    """A variant whose step did not pass: the step's key and its outcome, empty when unfinished."""

    variant: str
    step: str
    outcome: str


@dataclass(frozen=True)
class Cause:
    """A dependency that did not give the variants a build needs: its project, key and those."""

    project: str
    key: str
    variants: tuple[str, ...]


@dataclass(frozen=True)
class Record:
    """One failure record; see the module docstring."""

    project: str
    key: str
    plan: str
    build: Build
    reason: str | None
    failed: tuple[Failed, ...]
    caused_by: tuple[Cause, ...]
    shared_with: Build | None
    recorded_at: str
    recorded_at_epoch: int


@dataclass(frozen=True)
class Origin:
    """What every record a build writes says of it: its project, key, plan and build."""

    project: str
    key: str
    plan: str
    build: Build


# ---------------------------------------------------------------------------
# Composing
# ---------------------------------------------------------------------------


def _stated(r: Record) -> Record:
    """A record that states a failure: a reason, failed variants or a cause."""
    if r.reason is None and not r.failed and not r.caused_by:
        raise RecordError(f"record of {r.project} build {r.build.number}: states no failure")
    return r


def compose_record(
    origin: Origin,
    now: float,
    reason: str | None,
    failed: Iterable[Failed],
    caused_by: Iterable[Cause],
    shared_with: Build | None,
) -> Record:
    """The record ``origin`` writes at ``now`` (epoch seconds), its entries sorted.

    A record that states no failure is a ``RecordError``: a record is never
    replaced, and no reader accepts one (``decode_record``).
    """
    if reason is not None and reason not in REASONS:
        raise ValueError(f"reason {shown_value(reason)} is none of {', '.join(REASONS)}")
    causes = (Cause(c.project, c.key, tuple(sorted(set(c.variants)))) for c in caused_by)
    return _stated(
        Record(
            project=origin.project,
            key=origin.key,
            plan=origin.plan,
            build=origin.build,
            reason=reason,
            failed=tuple(sorted(failed, key=lambda f: (f.variant, f.step))),
            caused_by=tuple(sorted(causes, key=lambda c: (c.project, c.key))),
            shared_with=shared_with,
            recorded_at=iso_utc(now),
            recorded_at_epoch=math.floor(now),
        )
    )


def reason_record(origin: Origin, reason: str, now: float) -> Record:
    """The record of a build that failed before it built, for one of ``REASONS``."""
    return compose_record(origin, now, reason, (), (), None)


def caused_record(origin: Origin, causes: Sequence[Cause], now: float) -> Record:
    """The record of a build that a dependency left without the variants it needs."""
    return compose_record(origin, now, None, (), causes, None)


# ---------------------------------------------------------------------------
# Encoding and decoding
# ---------------------------------------------------------------------------


def build_document(b: Build, pipeline: str) -> dict[str, Any]:
    return {"id": b.id, "number": b.number, "url": b.url, "pipeline": pipeline}


def record_document(r: Record) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "project": r.project,
        "key": r.key,
        "plan": r.plan,
        "build": build_document(r.build, r.project),
        "reason": r.reason,
        "failed": [{"variant": f.variant, "step": f.step, "outcome": f.outcome} for f in r.failed],
        "caused_by": [
            {"project": c.project, "key": c.key, "variants": list(c.variants)} for c in r.caused_by
        ],
        "shared_with": None if r.shared_with is None else build_document(r.shared_with, r.project),
        "recorded_at": r.recorded_at,
        "recorded_at_epoch": r.recorded_at_epoch,
    }


def encode_record(r: Record) -> str:
    """The record as compact JSON with sorted keys, as it is written into the bucket."""
    return canonical_json(record_document(r)).decode("utf-8")


_STEP_KEY = re.compile(r"[!-~]+")
_OUTCOME = re.compile(r"[a-z_]*")


def is_step_key(text: str) -> bool:
    """True for a step key as a record holds it: printable ASCII without white space."""
    return _STEP_KEY.fullmatch(text) is not None


def is_outcome(text: str) -> bool:
    """True for a step outcome: lowercase letters and ``_``; empty for a step not finished."""
    return _OUTCOME.fullmatch(text) is not None


def _member(d: Any, name: str, kind: type, where: str) -> Any:
    """``d[name]``, which must be of ``kind``; ``RecordError`` naming ``where`` otherwise."""
    return member(d, name, kind, where, RecordError)


def _text(d: Any, name: str, form: Callable[[str], bool], what: str, where: str) -> str:
    v = _member(d, name, str, where)
    if not form(v):
        raise RecordError(f"{where}.{name}: {shown_value(v)} is not {what}")
    return v


def _count(d: Any, name: str, where: str) -> int:
    v = _member(d, name, int, where)
    if v < 0:
        raise RecordError(f"{where}.{name}: {v} is negative")
    return v


def _list(d: Any, name: str, where: str) -> list[Any]:
    return _member(d, name, list, where)


def _build(d: Any, project: str, where: str) -> Build:
    """A build of ``project``, as a record names it."""
    build = Build(
        id=_text(d, "id", is_build_id, "a build id", where),
        number=_count(d, "number", where),
        url=_text(d, "url", is_https_url, "an https URL", where),
    )
    pipeline = _member(d, "pipeline", str, where)
    if pipeline != project:
        shown = shown_value(pipeline)
        raise RecordError(f"{where}.pipeline: {shown} is not the record's project {project}")
    return build


def _failed(d: Any, where: str) -> Failed:
    return Failed(
        variant=_text(d, "variant", is_variant, "a variant", where),
        step=_text(d, "step", is_step_key, "a step key", where),
        outcome=_text(d, "outcome", is_outcome, "a step outcome", where),
    )


def _variant(v: Any, where: str) -> str:
    if not is_variant(v):
        raise RecordError(f"{where}: {shown_value(v)} is not a variant")
    return v


def _cause(d: Any, where: str) -> Cause:
    variants = _list(d, "variants", where)
    if not variants:
        raise RecordError(f"{where}.variants: empty")
    return Cause(
        project=_text(d, "project", is_slug, "a pipeline slug", where),
        key=_text(d, "key", is_key, "a key", where),
        variants=tuple(_variant(v, f"{where}.variants") for v in variants),
    )


def _reason(d: Any) -> str | None:
    if not isinstance(d, dict) or "reason" not in d:
        raise RecordError("record: no reason")
    reason = d["reason"]
    if reason is not None and reason not in REASONS:
        raise RecordError(f"record.reason: {shown_value(reason)} is none of {', '.join(REASONS)}")
    return reason


def _shared_with(d: Any, project: str) -> Build | None:
    if not isinstance(d, dict) or "shared_with" not in d:
        raise RecordError("record: no shared_with")
    shared = d["shared_with"]
    return None if shared is None else _build(shared, project, "record.shared_with")


def decode_record(text: str) -> Record:
    """The record in ``text``, as ``encode_record`` wrote it; ``RecordError`` when it is not one.

    Every text of the record must have its form: a project a pipeline slug,
    the pipeline of a build the record's project, a key a key, the plan and a
    build id a build uuid, a URL an https URL, a variant a variant
    (``forms.is_variant``), a step a step key, an outcome lowercase letters and
    ``_``; a number is an integer that is not negative. Members of no meaning
    here are not read.
    """
    try:
        d = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise RecordError(f"record: not JSON: {shown_text(str(exc))}") from exc
    if not isinstance(d, dict) or type(d.get("schema")) is not int or d["schema"] != SCHEMA:
        shown = shown_value(d.get("schema") if isinstance(d, dict) else type(d).__name__)
        raise RecordError(f"record: not a failure record of schema {SCHEMA} ({shown})")
    failed = _list(d, "failed", "record")
    causes = _list(d, "caused_by", "record")
    project = _text(d, "project", is_slug, "a pipeline slug", "record")
    return _stated(
        Record(
            project=project,
            key=_text(d, "key", is_key, "a key", "record"),
            plan=_text(d, "plan", is_build_id, "a build id", "record"),
            build=_build(_member(d, "build", dict, "record"), project, "record.build"),
            reason=_reason(d),
            failed=tuple(_failed(f, f"record.failed[{i}]") for i, f in enumerate(failed)),
            caused_by=tuple(_cause(c, f"record.caused_by[{i}]") for i, c in enumerate(causes)),
            shared_with=_shared_with(d, project),
            recorded_at=_text(d, "recorded_at", is_iso_utc, "a UTC ISO 8601 time", "record"),
            recorded_at_epoch=_count(d, "recorded_at_epoch", "record"),
        )
    )


# ---------------------------------------------------------------------------
# Leave: the variants whose step did not pass
# ---------------------------------------------------------------------------


def unpassed(promotes: Mapping[str, str], outcomes: Mapping[str, str]) -> list[str]:
    """The variants of ``promotes`` whose step did not pass, sorted.

    ``promotes`` is ``{variant: step key}`` (meta-data ``snapshot-promotes``),
    ``outcomes`` the outcome of each step as read, a step not in it not
    finished. Only ``passed`` passes.
    """
    return sorted(v for v, step in promotes.items() if outcomes.get(step, "") != PASSED)


def leave_record(
    origin: Origin,
    promotes: Mapping[str, str],
    outcomes: Mapping[str, str],
    present: Iterable[str],
    now: float,
) -> Record | None:
    """The leave step's record; None when every variant of ``promotes`` passed or exists.

    ``promotes`` and ``outcomes`` as ``unpassed`` takes them, ``present`` the
    variants that have a pointer at the key. A variant goes into the record
    when its step did not pass and it has no pointer.
    """
    exists = frozenset(present)
    failed = [
        Failed(v, promotes[v], outcomes.get(promotes[v], ""))
        for v in unpassed(promotes, outcomes)
        if v not in exists
    ]
    return compose_record(origin, now, None, failed, (), None) if failed else None


# ---------------------------------------------------------------------------
# Step 8: the failure of a build this build waited behind
# ---------------------------------------------------------------------------


def recorded_time(r: Record) -> float:
    """When ``r`` was recorded, in epoch seconds to the millisecond: its ``recorded_at``.

    ``recorded_at_epoch`` is the same time to the second, too coarse for the
    five seconds of step 8.
    """
    return datetime.datetime.strptime(r.recorded_at, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()


def _most_recent(records: Iterable[Record]) -> Record | None:
    return max(records, key=lambda r: (recorded_time(r), r.build.id), default=None)


def _hard_failed(r: Record) -> frozenset[str]:
    return frozenset(f.variant for f in r.failed if f.outcome == HARD_FAILED)


def shared_failure(
    records: Iterable[Record], own_build: str, arrived: float, wanted: Iterable[str]
) -> Record | None:
    """The record of a build of this key that failed on ``wanted`` while this build waited.

    ``records`` are the failure records of the key, ``own_build`` this build's
    uuid, ``arrived`` its ``snapshot-arrived`` (epoch seconds), ``wanted`` the
    variants it needs that have no pointer. A record counts when another build
    recorded it more than ``SHARED_AFTER`` seconds after ``arrived``, to the
    millisecond (``recorded_time``), and every wanted variant has the outcome
    ``hard_failed`` in it. With nothing wanted nothing is shared. Of several,
    the most recent.
    """
    needed = frozenset(wanted)
    return _most_recent(
        r
        for r in records
        if needed
        and r.build.id != own_build
        and recorded_time(r) > arrived + SHARED_AFTER
        and needed <= _hard_failed(r)
    )


def shared_record(origin: Origin, source: Record, wanted: Iterable[str], now: float) -> Record:
    """The own record of a shared failure: ``source``'s entries of ``wanted``, and its build."""
    needed = frozenset(wanted)
    copied = [f for f in source.failed if f.variant in needed]
    return compose_record(origin, now, None, copied, (), source.build)


# ---------------------------------------------------------------------------
# Step 10 and resume: a dependency that failed in this chain
# ---------------------------------------------------------------------------


def chain_failure(records: Iterable[Record], plan: str) -> Record | None:
    """The record of a dependency's key that carries ``plan``; None when there is none.

    Every record states a failure, and a key built again in the same chain
    fails the same way, so any record of the chain stops it. Of several, the
    most recent.
    """
    return _most_recent(r for r in records if r.plan == plan)
