"""The snapshot pointer: which build holds one variant of a snapshot.

A snapshot of a project is one commit of it together with one snapshot of every
project it depends on. Its key is a hash of exactly that, so the key is a
function of git commits alone. The pointer of a variant of a snapshot is

    <prefix>/_snapshot/v1/pointers/<project>/<key>/<variant>.json

    {"schema":1,"project":"...","key":"v1-...","variant":"...",
     "git_ref":"refs/heads/...","commit":"<40 hex digits>",
     "build":{"id":"<uuid>","number":0,"url":"https://..."},
     "written_at":"<UTC, ISO 8601 to the millisecond>"}

The step that promotes the variant creates it, after its command passed and it
moved LATEST_SUCCESSFUL. The first build wins: a later build of the same key
finds the pointer and leaves it alone, and nothing changes or removes a
pointer. So everyone who asks for (project, key, variant) gets the same build,
on any platform and at any time.

Other builds read a pointer, long after it was written, to choose the build
they download. Its path is its identity: what it holds is read as it was
written, by the same code.

This is ordinary job code: the step that promotes runs on any platform, so
nothing here imports `snapshot`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from . import layout

SCHEMA = 1


@dataclass(frozen=True)
class Build:
    id: str
    number: int
    url: str


@dataclass(frozen=True)
class Pointer:
    project: str
    key: str
    variant: str
    git_ref: str  # the ref the variant's files were uploaded under
    commit: str  # git rev-parse HEAD of the checkout of the job that promoted
    build: Build
    written_at: str  # for people; nothing decides on it


def encode(p: Pointer) -> bytes:
    return json.dumps({"schema": SCHEMA, **asdict(p)}, separators=(",", ":")).encode("utf-8")


def decode(body: bytes) -> Pointer:
    """The pointer body holds, as encode wrote it."""
    fields = json.loads(body)
    build = fields["build"]
    return Pointer(
        project=fields["project"],
        key=fields["key"],
        variant=fields["variant"],
        git_ref=fields["git_ref"],
        commit=fields["commit"],
        build=Build(id=build["id"], number=build["number"], url=build["url"]),
        written_at=fields["written_at"],
    )


def find(store, prefix: str, project: str, key: str, variant: str) -> Pointer | None:
    """The pointer of a variant of a snapshot, or None when no build holds it yet.

    A read the store refuses raises as the store refused it.
    """
    stored = store.get(layout.snapshot_pointer(prefix, project, key, variant))
    return None if stored is None else decode(stored.body)


def create(store, prefix: str, p: Pointer) -> Pointer:
    """Create the pointer p unless a build holds it already; the pointer that holds
    afterwards: p, or the one another build wrote first, which stays as it is.

    That another build was first is no error: both builds made the variant from
    the same commits.
    """
    where = layout.snapshot_pointer(prefix, p.project, p.key, p.variant)
    if store.create(where, encode(p), "application/json"):
        return p
    return find(store, prefix, p.project, p.key, p.variant)
