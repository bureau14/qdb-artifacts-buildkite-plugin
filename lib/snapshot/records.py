"""Failure records, and what the gate steps decide over them (specification sections 7 and 9).

A failure record says that one build of a key did not make some steps, and
why. Only a build that has a key writes one, and it carries the plan of its
chain. It is written once into the bucket, at
``<project>/<key>/failures/<build id>.json``, and never changed; where it lives
and how it is read are the store's business. Here: the record as a value
(``Record``), its JSON (``encode_record``, ``decode_record``), and the
decisions of section 7 over records the caller has read:

- ``unpassed`` and ``leave_record``: the folders (platform and step id) whose
  step did not pass, whose manifests the leave step reads, and its record of
  those that are not made;
- ``shared_failure`` and ``shared_record``: step 7, the failure of another
  build of the same key since the root build of this chain started;
- ``chain_failure`` and ``caused_record``: step 9 and resume, a dependency
  that failed earlier in this chain;
- ``reason_record``: steps 1, 2 and 5, a build that failed before it built.

A record never says whether a step is made: its manifest does. A record only
saves a waiting build from compiling in vain, and lets a consumer name the
cause. Canonical JSON::

    {"build": {"id": "<uuid>", "number": <n>, "url": "<url>"},
     "caused_by": [{"key": "<key>", "missing": [{"step": "<step id>", "variant": "<platform>"}],
                    "project": "<slug>"}],
     "failed": [{"outcome": "hard_failed", "step": "<step id>", "step_key": "<Buildkite key>",
                 "variant": "<platform>"}],
     "plan": "<plan id>", "reason": "generator_failed" | "rule" | "not_declared" | null,
     "recorded_at": "<UTC ISO 8601, ms, Z>",
     "shared_with": null | {"id": "<uuid>", "number": <n>, "url": "<url>"}}

The project and the key of a record are those of its path; a ``Record`` holds
them as its reader found it.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from ..forms import iso_utc
from ..key import canonical_json
from ..layout import Folder
from ..manifest import Build, build_document, build_of

# Why a build failed before it built: its generator failed, its generated
# pipeline breaks the rule, or it was asked for a step it does not make.
GENERATOR_FAILED = "generator_failed"
RULE = "rule"
NOT_DECLARED = "not_declared"

# The outcome of a step that failed the build; the only one another build
# shares (step 7). ``buildkite-agent step get outcome`` gives it.
HARD_FAILED = "hard_failed"
PASSED = "passed"

# A record counts as a failure this build shares only when it was recorded this
# many seconds after the root build of this build's chain arrived at the gate:
# one recorded before belongs to a build that ended before this chain began,
# and a new root build tries the key again.
SHARED_AFTER = 5


@dataclass(frozen=True)
class Failed:
    """A folder whose step did not pass: the step's Buildkite key and its outcome, empty when
    unfinished."""

    folder: Folder
    step_key: str
    outcome: str


@dataclass(frozen=True)
class Cause:
    """A dependency that did not make the folders a build needs: its project, key and those."""

    project: str
    key: str
    missing: tuple[Folder, ...]


@dataclass(frozen=True)
class Record:
    """One failure record; see the module docstring. ``project`` and ``key`` are its path's."""

    project: str
    key: str
    plan: str
    build: Build
    reason: str | None
    failed: tuple[Failed, ...]
    caused_by: tuple[Cause, ...]
    shared_with: Build | None
    recorded_at: str


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


def compose_record(
    origin: Origin,
    now: float,
    reason: str | None,
    failed: Iterable[Failed],
    caused_by: Iterable[Cause],
    shared_with: Build | None,
) -> Record:
    """The record ``origin`` writes at ``now`` (epoch seconds), its entries sorted."""
    causes = (Cause(c.project, c.key, tuple(sorted(set(c.missing)))) for c in caused_by)
    return Record(
        project=origin.project,
        key=origin.key,
        plan=origin.plan,
        build=origin.build,
        reason=reason,
        failed=tuple(sorted(failed, key=lambda f: (f.folder, f.step_key))),
        caused_by=tuple(sorted(causes, key=lambda c: (c.project, c.key))),
        shared_with=shared_with,
        recorded_at=iso_utc(now),
    )


def reason_record(origin: Origin, reason: str, now: float) -> Record:
    """The record of a build that failed before it built: its generator, the rule, or a
    step it does not make."""
    return compose_record(origin, now, reason, (), (), None)


def caused_record(origin: Origin, causes: Sequence[Cause], now: float) -> Record:
    """The record of a build that a dependency left without the steps it needs."""
    return compose_record(origin, now, None, (), causes, None)


# ---------------------------------------------------------------------------
# Encoding and decoding
# ---------------------------------------------------------------------------


def folder_document(f: Folder) -> dict[str, str]:
    return {"step": f.step, "variant": f.platform}


def folder_of(d: Mapping[str, Any]) -> Folder:
    return Folder(d["variant"], d["step"])


