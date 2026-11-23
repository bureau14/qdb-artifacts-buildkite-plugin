"""The key of a build, and ``key.json``, the small file it is a hash of.

A key says what one build of one project was built from: the project's commit, a dash, and
the first 32 hex digits of the SHA-256 of ``key.json``, the canonical JSON of::

    {"commit": "<40 hex>", "deps": [["<project>", "<key>"], ...], "project": "<slug>"}

with ``"build": "<build id>"`` besides for a key that only one build has. ``deps`` holds the
key of every build it downloads from another project, so reading ``key.json`` down from any
key gives every commit of the tree.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from . import layout
from .codec import canonical_json
from .job import Job, checkout_commit

DIGEST_LENGTH = 32


@dataclass(frozen=True)
class KeyJson:
    """What a key stands for. ``deps`` holds ``(project, key)`` pairs; ``build`` is the id of
    the one build whose own key this is, None for a key of commits only."""

    project: str
    commit: str
    deps: tuple[tuple[str, str], ...] = ()
    build: str | None = None


def key_document(k: KeyJson) -> dict[str, Any]:
    """``key.json`` as data: ``deps`` sorted, each pair once; ``build`` only when there is one."""
    document = {
        "commit": k.commit,
        "deps": [list(pair) for pair in sorted(set(k.deps))],
        "project": k.project,
    }
    return document if k.build is None else {**document, "build": k.build}


def encode_key_json(k: KeyJson) -> bytes:
    return canonical_json(key_document(k))


def key_of(k: KeyJson) -> str:
    digest = hashlib.sha256(encode_key_json(k)).hexdigest()[:DIGEST_LENGTH]
    return f"{k.commit}-{digest}"


def commit_of(key: str) -> str:
    return key.rsplit("-", 1)[0]


def create_key_json(store, key: str, k: KeyJson) -> bool:
    """Create ``key.json`` of ``key`` unless it is there; whether this call did. Every writer
    of one key writes the same bytes."""
    where = layout.key_json(store.prefix, k.project, key)
    return store.create(where, encode_key_json(k), "application/json")


@dataclass(frozen=True)
class OwnKey:
    """The key of the job's build; ``stands_for`` is the ``key.json`` to create when the job
    computed the key, None when the plan step wrote it."""

    key: str
    stands_for: KeyJson | None


def own_key(job: Job, planned: str | None) -> OwnKey:
    """The plan step's key ``planned``; without one, a key of this build alone: its commit, no
    dependency, and its build id."""
    if planned:
        return OwnKey(planned, None)
    k = KeyJson(job.project, checkout_commit(), (), job.build.id)
    return OwnKey(key_of(k), k)
