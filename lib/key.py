"""The key of a build, and ``key.json``, the small file it is a hash of.

A key says what one build of one project was built from: the project's
commit, a dash, and the first 32 hex digits of the SHA-256 of ``key.json``,
the canonical JSON of::

    {"commit": "<40 hex>", "deps": [["<project>", "<key>"], ...], "project": "<slug>"}

with ``"build": "<build id>"`` besides for a key of one build's own, which no
other build has. ``deps`` holds, for every project the build downloads from,
the key of each build it takes, each pair once, sorted. They are keys and not
commits, so each ``key.json`` is one level deep, and reading them down from
any key gives every commit of the tree.

The plan step computes the keys of its build and of the builds it starts, and
for a download that takes the latest successful build of another project the
key it reads from that project's latest files is a dependency's; a build
without a plan step computes its key in each job, with its own build id in it.
Jobs compute keys as well as the plan step, so nothing here imports
``snapshot``.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from . import layout

DIGEST_LENGTH = 32


def canonical_json(v: Any) -> bytes:
    """``v`` as canonical JSON: members sorted, no white space, non-ASCII as itself, UTF-8."""
    text = json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


@dataclass(frozen=True)
class KeyJson:
    """What a key stands for: a project at a commit, and the key taken of each dependency.

    ``deps`` holds ``(project, key)`` pairs; ``build`` is the id of the one
    build whose own key this is, None for a key of commits only.
    """

    project: str
    commit: str
    deps: tuple[tuple[str, str], ...] = ()
    build: str | None = None


def key_document(k: KeyJson) -> dict[str, Any]:
    """``key.json`` as data: ``deps`` sorted by project, then key, each pair once."""
    document: dict[str, Any] = {
        "commit": k.commit,
        "deps": [list(pair) for pair in sorted(set(k.deps))],
        "project": k.project,
    }
    if k.build is not None:
        document["build"] = k.build
    return document


def encode_key_json(k: KeyJson) -> bytes:
    """The bytes of ``key.json``, the bytes the key is a hash of."""
    return canonical_json(key_document(k))


def key_of(k: KeyJson) -> str:
    """The key: the commit, a dash, and the first 32 hex digits of the SHA-256 of ``key.json``."""
    digest = hashlib.sha256(encode_key_json(k)).hexdigest()[:DIGEST_LENGTH]
    return f"{k.commit}-{digest}"


def commit_of(key: str) -> str:
    """The commit a key names: what ``key_of`` wrote before the dash."""
    return key.rsplit("-", 1)[0]


def create_key_json(store, prefix: str, key: str, k: KeyJson) -> bool:
    """Create ``key.json`` of ``key`` unless it is there: ``k``, what the key stands for, whose
    hash it is; whether this call did. ``key`` is the build's key as its one source gave it,
    so no writer computes it a second time.

    Every writer of one key writes the same bytes, so one that finds it there
    is no error.
    """
    where = layout.key_json(prefix, k.project, key)
    return store.create(where, encode_key_json(k), "application/json")
