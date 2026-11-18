"""Upload: select files, transfer them, write the manifest, annotate the job.

The files travel through `transfer`; the manifest is written after the last of
them has arrived, and the annotation after the manifest.
"""

import datetime
import glob
from pathlib import Path

from . import layout, transfer
from .annotation import create_buildkite_annotation
from .clients import aws_clients, resolve_object_auth
from .config import load_store_config
from .errors import ArtifactsError
from .log import fmt_size, log
from .manifest import compose_manifest, upload_manifest
from .objects import destination_store
from .rules import is_excluded


def upload(
    project_id,
    build_id,
    git_ref,
    variant,
    patterns,
    parallel=4,
    concurrency=32,
    base_dir=None,
    annotate=True,
    exclude_patterns=None,
    allow_empty=False,
    provenance=None,
):
    """Glob files and upload to the current build/variant prefix. CWD-relative paths
    become the S3 key suffix, preserving directory structure.
    Uploads a manifest.json file containing metadata about the uploaded files once all uploads are complete.
    With provenance, the manifest also says where the files were made.

    Creds are minted once before the pool -- for R2, ensure TTL covers the full upload.
    Returns True after uploading, or False when allow_empty skips an empty upload.
    """
    cwd = Path.cwd().resolve()

    base_path = cwd
    if base_dir:
        base_path = Path(base_dir).resolve()
        if not base_path.is_dir():
            raise ArtifactsError(f"--base-dir {base_dir!r} does not exist or is not a directory")

    matched_files = sorted(
        {
            Path(f).resolve()
            for p in patterns
            for f in glob.glob(p, recursive=True)
            if Path(f).is_file()
        }
    )
    if not matched_files:
        if allow_empty:
            log(f"No files matched: {patterns}; skipping artifact upload (allow_empty=true)")
            return False
        raise ArtifactsError(f"no files matched: {patterns}")

    if base_dir:
        for f in matched_files:
            try:
                f.relative_to(base_path)
            except ValueError:
                raise ArtifactsError(f"file {f} is not under base_dir {base_dir}") from None

    files = [
        f
        for f in matched_files
        if not is_excluded(f.relative_to(base_path).as_posix(), exclude_patterns)
    ]
    if not files:
        if allow_empty:
            log(
                "No files left after applying exclude patterns: "
                f"{exclude_patterns or []}; skipping artifact upload (allow_empty=true)"
            )
            return False
        raise ArtifactsError(
            f"no files left after applying exclude patterns: {exclude_patterns or []}"
        )

    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    store, dest_prefix = destination_store(cfg, auth)
    bucket = store.bucket
    pfx = layout.build_prefix(dest_prefix, project_id, git_ref, variant, build_id)
    tc = transfer.transfer_config(concurrency)

    total_bytes = sum(f.stat().st_size for f in files)
    log(
        f"Uploading {len(files)} artifact(s) ({fmt_size(total_bytes)}) to "
        f"s3://{bucket}/{pfx}/ via backend={cfg.backend}"
    )
    transfer.log_settings(parallel, concurrency)
    if exclude_patterns:
        log(f"  exclude={exclude_patterns}")

    def upload_one(f):
        rel = f.relative_to(base_path).as_posix()
        log(f"  {rel} ({fmt_size(f.stat().st_size)})")

        def attempt(client):
            client.upload_file(
                str(f),
                bucket,
                layout.key_join(pfx, rel),
                ExtraArgs={"ContentDisposition": f'attachment; filename="{f.name}"'},
                Config=tc,
            )

        transfer.one_file(cfg, auth, rel, attempt)

    relative_files_with_sizes = [
        (f.relative_to(base_path).as_posix(), f.stat().st_size) for f in files
    ]
    relative_files_path = [rel for rel, _ in relative_files_with_sizes]

    def write_manifest():
        manifest_payload = compose_manifest(
            relative_files_path,
            total_bytes,
            base_dir,
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            provenance,
        )
        upload_manifest(store, pfx, manifest_payload)

    elapsed = transfer.run(upload_one, files, parallel, then=write_manifest)

    if annotate:
        create_buildkite_annotation(cfg.artifacts_domain, pfx, relative_files_with_sizes)
    else:
        log("  annotate=false; skipping Buildkite annotation")
    transfer.log_done("Uploaded", len(files), total_bytes, elapsed)
    return True
