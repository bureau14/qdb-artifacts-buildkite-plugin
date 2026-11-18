"""Upload: select files, transfer them, write the manifest, annotate the job.

Files travel `parallel` at a time (default 4), each with `concurrency` multipart
threads (default 32), so the defaults allow up to 128 streams at once. Every
file gets its own boto3 client, because a client is not safe to share between
threads.
"""

import datetime
import glob
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from boto3.s3.transfer import TransferConfig

from .annotation import _create_buildkite_annotation
from .clients import _s3_client, aws_clients, resolve_object_auth
from .config import load_store_config
from .errors import ArtifactsError
from .layout import key_join, scope
from .log import fmt_size, log
from .manifest import _upload_manifest
from .retry import _with_retry
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
):
    """Glob files and upload to the current build/variant prefix. CWD-relative paths
    become the S3 key suffix, preserving directory structure.
    Uploads a manifest.json file containing metadata about the uploaded files once all uploads are complete.

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
    bucket, pfx = scope(cfg, project_id, build_id, variant, git_ref)
    tc = TransferConfig(max_concurrency=concurrency)

    total_bytes = sum(f.stat().st_size for f in files)
    log(
        f"Uploading {len(files)} artifact(s) ({fmt_size(total_bytes)}) to "
        f"s3://{bucket}/{pfx}/ via backend={cfg.backend}"
    )
    log(f"  parallel={parallel}, concurrency={concurrency}")
    if exclude_patterns:
        log(f"  exclude={exclude_patterns}")

    def _upload_one(f):
        rel = f.relative_to(base_path).as_posix()
        log(f"  {rel} ({fmt_size(f.stat().st_size)})")

        def _do():
            _s3_client(cfg, auth).upload_file(
                str(f),
                bucket,
                key_join(pfx, rel),
                ExtraArgs={"ContentDisposition": f'attachment; filename="{f.name}"'},
                Config=tc,
            )

        _with_retry(_do, rel)

    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=parallel)
    futs = {pool.submit(_upload_one, f): f for f in files}
    relative_files_with_sizes = [
        (f.relative_to(base_path).as_posix(), f.stat().st_size) for f in files
    ]
    relative_files_path = [rel for rel, _ in relative_files_with_sizes]
    try:
        for fut in as_completed(futs):
            fut.result()

        # After uploads are done, write a manifest file with metadata about the uploaded artifacts.
        # might come handy for debugging / validation / future features
        manifest_payload = {
            "file_count": len(files),
            "files": relative_files_path,
            "uploaded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "total_size_bytes": total_bytes,
            "base_dir": base_dir,
        }
        _upload_manifest(bucket, pfx, manifest_payload, cfg, auth)
    except KeyboardInterrupt:
        # Hard exit: ThreadPoolExecutor.shutdown(wait=True) would block for minutes
        # waiting for in-flight transfers. 130 = POSIX SIGINT convention.
        os._exit(130)
    pool.shutdown()

    elapsed = time.monotonic() - t0
    avg_speed = total_bytes / elapsed if elapsed > 0 else 0
    if annotate:
        _create_buildkite_annotation(cfg.artifacts_domain, pfx, relative_files_with_sizes)
    else:
        log("  annotate=false; skipping Buildkite annotation")
    log(
        f"Uploaded {len(files)} file(s), {fmt_size(total_bytes)} in {elapsed:.1f}s "
        f"({fmt_size(avg_speed)}/s)"
    )
    return True
