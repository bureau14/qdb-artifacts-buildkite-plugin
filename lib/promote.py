"""Promote: point LATEST_SUCCESSFUL of a variant at a build.

A download that asks for LATEST_SUCCESSFUL gets the build promoted last. The
plugin promotes only after the step's command passed, so the pointer names only
builds whose command succeeded.
"""

from . import layout
from .clients import aws_clients, resolve_object_auth, s3_client
from .config import load_store_config
from .log import log
from .objects import Store


def promote(project_id, build_id, git_ref, variant):
    """Update the LATEST_SUCCESSFUL pointer file for the current ref to point to the current build ID.
    This allows test steps to use --build-id LATEST_SUCCESSFUL to always get the latest green build's artifacts."""
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    bucket, prefix = layout.parse_destination(cfg.destination)

    target_key = layout.latest_pointer(prefix, project_id, git_ref, variant)
    Store(s3_client(cfg, auth), bucket).put(target_key, build_id.encode("utf-8"), "text/plain")
    log(f"Set {target_key} \N{RIGHTWARDS ARROW} {build_id} for ref {git_ref}")
