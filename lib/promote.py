"""Promote: point LATEST_SUCCESSFUL of a variant at a build, and in a build with a
snapshot key create the snapshot pointer of the variant.

A download that asks for LATEST_SUCCESSFUL gets the build promoted last. The
plugin promotes only after the step's command passed, so the pointer names only
builds whose command succeeded. The snapshot pointer is created after
LATEST_SUCCESSFUL moved, with the same store and credentials, and only once:
one act of the step that promotes serves both kinds of dependency.
"""

from . import layout, pointer
from .clients import aws_clients, resolve_object_auth
from .config import load_store_config
from .log import log
from .objects import destination_store


def log_holder(wanted, held, where):
    """Say which build the pointer of the variant names after this step created it,
    or found it."""
    if held == wanted:
        log(f"Created {where}: build {held.build.number} holds {held.variant} of {held.key}")
    elif held.build.id == wanted.build.id:
        log(f"{where} names this build since {held.written_at}; left as it is")
    else:
        log(
            f"{where} names build {held.build.number} ({held.build.url}), which promoted "
            f"{held.variant} of {held.key} first; left as it is"
        )


def promote(project_id, build_id, git_ref, variant, snapshot_pointer=None):
    """Update the LATEST_SUCCESSFUL pointer file for the current ref to point to the current build ID.
    This allows test steps to use --build-id LATEST_SUCCESSFUL to always get the latest green build's artifacts.
    With snapshot_pointer, then create that pointer unless a build holds it already."""
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    store, prefix = destination_store(cfg, auth)

    target_key = layout.latest_pointer(prefix, project_id, git_ref, variant)
    store.put(target_key, build_id.encode("utf-8"), "text/plain")
    log(f"Set {target_key} \N{RIGHTWARDS ARROW} {build_id} for ref {git_ref}")

    if snapshot_pointer is not None:
        held = pointer.create(store, prefix, snapshot_pointer)
        where = layout.snapshot_pointer(prefix, held.project, held.key, held.variant)
        log_holder(snapshot_pointer, held, where)
