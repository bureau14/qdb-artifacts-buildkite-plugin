#!/usr/bin/env python3
from __future__ import annotations

"""Artifact upload/download for Buildkite CI pipelines (AWS S3 + Cloudflare R2).

Replaces Buildkite's native artifact handling with backend choice (S3/R2), streaming
in-memory extraction, entry-level filtering, and two-level parallel transfers.

Parallelism
-----------
  --parallel (default 4)     files transferred simultaneously (ThreadPoolExecutor)
  --concurrency (default 32) multipart threads per file (boto3 TransferConfig)

Each worker creates its own boto3 client (not thread-safe to share). Combined
defaults yield up to 128 concurrent TCP streams.

Usage
-----
    # upload (build step)
    python3 .buildkite/tools/artifacts.py upload 'artifacts/**/*.tar.zst'

    # download + extract (test step, cross-step, entry-filtered)
    python3 .buildkite/tools/artifacts.py download \\
        --variant build-linux-amd64-release --extract --clean --output-dir artifacts \\
        '*-c-api.tar.zst!lib/*' '*-server.tar.zst!bin/*' '*-tests.tar.zst!bin/*'
"""

import os
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from boto3.s3.transfer import TransferConfig

from .clients import _s3_client, aws_clients, resolve_object_auth
from .config import load_store_config
from .errors import die
from .extract import extract_local_archive
from .layout import key_join, scope
from .log import fmt_size, log
from .resolve import resolve_artifacts
from .retry import _with_retry
from .upload import upload


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

    With --clean, download() wipes output_dir first — needed because Buildkite retries reuse the same
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
                    _s3_client(cfg, auth).download_file(bucket, key, tmp_path, Config=tc)
                    extract_local_archive(tmp_path, rel, out, entry_rules=entry_rules)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                dest = out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                _s3_client(cfg, auth).download_file(bucket, key, str(dest), Config=tc)

        _with_retry(_do, rel)
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


def _get_env_bool(val, default=False):
    if val is None:
        return default
    return str(val).strip().lower() in ("true", "1", "on", "yes")


def _get_env_array(prefix, key):
    """Read a Buildkite plugin array env value.

    Buildkite exposes multi-item arrays as KEY_0, KEY_1, ... and single-item
    arrays as KEY without the _0 suffix.
    """
    values = []
    j = 0
    while True:
        val = os.environ.get(f"{prefix}{key}_{j}")
        if not val:
            if j == 0:
                single_val = os.environ.get(f"{prefix}{key}")
                if single_val:
                    values.append(single_val)
            break
        values.append(val)
        j += 1
    return values


def parse_upload_config_from_env():
    """Parse the Buildkite plugin's upload schema from environment variables."""
    prefix = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_"
    return {
        "variant": os.environ.get(f"{prefix}VARIANT"),
        "git_ref": os.environ.get(f"{prefix}GIT_REF"),
        "project_id": os.environ.get(f"{prefix}PROJECT_ID"),
        "build_id": os.environ.get(f"{prefix}BUILD_ID"),
        "base_dir": os.environ.get(f"{prefix}BASE_DIR"),
        "parallel": os.environ.get(f"{prefix}PARALLEL"),
        "concurrency": os.environ.get(f"{prefix}CONCURRENCY"),
        "annotate": _get_env_bool(os.environ.get(f"{prefix}ANNOTATE"), default=True),
        "allow_empty": _get_env_bool(os.environ.get(f"{prefix}ALLOW_EMPTY")),
        "files": _get_env_array(prefix, "FILES"),
        "exclude": _get_env_array(prefix, "EXCLUDE"),
    }


def parse_download_projects_from_env():
    """Parse the Buildkite plugin's download.projects[] schema from environment variables."""
    projects = []
    i = 0
    while True:
        prefix = f"BUILDKITE_PLUGIN_QDB_ARTIFACTS_DOWNLOAD_PROJECTS_{i}_"
        variant = os.environ.get(f"{prefix}VARIANT")
        if not variant:
            break

        p = {
            "variant": variant,
            "git_ref": os.environ.get(f"{prefix}GIT_REF"),
            "require_strict_ref_match": _get_env_bool(
                os.environ.get(f"{prefix}REQUIRE_STRICT_REF_MATCH")
            ),
            "project_id": os.environ.get(f"{prefix}PROJECT_ID"),
            "build_id": os.environ.get(f"{prefix}BUILD_ID"),
            "output_dir": os.environ.get(f"{prefix}OUTPUT_DIR", "."),
            "extract": _get_env_bool(os.environ.get(f"{prefix}EXTRACT")),
            "files": _get_env_array(prefix, "FILES"),
            "exclude": _get_env_array(prefix, "EXCLUDE"),
        }

        projects.append(p)
        i += 1

    return projects


def parse_download_config_from_env():
    """Parse the Buildkite plugin's download schema from environment variables."""
    prefix = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_DOWNLOAD_"
    return {
        "clean": _get_env_bool(os.environ.get(f"{prefix}CLEAN")),
        "parallel": int(os.environ.get(f"{prefix}PARALLEL", "4")),
        "concurrency": int(os.environ.get(f"{prefix}CONCURRENCY", "32")),
        "projects": parse_download_projects_from_env(),
    }


def _current_buildkite_git_ref():
    """Return the current Buildkite git ref in artifact namespace format, if known."""
    tag = os.environ.get("BUILDKITE_TAG")
    if tag:
        return tag if tag.startswith("refs/") else f"refs/tags/{tag}"

    branch = os.environ.get("BUILDKITE_BRANCH")
    if branch:
        return branch if branch.startswith("refs/") else f"refs/heads/{branch}"

    return None


