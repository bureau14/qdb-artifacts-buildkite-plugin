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
they download, so every field is checked for its form before a pointer is
written and again when it is read.

This is ordinary job code: the step that promotes runs on any platform, so
nothing here imports `snapshot`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from . import layout
from .errors import ArtifactsError
from .forms import (
    is_build_id,
    is_count,
    is_full_ref,
    is_https_url,
    is_iso_utc,
    is_key,
    is_object_id,
    is_slug,
    is_variant,
)

SCHEMA = 1
MEMBERS = ("schema", "project", "key", "variant", "git_ref", "commit", "build", "written_at")
BUILD_MEMBERS = ("id", "number", "url")


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


# ---------------------------------------------------------------------------
# The form of each field
# ---------------------------------------------------------------------------


def fields_and_forms(p: Pointer):
    return (
        ("project", p.project, is_slug, "a pipeline slug"),
        ("key", p.key, is_key, "a snapshot key"),
        ("variant", p.variant, is_variant, "a variant name"),
        ("git_ref", p.git_ref, is_full_ref, "a full branch or tag ref"),
        ("commit", p.commit, is_object_id, "a commit id of 40 hex digits"),
        ("build.id", p.build.id, is_build_id, "a build uuid"),
        ("build.number", p.build.number, is_count, "a build number"),
        ("build.url", p.build.url, is_https_url, "an https URL"),
        ("written_at", p.written_at, is_iso_utc, "a UTC time to the millisecond"),
    )


def shown(value) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def checked(p: Pointer) -> Pointer:
    """p, when every field has its form; else ArtifactsError naming the first that has not."""
    for name, value, has_form, form in fields_and_forms(p):
        if not has_form(value):
            raise ArtifactsError(f"snapshot pointer: {name} {shown(value)} is not {form}")
    return p


# ---------------------------------------------------------------------------
# The object
# ---------------------------------------------------------------------------


def encode(p: Pointer) -> bytes:
    return json.dumps({"schema": SCHEMA, **asdict(p)}, separators=(",", ":")).encode("utf-8")


def decode(body: bytes) -> Pointer:
    """The pointer body holds; ArtifactsError when it is not a pointer of this schema
    with every field of its form."""
    try:
        fields = json.loads(body)
    except (UnicodeDecodeError, ValueError) as error:
        raise ArtifactsError(f"snapshot pointer: not JSON ({error})") from None
    if not isinstance(fields, dict) or sorted(fields) != sorted(MEMBERS):
        raise ArtifactsError(f"snapshot pointer: not an object of the members {list(MEMBERS)}")
    if not is_count(fields["schema"]) or fields["schema"] != SCHEMA:
        raise ArtifactsError(f"snapshot pointer: schema {shown(fields['schema'])} is not {SCHEMA}")
    build = fields["build"]
    if not isinstance(build, dict) or sorted(build) != sorted(BUILD_MEMBERS):
        raise ArtifactsError(f"snapshot pointer: build is not an object of {list(BUILD_MEMBERS)}")
    return checked(
        Pointer(
            project=fields["project"],
            key=fields["key"],
            variant=fields["variant"],
            git_ref=fields["git_ref"],
            commit=fields["commit"],
            build=Build(id=build["id"], number=build["number"], url=build["url"]),
            written_at=fields["written_at"],
        )
    )


def find(store, prefix: str, project: str, key: str, variant: str) -> Pointer | None:
    """The pointer of a variant of a snapshot, or None when no build holds it yet.

    A pointer that does not read, or that names another snapshot than its key
    does, is an ArtifactsError that names its key. A read the store refuses
    raises as the store refused it.
    """
    where = layout.snapshot_pointer(prefix, project, key, variant)
    stored = store.get(where)
    if stored is None:
        return None
    try:
        found = decode(stored.body)
    except ArtifactsError as error:
        raise ArtifactsError(f"s3://{store.bucket}/{where}: {error}") from None
    if (found.project, found.key, found.variant) != (project, key, variant):
        raise ArtifactsError(
            f"s3://{store.bucket}/{where} names {found.variant} of {found.project} at {found.key}"
        )
    return found


def create(store, prefix: str, p: Pointer) -> Pointer:
    """Create the pointer p unless a build holds it already; the pointer that holds
    afterwards: p, or the one another build wrote first, which stays as it is.

    That another build was first is no error: both builds made the variant from
    the same commits.
    """
    where = layout.snapshot_pointer(prefix, p.project, p.key, p.variant)
    if store.create(where, encode(checked(p)), "application/json"):
        return p
    held = find(store, prefix, p.project, p.key, p.variant)
    if held is None:
        raise ArtifactsError(f"s3://{store.bucket}/{where} exists for a create and not for a read")
    return held
