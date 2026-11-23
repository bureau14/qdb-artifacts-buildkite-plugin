"""manifest.json of a step's folder: its files, who uploaded them, and who promoted them.

    {"files": [{"path": "<path>", "size": <bytes>}, ...],
     "promoted": null or {"at": "<UTC ISO 8601, ms, Z>", "branch": "<branch>", "build": {...}},
     "uploaded": {"branch": "<branch>", "build": {"id": "<uuid>", "number": <n>, "url": "<url>"}}}

An upload writes it after its last file, with ``promoted`` null: the step's files are
captured. A promote writes it back once, with ``promoted`` set: the step is made at its key.
A download takes the files it names: a listing cannot tell a promoted upload from a
half-written one.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import layout
from .codec import decoded, encoded
from .job import Build


@dataclass(frozen=True)
class Uploaded:
    branch: str
    build: Build


@dataclass(frozen=True)
class Promoted:
    at: str
    branch: str
    build: Build


@dataclass(frozen=True, order=True)
class File:
    path: str
    size: int


@dataclass(frozen=True)
class Manifest:
    files: tuple[File, ...]
    uploaded: Uploaded
    promoted: Promoted | None = None


def captured(files: Iterable[tuple[str, int]], uploaded: Uploaded) -> Manifest:
    """The manifest an upload of ``files`` (path and size) writes: captured, not promoted."""
    return Manifest(tuple(sorted(File(path, size) for path, size in files)), uploaded)


def is_made(m: Manifest | None) -> bool:
    """True for a manifest that says promoted: the step is made at its key."""
    return m is not None and m.promoted is not None


def read_manifest_at(store, step_folder: str) -> Manifest | None:
    """The manifest of a step's folder; None when no upload wrote one."""
    body = store.get(layout.manifest(step_folder))
    return None if body is None else decoded(Manifest, body)


def write_manifest(store, step_folder: str, m: Manifest) -> None:
    store.put(layout.manifest(step_folder), encoded(m), "application/json")


def read_manifest(resolved) -> dict[str, Any]:
    """The manifest.json of a step that ``resolve_artifacts`` or ``download_artifacts``
    resolved, as a dict."""
    return json.loads(encoded(resolved.manifest))
