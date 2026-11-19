"""Canonical JSON and the key of a snapshot (specification section 4).

A snapshot of a project is one commit of it together with one snapshot of
every project it depends on. Its key is a hash of exactly that, so it is a
function of git commits alone: two builds of one key are interchangeable, and
the first usable build of a key is reused by everyone.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

KEY_PREFIX = "v1-"
DIGEST_LENGTH = 32


def canonical_json(v: Any) -> bytes:
    """Serialise ``v`` as canonical JSON: sorted keys, no whitespace, UTF-8.

    Non-ASCII characters are written as themselves, not escaped, so the bytes
    are the UTF-8 encoding of the text.
    """
    text = json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return text.encode("utf-8")


def digest(v: Any) -> str:
    """Return the first 32 hex digits of the sha256 of the canonical JSON of ``v``."""
    return hashlib.sha256(canonical_json(v)).hexdigest()[:DIGEST_LENGTH]


def snapshot_key(
    project: str, commit: str, deps: Mapping[str, str], rebuild: str | None = None
) -> str:
    """Return the key of the snapshot of ``project`` at ``commit``.

    ``deps`` maps each direct snapshot dependency's project to its key. The
    dependencies enter the key as a list of ``[project, key]`` pairs sorted by
    project. The formula never changes: a key is the name its pointers are
    stored under, and another formula would find none of them.

    ``rebuild`` is the id of the plan that forces this build again
    (``SNAPSHOT_REBUILD``). It enters the hashed object as the member
    ``"rebuild"``, so a forced build gets a key no earlier build has; without
    it the object, and so the key, is that of every build of the same commits.
    """
    pairs = [[d, deps[d]] for d in sorted(deps)]
    fields: dict[str, Any] = {"project": project, "commit": commit, "deps": pairs}
    if rebuild is not None:
        fields["rebuild"] = rebuild
    return KEY_PREFIX + digest(fields)
