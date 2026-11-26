"""Failure records, and what the plan step and the last step decide over them.

A failure record says that one build of a key did not make some steps, and why. It is written
once, at ``<project>/<key>/failures/<build id>.json``, and never changed. A record never says
whether a step is made: its manifest does. A record only saves a waiting build from compiling
in vain, and lets a dependent name the cause.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace

from ..forms import epoch_of, iso_utc
from ..job import Build
from ..layout import Folder

# Why a build failed before it built: its generated pipeline breaks the rule, or it was asked
# for a step it does not make.
RULE = "rule"
NOT_DECLARED = "not_declared"

# Outcomes as ``buildkite-agent step get outcome`` gives them.
HARD_FAILED = "hard_failed"
PASSED = "passed"

# A record of another build counts as a failure this build shares only when it was recorded
# this many seconds after this build's root build began: an older one belongs to a build that
# ended before, and a new root build tries the key again.
SHARED_AFTER = 5


@dataclass(frozen=True)
class Failed:
    """A folder whose step did not pass: the step's Buildkite key, and its outcome, empty when
    it did not finish."""

    folder: Folder
    step_key: str
    outcome: str


@dataclass(frozen=True)
class Cause:
    """A dependency that did not make the folders a build needs. When the dependency refused
    them, ``template`` is the template of the entry that asked for them and ``takes`` the steps
    that entry names, None for every step."""

    project: str
    key: str
    missing: tuple[Folder, ...]
    template: str | None = None
    takes: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RefusedFolder:
    """A folder a build was asked for and does not make, and the step ids it makes for that
    platform."""

    folder: Folder
    promotes: tuple[str, ...]


@dataclass(frozen=True)
class Origin:
    """What every record a build writes says of it: its project, key, plan and build."""

    project: str
    key: str
    plan: str
    build: Build


@dataclass(frozen=True)
class Record:
    """``reason`` is one of the three above or None; ``shared_with`` the build whose failure
    this record copies."""

    project: str
    key: str
    plan: str
    build: Build
    recorded_at: str
    reason: str | None = None
    failed: tuple[Failed, ...] = ()
    caused_by: tuple[Cause, ...] = ()
    shared_with: Build | None = None
    refused: tuple[RefusedFolder, ...] = ()


def record(origin: Origin, now: float, **said) -> Record:
    """The record ``origin`` writes at ``now`` (epoch seconds), its entries sorted."""
    r = Record(origin.project, origin.key, origin.plan, origin.build, iso_utc(now), **said)
    causes = (replace(c, missing=tuple(sorted(set(c.missing)))) for c in r.caused_by)
    return replace(
        r,
        failed=tuple(sorted(r.failed, key=lambda f: (f.folder, f.step_key))),
        caused_by=tuple(sorted(causes, key=lambda c: (c.project, c.key))),
        refused=tuple(sorted(r.refused, key=lambda x: x.folder)),
    )


# ---------------------------------------------------------------------------
# The last step: the folders whose step did not pass
# ---------------------------------------------------------------------------


def unpassed(promotes: Mapping[Folder, str], outcomes: Mapping[str, str]) -> list[Folder]:
    """The folders of ``promotes`` (each with the Buildkite key of the step that promotes it)
    whose step did not pass, sorted. A step not in ``outcomes`` did not finish."""
    return sorted(f for f, step in promotes.items() if outcomes.get(step, "") != PASSED)


def last_step_record(
    origin: Origin,
    promotes: Mapping[Folder, str],
    outcomes: Mapping[str, str],
    made: Iterable[Folder],
    now: float,
) -> Record | None:
    """The record of the folders whose step did not pass and that are not ``made``; None when
    there is none."""
    done = frozenset(made)
    failed = tuple(
        Failed(f, promotes[f], outcomes.get(promotes[f], ""))
        for f in unpassed(promotes, outcomes)
        if f not in done
    )
    return record(origin, now, failed=failed) if failed else None


# ---------------------------------------------------------------------------
# The plan step: failures of the same key, and of this plan
# ---------------------------------------------------------------------------


def recorded_time(r: Record) -> float:
    return epoch_of(r.recorded_at)


def most_recent(records: Iterable[Record]) -> Record | None:
    return max(records, key=lambda r: (recorded_time(r), r.build.id), default=None)


def shared_failure(
    records: Iterable[Record], own_build: str, began: float, wanted: Iterable[Folder]
) -> Record | None:
    """The most recent record of another build of this key that hard failed on every folder of
    ``wanted`` more than ``SHARED_AFTER`` seconds after ``began``, when this build's root build began. A record that copies a failure (``shared_with``) never counts: the failure it
    copies is a record of the same key and counts by its own time."""
    needed = frozenset(wanted)
    return most_recent(
        r
        for r in records
        if needed
        and r.shared_with is None
        and r.build.id != own_build
        and recorded_time(r) > began + SHARED_AFTER
        and needed <= frozenset(f.folder for f in r.failed if f.outcome == HARD_FAILED)
    )


def shared_record(origin: Origin, source: Record, wanted: Iterable[Folder], now: float) -> Record:
    """The own record of a shared failure: ``source``'s entries of ``wanted``, and its build."""
    needed = frozenset(wanted)
    copied = tuple(f for f in source.failed if f.folder in needed)
    return record(origin, now, failed=copied, shared_with=source.build)


def failure_in_plan(records: Iterable[Record], plan: str) -> Record | None:
    """The most recent record that carries ``plan``. Within one plan a key is built at most
    once, so any record of the plan says that its key failed."""
    return most_recent(r for r in records if r.plan == plan)