def resolve_default_download_build_id(project_config, pipeline_name):
    """Resolve the implicit download build_id for a project config.

    Same-pipeline downloads default to the current build only when the requested
    git_ref is the current Buildkite ref. If the same pipeline asks for another
    ref, use LATEST_SUCCESSFUL so branch/main fallbacks point at promoted builds
    instead of looking for the current build ID under a different ref.
    """
    build_id = project_config.get("build_id")
    if build_id:
        return build_id

    project_id = project_config.get("project_id")
    is_current_project = not project_id or project_id == pipeline_name
    if not is_current_project:
        return "LATEST_SUCCESSFUL"

    current_git_ref = _current_buildkite_git_ref()
    requested_git_ref = project_config.get("git_ref")
    if current_git_ref and requested_git_ref and requested_git_ref != current_git_ref:
        return "LATEST_SUCCESSFUL"

    build_id = os.environ.get("BUILDKITE_BUILD_ID")
    if not build_id:
        die("BUILDKITE_BUILD_ID is not set and no build_id override was given")
    return build_id


def main():
    args = sys.argv[1:]
    cmd = args.pop(0)
    cmd_project_id = None
    cmd_build_id = None
    variant = None
    git_ref = None

    if cmd == "upload":
        # The hook uses exit code 3 to distinguish a skipped upload from a completed
        # upload and preserve LATEST_SUCCESSFUL. Direct uploads still exit successfully.
        report_empty = args == ["--report-empty"]
        if args and not report_empty:
            die(f"unknown upload arguments: {args}")
        upload_config = parse_upload_config_from_env()

        pipeline_name = os.environ.get("BUILDKITE_PIPELINE_SLUG")
        upload_parallel = int(upload_config.get("parallel") or "4")
        upload_concurrency = int(upload_config.get("concurrency") or "32")

        if not upload_config.get("git_ref"):
            die(f"missing required git_ref in upload config: {upload_config}")
        if not upload_config.get("variant"):
            die(f"missing required variant in upload config: {upload_config}")
        if not upload_config.get("files"):
            upload_config["files"] = ["*.tar.zst"]

        project_id = upload_config.get("project_id") or pipeline_name
        if not project_id:
            die("missing required project_id and BUILDKITE_PIPELINE_SLUG is not set")
        upload_config["resolved_project_id"] = project_id

        build_id = upload_config.get("build_id") or os.environ.get("BUILDKITE_BUILD_ID")
        if not build_id:
            die("BUILDKITE_BUILD_ID is not set and no build_id override was given")
        upload_config["resolved_build_id"] = build_id

        uploaded = upload(
            upload_config["resolved_project_id"],
            upload_config["resolved_build_id"],
            upload_config["git_ref"],
            upload_config["variant"],
            upload_config["files"],
            upload_parallel,
            upload_concurrency,
            upload_config["base_dir"],
            upload_config["annotate"],
            upload_config["exclude"],
            allow_empty=upload_config["allow_empty"],
        )
        if report_empty and not uploaded:
            sys.exit(3)

    elif cmd == "promote":
        # this is a separate variant because sometimes we want to
        i = 0
        while i < len(args):
            if args[i] == "--variant":
                variant = args[i + 1]
                i += 2
            elif args[i] == "--project-id":
                cmd_project_id = args[i + 1]
                i += 2
            elif args[i] == "--build-id":
                cmd_build_id = args[i + 1]
                i += 2
            elif args[i] == "--git-ref":
                git_ref = args[i + 1]
                i += 2
            else:
                die(f"unknown argument: {args[i]}")

        if not variant:
            die("missing required --variant argument")
        if not git_ref:
            die("missing required --git-ref argument")

        project_id = cmd_project_id or os.environ.get("BUILDKITE_PIPELINE_SLUG")
        if not project_id:
            die("missing required --project-id argument and BUILDKITE_PIPELINE_SLUG is not set")

        build_id = cmd_build_id or os.environ.get("BUILDKITE_BUILD_ID")
        if not build_id:
            die("BUILDKITE_BUILD_ID is not set and no --build-id override was given")

        promote(project_id, build_id, git_ref, variant)

    elif cmd == "download":
        download_config = parse_download_config_from_env()
        projects_config = download_config["projects"]
        if not projects_config:
            die("no download projects configured")

        pipeline_name = os.environ.get("BUILDKITE_PIPELINE_SLUG")
        dirs_to_clean = set()

        for p in projects_config:
            if not p.get("git_ref"):
                die(f"missing required git_ref in download project config: {p}")
            if not p.get("variant"):
                die(f"missing required variant in download project config: {p}")
            if not p.get("files"):
                p["files"] = ["*.tar.zst"]

            project_id = p.get("project_id") or pipeline_name
            if not project_id:
                die("missing required project_id and BUILDKITE_PIPELINE_SLUG is not set")
            p["resolved_project_id"] = project_id

            p["resolved_build_id"] = resolve_default_download_build_id(p, pipeline_name)

            out_dir = Path(p["output_dir"]).resolve()
            if download_config["clean"]:
                dirs_to_clean.add(out_dir)

        download(
            projects_config,
            dirs_to_clean,
            download_config["parallel"],
            download_config["concurrency"],
        )

    else:
        die(f"unknown command: {cmd}")
