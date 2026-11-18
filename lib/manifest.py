"""manifest.json of a build: what an upload wrote, as compact JSON.

An upload writes it last, after every file of the build has arrived, so a build
whose manifest exists holds the whole upload. A download reads it for the
build's log lines only; a caller outside the plugin reads it through
read_manifest.

In a build with a snapshot key the manifest also says where its files were
made, so that whoever finds a build by its snapshot pointer can tell from what:

    source     {schema, repository, commit, pipeline, build_number, build_id,
                job_id, snapshot}
    downloads  [{project_id, variant, git_ref, build_id}], the builds the job
               downloaded by those of its own download options that name one
"""

import json
from dataclasses import asdict, dataclass

from botocore.exceptions import ClientError

from . import layout
from .clients import s3_client
from .objects import Store

SOURCE_SCHEMA = 1


@dataclass(frozen=True)
class Source:
    repository: str
    commit: str  # git rev-parse HEAD of the job's checkout
    pipeline: str
    build_number: int
    build_id: str  # the build the files are uploaded under
    job_id: str
    snapshot: str  # the key of the snapshot the build makes


@dataclass(frozen=True)
class Download:
    project_id: str
    variant: str
    git_ref: str
    build_id: str


@dataclass(frozen=True)
class Provenance:
    source: Source
    downloads: tuple


def compose_manifest(files, total_size_bytes, base_dir, uploaded_at, provenance=None):
    """The manifest of an upload of files (paths relative to base_dir); with
    provenance, after the five fields every upload writes, where they were made."""
    manifest = {
        "file_count": len(files),
        "files": files,
        "uploaded_at": uploaded_at,
        "total_size_bytes": total_size_bytes,
        "base_dir": base_dir,
    }
    if provenance is None:
        return manifest
    return {
        **manifest,
        "source": {"schema": SOURCE_SCHEMA, **asdict(provenance.source)},
        "downloads": [asdict(d) for d in provenance.downloads],
    }


def upload_manifest(store, prefix, manifest_payload):
    store.put(
        layout.manifest(prefix),
        json.dumps(manifest_payload, separators=(",", ":")).encode("utf-8"),
        "application/json",
    )


def read_manifest_at(store, prefix):
    """Best-effort fetch of <prefix>/manifest.json. Returns dict or None on any
    error -- this is purely informational and must never break the download."""
    try:
        stored = store.get(layout.manifest(prefix))
        return None if stored is None else json.loads(stored.body)
    except (ClientError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def read_manifest(cfg, auth, bucket, resolved):
    """The manifest.json of a resolved build as a dict, or None when there is none
    that reads as a JSON object: missing, refused by the store, not JSON, or not
    an object.

    It reads the manifest the way a download does. A caller must expect None: an
    upload that failed before its end leaves its files without a manifest.
    """
    manifest = read_manifest_at(Store(s3_client(cfg, auth), bucket), resolved.prefix)
    return manifest if isinstance(manifest, dict) else None
