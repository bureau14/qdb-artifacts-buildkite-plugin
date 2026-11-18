"""Every object key the plugin reads or writes.

    <prefix>/<project>/<git ref>/variants/<variant>/
        LATEST_SUCCESSFUL                 text, a build id; moved by promote
        builds/<build id>/manifest.json   written by upload
        builds/<build id>/<file>          written by upload

    <prefix>/_snapshot/v1/
        pointers/<project>/<key>/<variant>.json    written once, see pointer.py
        failures/<project>/<key>/<build id>.json   written once by the plan step

The destination names the bucket and the prefix. Project, ref and build id keep
the runs of pipelines apart; the variant keeps apart what the steps of one
build produce, so that a test step downloads under the variant its build step
uploaded to. A file keeps its path relative to the checkout, or to `base_dir`.

The objects of snapshot dependencies sit beside the projects and never inside
a build prefix: a deployed plugin counts every object there as an artifact of
the build, and a step may download "*" of another pipeline's build. No pipeline
slug starts with "_", so no project shares their prefix.

Each kind of key has its own function, so the kind of a key follows from the
function a caller names and never from the value of a build id.
"""

from __future__ import annotations

from urllib.parse import urlparse

LATEST = "LATEST_SUCCESSFUL"
MANIFEST = "manifest.json"
SNAPSHOT_ROOT = "_snapshot/v1"


def parse_destination(destination: str) -> tuple[str, str]:
    """(bucket, prefix) of `s3://bucket/prefix`; a bare name is a bucket."""
    if not destination.startswith("s3://"):
        return destination.strip("/"), ""
    parsed = urlparse(destination)
    return parsed.netloc, parsed.path.strip("/")


def key_join(*parts: str) -> str:
    """Join key segments with one slash; an empty segment is left out."""
    return "/".join(p.strip("/") for p in parts if p.strip("/"))


def variant_prefix(prefix: str, project: str, git_ref: str, variant: str) -> str:
    return key_join(prefix, project, git_ref, "variants", variant)


def latest_pointer(prefix: str, project: str, git_ref: str, variant: str) -> str:
    return key_join(variant_prefix(prefix, project, git_ref, variant), LATEST)


def build_prefix(prefix: str, project: str, git_ref: str, variant: str, build_id: str) -> str:
    return key_join(variant_prefix(prefix, project, git_ref, variant), "builds", build_id)


def manifest(build: str) -> str:
    return key_join(build, MANIFEST)


def snapshot_pointer(prefix: str, project: str, key: str, variant: str) -> str:
    return key_join(prefix, SNAPSHOT_ROOT, "pointers", project, key, f"{variant}.json")


def failure_records(prefix: str, project: str, key: str) -> str:
    return key_join(prefix, SNAPSHOT_ROOT, "failures", project, key) + "/"


def failure_record(prefix: str, project: str, key: str, build_id: str) -> str:
    return failure_records(prefix, project, key) + f"{build_id}.json"
