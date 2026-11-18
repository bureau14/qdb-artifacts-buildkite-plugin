"""Download: resolve the build, transfer the selected artifacts, extract them.

Transfers run as uploads do: `parallel` files at a time, each with
`concurrency` multipart threads and a boto3 client of its own.
"""

import os
import shutil
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from boto3.s3.transfer import TransferConfig

from .clients import aws_clients, resolve_object_auth, s3_client
from .config import load_store_config
from .extract import extract_local_archive
from .log import fmt_size, log
from .resolve import resolve_artifacts
from .retry import with_retry


def download_artifacts(
    cfg,
    auth,
    project_id,
    build_id,
    git_ref,
    variant,
    patterns,
    exclude_patterns=None,
    output_dir=".",
    extract=False,
    require_strict_ref_match=False,
    parallel=4,
    concurrency=32,
):
    """List matching artifacts under the variant prefix and download/extract in parallel.
    Uses explicit cfg and auth plus project_id to locate the artifacts namespace;
    no Buildkite environment or SSM configuration is required. The plugin wrapper
    defaults an omitted project_id to the current BUILDKITE_PIPELINE_SLUG.
    Resolves build_id (can be LATEST_SUCCESSFUL) via resolve_artifacts and applies fallback
    logic to find artifacts from main/master branch if missing on the current branch unless
    require_strict_ref_match is set. If build_id is omitted, the plugin defaults it to
    BUILDKITE_BUILD_ID if downloading from the current pipeline and ref, otherwise LATEST_SUCCESSFUL.

    With --clean, download() wipes output_dir first -- needed because Buildkite retries reuse the same
    workspace, and stale artifacts from a failed attempt would corrupt test runs.
    With --extract, archives are downloaded to a temp file via boto3's transfer manager
    (parallel ranged GETs), then extracted locally, then the temp file is removed.

    Returns (bucket, ResolvedBuild, objects) after all transfers complete, so callers
    can record the selected build and artifacts without resolving them again.
    """
    bucket, resolved, objects = resolve_artifacts(
        cfg=cfg,
        auth=auth,
        project_id=project_id,
        build_id=build_id,
        git_ref=git_ref,
        variant=variant,
        patterns=patterns,
        exclude_patterns=exclude_patterns,
        require_strict_ref_match=require_strict_ref_match,
    )
    pfx = resolved.prefix

    tc = TransferConfig(max_concurrency=concurrency)
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    total_bytes = sum(obj.size for obj in objects)
    log(
        f"Downloading {len(objects)} artifact(s) ({fmt_size(total_bytes)}) from "
        f"s3://{bucket}/{pfx}/ via backend={cfg.backend}"
    )
    log(f"  parallel={parallel}, concurrency={concurrency}")

    def _download_one(key, rel, sz, entry_rules):
        log(f"  start: {rel} ({fmt_size(sz)})")
        ft0 = time.monotonic()

        def _do():
            if extract:
                with tempfile.NamedTemporaryFile(dir=out, suffix=".tmp", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    s3_client(cfg, auth).download_file(bucket, key, tmp_path, Config=tc)
                    extract_local_archive(tmp_path, rel, out, entry_rules=entry_rules)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                dest = out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                s3_client(cfg, auth).download_file(bucket, key, str(dest), Config=tc)

        with_retry(_do, rel)
        elapsed = time.monotonic() - ft0
        speed = sz / elapsed if elapsed > 0 else 0
        # Loud destination line. rel is repeated so parallel-interleaved logs are still parseable.
        if extract:
            extras = []
            for entry_filter, strip_prefix in entry_rules:
                if entry_filter:
                    extras.append(f"filter={entry_filter}")
                if strip_prefix:
                    extras.append(f"strip={strip_prefix}")
            extra_str = f"  ({', '.join(extras)})" if extras else ""
            log(
                f"  done : {rel} -> extracted into {out}/{extra_str}  "
                f"[{elapsed:.1f}s, {fmt_size(speed)}/s]"
            )
        else:
            log(f"  done : {rel} -> {out / rel}  [{elapsed:.1f}s, {fmt_size(speed)}/s]")

    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=parallel)
    futs = {
        pool.submit(_download_one, obj.key, obj.rel, obj.size, obj.entry_rules): obj.rel
        for obj in sorted(objects, key=lambda obj: obj.key)
    }
    try:
        for fut in as_completed(futs):
            fut.result()
    except KeyboardInterrupt:
        os._exit(130)
    pool.shutdown()

    elapsed = time.monotonic() - t0
    avg_speed = total_bytes / elapsed if elapsed > 0 else 0
    log(
        f"Downloaded {len(objects)} file(s), {fmt_size(total_bytes)} in {elapsed:.1f}s "
        f"({fmt_size(avg_speed)}/s)"
    )
    return bucket, resolved, objects


def download(projects_config, dirs_to_clean, parallel=4, concurrency=32):
    for d in dirs_to_clean:
        if d.exists():
            log(f"Cleaning {d}")
            shutil.rmtree(d)

    for p in projects_config:
        _, ssm = aws_clients()
        cfg = load_store_config(ssm)
        auth = resolve_object_auth(ssm, cfg, permission="object-read-only")
        download_artifacts(
            cfg=cfg,
            auth=auth,
            project_id=p["resolved_project_id"],
            build_id=p["resolved_build_id"],
            git_ref=p["git_ref"],
            patterns=p["files"],
            variant=p["variant"],
            exclude_patterns=p.get("exclude", []),
            output_dir=p["output_dir"],
            extract=p["extract"],
            require_strict_ref_match=p["require_strict_ref_match"],
            parallel=parallel,
            concurrency=concurrency,
        )
