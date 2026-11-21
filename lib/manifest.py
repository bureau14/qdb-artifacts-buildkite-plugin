"""manifest.json of a step's folder: its files, who uploaded them, and who promoted them.

    {"files": [{"path": "<path>", "size": <bytes>}, ...],
     "promoted": null or {"at": "<UTC ISO 8601, ms, Z>", "branch": "<branch>",
                          "id": "<uuid>", "number": <n>, "url": "<url>"},
     "uploaded": {"branch": "<branch>", "id": "<uuid>", "number": <n>, "url": "<url>"}}

Canonical JSON, ``files`` sorted by path. An upload writes it after its last
file, with ``promoted`` null: the step's files are captured. A promote writes
it back once, with ``promoted`` set: the step is made at its key, and the
first promote wins. A download reads it and takes the files it names: a
listing cannot tell a promoted upload from a failed or half-written one.

This is ordinary job code: the plan step reads the same manifests, so nothing
here imports ``snapshot``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from typing import Any

from . import layout
from .clients import s3_client
from .key import canonical_json
from .objects import Store


@dataclass(frozen=True)
class Build:
    id: str
    number: int
    url: str


@dataclass(frozen=True)
class Uploaded:
    """The build whose bytes a folder holds, and the branch it ran under."""

    branch: str
    build: Build


@dataclass(frozen=True)
class Promoted:
    """The build that promoted a folder, the branch it ran under, and when (``at``)."""

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
    listed = sorted(File(path, size) for path, size in files)
    return Manifest(tuple(listed), uploaded)


def promoted_by(m: Manifest, p: Promoted) -> Manifest:
    """``m`` written back by the promote ``p``."""
    return replace(m, promoted=p)


def is_made(m: Manifest | None) -> bool:
    """True for a manifest that says promoted: the step is made at its key."""
    return m is not None and m.promoted is not None


# ---------------------------------------------------------------------------
# Encoding and decoding
# ---------------------------------------------------------------------------


def build_document(b: Build) -> dict[str, Any]:
    return {"id": b.id, "number": b.number, "url": b.url}


def manifest_document(m: Manifest) -> dict[str, Any]:
    promoted = m.promoted
    return {
        "files": [{"path": f.path, "size": f.size} for f in m.files],
        "promoted": (
            None
            if promoted is None
            else {"at": promoted.at, "branch": promoted.branch, **build_document(promoted.build)}
        ),
        "uploaded": {"branch": m.uploaded.branch, **build_document(m.uploaded.build)},
    }


def encode_manifest(m: Manifest) -> bytes:
    return canonical_json(manifest_document(m))


def build_of(d: Mapping[str, Any]) -> Build:
    return Build(id=d["id"], number=d["number"], url=d["url"])


def decode_manifest(body: bytes) -> Manifest:
    """The manifest in ``body``, as ``encode_manifest`` wrote it."""
    d = json.loads(body)
    promoted = d["promoted"]
    return Manifest(
        files=tuple(File(f["path"], f["size"]) for f in d["files"]),
        uploaded=Uploaded(d["uploaded"]["branch"], build_of(d["uploaded"])),
        promoted=(
            None
            if promoted is None
            else Promoted(promoted["at"], promoted["branch"], build_of(promoted))
        ),
    )


# ---------------------------------------------------------------------------
# In the store
# ---------------------------------------------------------------------------


def read_manifest_at(store, step_folder: str) -> Manifest | None:
    """The manifest of a step's folder; None when no upload wrote one."""
    stored = store.get(layout.manifest(step_folder))
    return None if stored is None else decode_manifest(stored.body)


def write_manifest(store, step_folder: str, m: Manifest) -> None:
    store.put(layout.manifest(step_folder), encode_manifest(m), "application/json")


def read_manifest(cfg, auth, bucket, resolved) -> dict[str, Any] | None:
    """The manifest.json of a step that ``resolve_artifacts`` or ``download_artifacts``
    resolved, as a dict; None when the folder has none.

    It reads the manifest the way a download does.
    """
    m = read_manifest_at(Store(s3_client(cfg, auth), bucket), resolved.location)
    return None if m is None else manifest_document(m)
