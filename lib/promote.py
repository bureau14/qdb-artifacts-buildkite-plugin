"""Promote: point LATEST_SUCCESSFUL of a variant at a build.

A download that asks for LATEST_SUCCESSFUL gets the build promoted last. The
plugin promotes only after the step's command passed, so the pointer names only
builds whose command succeeded.
"""

from .clients import _s3_client, aws_clients, resolve_object_auth
from .config import load_store_config
from .layout import key_join, scope
from .log import log


def promote(project_id, build_id, git_ref, variant):
    """Update the LATEST_SUCCESSFUL pointer file for the current ref to point to the current build ID.
    This allows test steps to use --build-id LATEST_SUCCESSFUL to always get the latest green build's artifacts."""
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    bucket, pfx = scope(cfg, project_id, build_id, variant, git_ref, skip_build_id=True)

    target_key = key_join(pfx, "LATEST_SUCCESSFUL")
    _s3_client(cfg, auth).put_object(
        Bucket=bucket,
        Key=target_key,
        Body=build_id.encode("utf-8"),
        ContentType="text/plain",
    )
    log(f"Set {target_key} → {build_id} for ref {git_ref}")