def record_document(r: Record) -> dict[str, Any]:
    return {
        "build": build_document(r.build),
        "caused_by": [
            {"key": c.key, "missing": [folder_document(f) for f in c.missing], "project": c.project}
            for c in r.caused_by
        ],
        "failed": [
            {"outcome": f.outcome, "step_key": f.step_key, **folder_document(f.folder)}
            for f in r.failed
        ],
        "plan": r.plan,
        "reason": r.reason,
        "recorded_at": r.recorded_at,
        "shared_with": None if r.shared_with is None else build_document(r.shared_with),
    }


def encode_record(r: Record) -> str:
    """The record as canonical JSON, as it is written into the bucket."""
    return canonical_json(record_document(r)).decode("utf-8")


def decode_record(text: str, project: str, key: str) -> Record:
    """The record in ``text``, as ``encode_record`` wrote it, of ``project`` at ``key``."""
    d = json.loads(text)
    shared = d["shared_with"]
    return Record(
        project=project,
        key=key,
        plan=d["plan"],
        build=build_of(d["build"]),
        reason=d["reason"],
        failed=tuple(Failed(folder_of(f), f["step_key"], f["outcome"]) for f in d["failed"]),
        caused_by=tuple(
            Cause(c["project"], c["key"], tuple(folder_of(m) for m in c["missing"]))
            for c in d["caused_by"]
        ),
        shared_with=None if shared is None else build_of(shared),
        recorded_at=d["recorded_at"],
    )


# ---------------------------------------------------------------------------
# Leave: the folders whose step did not pass
# ---------------------------------------------------------------------------


def unpassed(promotes: Mapping[Folder, str], outcomes: Mapping[str, str]) -> list[Folder]:
    """The folders of ``promotes`` whose step did not pass, sorted.

    ``promotes`` maps each folder the build makes to the Buildkite key of the
    step that promotes it (meta-data ``snapshot-promotes``), ``outcomes`` the
    outcome of each step as read, a step not in it not finished. Only
    ``passed`` passes.
    """
    return sorted(f for f, step in promotes.items() if outcomes.get(step, "") != PASSED)


def leave_record(
    origin: Origin,
    promotes: Mapping[Folder, str],
    outcomes: Mapping[str, str],
    made: Iterable[Folder],
    now: float,
) -> Record | None:
    """The leave step's record; None when every folder of ``promotes`` passed or is made.

    ``promotes`` and ``outcomes`` as ``unpassed`` takes them, ``made`` the
    folders whose manifest at the key says promoted. A folder goes into the
    record when its step did not pass and it is not made.
    """
    done = frozenset(made)
    failed = [
        Failed(f, promotes[f], outcomes.get(promotes[f], ""))
        for f in unpassed(promotes, outcomes)
        if f not in done
    ]
    return compose_record(origin, now, None, failed, (), None) if failed else None


# ---------------------------------------------------------------------------
# Step 7: the failure of another build of the key since this chain began
# ---------------------------------------------------------------------------


def recorded_time(r: Record) -> float:
    """When ``r`` was recorded, in epoch seconds to the millisecond: its ``recorded_at``."""
    return datetime.datetime.strptime(r.recorded_at, "%Y-%m-%dT%H:%M:%S.%f%z").timestamp()


def _most_recent(records: Iterable[Record]) -> Record | None:
    return max(records, key=lambda r: (recorded_time(r), r.build.id), default=None)


def _hard_failed(r: Record) -> frozenset[Folder]:
    return frozenset(f.folder for f in r.failed if f.outcome == HARD_FAILED)


def shared_failure(
    records: Iterable[Record], own_build: str, arrived: float, wanted: Iterable[Folder]
) -> Record | None:
    """The record of another build of this key that failed on ``wanted`` since this chain began.

    ``records`` are the failure records of the key, ``own_build`` this build's
    uuid, ``arrived`` the ``snapshot-arrived`` of the root build of this
    build's chain (epoch seconds), ``wanted`` the folders it needs that are
    not made. A record counts when another build recorded it more than
    ``SHARED_AFTER`` seconds after ``arrived``, to the millisecond
    (``recorded_time``), and every wanted folder has the outcome
    ``hard_failed`` in it. With nothing wanted nothing is shared. Of several,
    the most recent.

    A record that shares a failure (``shared_with``) never counts: it is
    written when its build shares, later than the failure it copies, which
    is a record of the same key and counts by its own time.
    """
    needed = frozenset(wanted)
    return _most_recent(
        r
        for r in records
        if needed
        and r.shared_with is None
        and r.build.id != own_build
        and recorded_time(r) > arrived + SHARED_AFTER
        and needed <= _hard_failed(r)
    )


def shared_record(origin: Origin, source: Record, wanted: Iterable[Folder], now: float) -> Record:
    """The own record of a shared failure: ``source``'s entries of ``wanted``, and its build."""
    needed = frozenset(wanted)
    copied = [f for f in source.failed if f.folder in needed]
    return compose_record(origin, now, None, copied, (), source.build)


# ---------------------------------------------------------------------------
# Step 9 and resume: a dependency that failed in this chain
# ---------------------------------------------------------------------------


def chain_failure(records: Iterable[Record], plan: str) -> Record | None:
    """The record of a dependency's key that carries ``plan``; None when there is none.

    Every record states a failure, and a key built again in the same chain
    fails the same way, so any record of the chain stops it. Of several, the
    most recent.
    """
    return _most_recent(r for r in records if r.plan == plan)
