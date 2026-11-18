"""Which build a download gets, and which of its objects.

A build id of LATEST_SUCCESSFUL is read from the pointer that promote moves. A
ref without artifacts falls back to refs/heads/master, then refs/heads/main, so
that a branch that has not built a variant yet gets the default branch's;
a strict ref match turns that off. The selection is kept apart from any
transfer, so that a caller outside the plugin resolves exactly as a download
does and fetches the objects its own way.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from . import layout
from .clients import s3_client
from .errors import ArtifactsError
from .log import fmt_size, log
from .manifest import read_manifest_at
from .objects import Store
from .rules import is_excluded, match_rules, parse_rules


@dataclass(frozen=True)
class ResolvedBuild:
    """Resolved artifact location after applying LATEST_SUCCESSFUL and
    default-branch fallback. Carries enough context for downstream logging
    (whether a fallback was applied, when the build was created)."""

    requested_ref: str
    requested_build_id: str
    ref: str
    build_id: str
    prefix: str
    fallback_applied: bool
    pointer_lastmod: datetime.datetime | None  # mtime of LATEST_SUCCESSFUL pointer, if used


@dataclass(frozen=True)
class ResolvedArtifact:
    """Named selection fields for callers; entry-rule order resolves overlapping filters."""

    key: str
    rel: str
    size: int
    entry_rules: list[tuple[str | None, str | None]]


def _fmt_age(t):
    """Format a UTC datetime as 'Nd Nh ago' / 'Nh Nm ago' / 'Nm ago' for log
    readability. Naive datetimes are assumed UTC."""
    if t is None:
        return "unknown"
    if t.tzinfo is None:
        t = t.replace(tzinfo=datetime.timezone.utc)
    delta = datetime.datetime.now(datetime.timezone.utc) - t
    s = int(delta.total_seconds())
    if s < 0:
        return "in the future"
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        h, m = divmod(s, 3600)
        m //= 60
        return f"{h}h {m}m ago" if m else f"{h}h ago"
    d, rem = divmod(s, 86400)
    h = rem // 3600
    return f"{d}d {h}h ago" if h else f"{d}d ago"


def _read_latest_successful(store, key):
    """Read the LATEST_SUCCESSFUL pointer. Returns (build_id, pointer_lastmod) or
    (None, None) if absent. The pointer mtime is useful as a build-age proxy
    when the build itself has no manifest.json."""
    stored = store.get(key)
    if stored is None:
        return None, None
    return stored.body.decode("utf-8").strip(), stored.last_modified


def _holds_objects(store, prefix):
    """Check if any objects exist under the given prefix."""
    return bool(store.list(f"{prefix}/", limit=1))


def _validate_artifact_path(
    store,
    dest_prefix,
    project_id,
    ref,
    variant,
    build_id,
    require_strict_ref_match=False,
):
    """Resolve the artifact prefix, logging every attempt as it goes.

    By default walks [requested_ref, refs/heads/master, refs/heads/main] in order,
    resolving LATEST_SUCCESSFUL pointers when needed and skipping refs whose prefix
    is empty. require_strict_ref_match limits resolution to the requested ref.
    Returns a ResolvedBuild describing the winning attempt. On failure, raises
    ArtifactsError with a structured trace listing every attempt that was tried and
    why each failed.
    """
    bucket = store.bucket
    master_refs = ("refs/heads/master", "refs/heads/main")

    refs_to_check = [ref]
    if not require_strict_ref_match and ref not in master_refs:
        refs_to_check += list(master_refs)

    # Each entry: dict(ref, resolved_build_id_or_None, prefix_or_None, status, detail)
    attempts = []

    for current_ref in refs_to_check:
        log(f"resolve attempt: ref={current_ref} build={build_id}")
        pointer_lastmod = None
        effective_build_id = build_id

        if build_id == "LATEST_SUCCESSFUL":
            pointer_key = layout.latest_pointer(dest_prefix, project_id, current_ref, variant)
            effective_build_id, pointer_lastmod = _read_latest_successful(store, pointer_key)
            if not effective_build_id:
                log(f"  no LATEST_SUCCESSFUL pointer at s3://{bucket}/{pointer_key}, skipping")
                attempts.append(
                    {
                        "ref": current_ref,
                        "build_id": None,
                        "prefix": None,
                        "status": "no-pointer",
                        "detail": f"s3://{bucket}/{pointer_key}",
                    }
                )
                continue
            mtime_str = (
                pointer_lastmod.strftime("%Y-%m-%d %H:%M:%SZ") if pointer_lastmod else "unknown"
            )
            log(f"  LATEST_SUCCESSFUL -> {effective_build_id} (pointer mtime {mtime_str})")

        prefix = layout.build_prefix(
            dest_prefix, project_id, current_ref, variant, effective_build_id
        )

        if _holds_objects(store, prefix):
            log(f"  prefix s3://{bucket}/{prefix}/ has objects -> MATCH")
            return ResolvedBuild(
                requested_ref=ref,
                requested_build_id=build_id,
                ref=current_ref,
                build_id=effective_build_id,
                prefix=prefix,
                fallback_applied=(current_ref != ref),
                pointer_lastmod=pointer_lastmod,
            )

        log(f"  prefix s3://{bucket}/{prefix}/ has no objects, skipping")
        attempts.append(
            {
                "ref": current_ref,
                "build_id": effective_build_id,
                "prefix": prefix,
                "status": "empty",
                "detail": f"s3://{bucket}/{prefix}/",
            }
        )

    lines = [
        (
            f"Artifact path could not be resolved for project={project_id}, "
            f"variant={variant}, requested_ref={ref}, requested_build_id={build_id}."
        ),
        f"Attempts ({len(attempts)}):",
    ]
    for a in attempts:
        if a["status"] == "no-pointer":
            lines.append(
                f"  - ref={a['ref']}: LATEST_SUCCESSFUL pointer not found at {a['detail']}"
            )
        elif a["status"] == "empty":
            lines.append(
                f"  - ref={a['ref']} build={a['build_id']}: "
                f"prefix {a['detail']} contained no objects"
            )
        else:
            lines.append(f"  - ref={a['ref']}: {a['status']} ({a['detail']})")
    raise ArtifactsError("\n".join(lines))


def resolve_artifacts(
    cfg,
    auth,
    project_id,
    build_id,
    git_ref,
    variant,
    patterns,
    exclude_patterns=None,
    require_strict_ref_match=False,
):
    """Resolve a build and select its artifacts without downloading or extracting.

    Accepts explicit StoreConfig, ObjectAuth, and project/build/ref/variant values;
    no Buildkite environment or local output directory is required. patterns uses
    the plugin's archive_glob[!entry_filter] syntax. LATEST_SUCCESSFUL resolution,
    branch fallback, exclusions, and ordered entry-filter aggregation are shared
    with the downloader. Returns (bucket, ResolvedBuild, objects), where objects
    is a list of ResolvedArtifact instances. Unresolved builds or empty
    matches keep the plugin's diagnostic logging and raise ArtifactsError.
    """
    # Keep selection independent of transfer/output handling so callers outside plugin
    # can reuse the exact same resolution policy with their own download mechanism.
    rules = parse_rules(patterns)
    bucket, dest_prefix = layout.parse_destination(cfg.destination)
    store = Store(s3_client(cfg, auth), bucket)  # pointers, manifest, listing; no transfers
    resolved = _validate_artifact_path(
        store,
        dest_prefix,
        project_id,
        git_ref,
        variant,
        build_id,
        require_strict_ref_match=require_strict_ref_match,
    )
    pfx = resolved.prefix

    if resolved.fallback_applied:
        log(
            f"WARNING: falling back from ref={resolved.requested_ref} to "
            f"ref={resolved.ref} (no artifacts on the requested ref)"
        )

    # Canonical resolved-build summary -- emitted once per project, before any transfer.
    manifest = read_manifest_at(store, pfx)
    log(f"resolved build for project={project_id} variant={variant}:")
    fallback_note = (
        f"  (fallback from {resolved.requested_ref})" if resolved.fallback_applied else ""
    )
    log(f"  ref       = {resolved.ref}{fallback_note}")
    bid_note = (
        " (via LATEST_SUCCESSFUL)" if resolved.requested_build_id == "LATEST_SUCCESSFUL" else ""
    )
    log(f"  build_id  = {resolved.build_id}{bid_note}")
    built_at = None
    if manifest and manifest.get("uploaded_at"):
        try:
            ua = manifest["uploaded_at"].rstrip("Z")
            built_at = datetime.datetime.fromisoformat(ua).replace(tzinfo=datetime.timezone.utc)
        except (ValueError, TypeError):
            built_at = None
    if built_at is None:
        built_at = resolved.pointer_lastmod
    if built_at is not None:
        log(f"  built at  = {built_at.strftime('%Y-%m-%d %H:%M:%SZ')} ({_fmt_age(built_at)})")
    log(f"  prefix    = s3://{bucket}/{pfx}/")
    if manifest:
        log(
            f"  contents  = {manifest.get('file_count', '?')} file(s), "
            f"{fmt_size(manifest.get('total_size_bytes', 0))}"
        )

    objects = []
    present_keys = []  # everything at the prefix except manifest.json, for diagnostics
    for key, size in store.list(f"{pfx}/"):
        rel = key[len(pfx) + 1 :]  # path relative to variant's artifact root
        if not rel or rel == layout.MANIFEST:
            continue
        present_keys.append(rel)
        entry_rules = match_rules(rel, rules)
        if entry_rules and not is_excluded(rel, exclude_patterns):
            objects.append(ResolvedArtifact(key=key, rel=rel, size=size, entry_rules=entry_rules))

    if not objects:
        rule_strs = [f"{ag}!{ef}" if ef else ag for ag, ef, _ in rules]
        lines = [
            "no artifacts matched the configured rules.",
            f"  rules    : {rule_strs}",
            f"  exclude  : {exclude_patterns or []}",
            f"  prefix   : s3://{bucket}/{pfx}/",
        ]
        if present_keys:
            sample = present_keys[:20]
            lines.append(f"  present  : {len(present_keys)} object(s) at this prefix, e.g.")
            for k in sample:
                lines.append(f"               {k}")
            if len(present_keys) > len(sample):
                lines.append(f"               ... and {len(present_keys) - len(sample)} more")
            lines.append("  hint: archive extension or variant name probably mismatched.")
        else:
            # _validate_artifact_path only returns a prefix where _holds_objects
            # was True, so reaching here means the only object was manifest.json.
            lines.append("  present  : (only manifest.json -- this build uploaded no artifacts)")
        raise ArtifactsError("\n".join(lines))

    return bucket, resolved, objects
