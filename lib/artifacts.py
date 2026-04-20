#!/usr/bin/env python3
from __future__ import annotations

"""Artifact upload/download for Buildkite CI pipelines (AWS S3 + Cloudflare R2).

Replaces Buildkite's native artifact handling with backend choice (S3/R2), streaming
in-memory extraction, entry-level filtering, and two-level parallel transfers.

Key layout
----------
    s3://<bucket>/<build_id>/<step_key>/<dest_prefix>/<cwd_relative_path>

Build ID isolates pipeline runs; step key separates producers so test steps can
address a specific build step's output via --step. CWD-relative paths preserve
the original directory structure.

Cross-step sharing
------------------
Build steps upload under their own step key. Test steps download with
``--step build-linux-amd64-release`` to look in the build step's namespace
(otherwise they'd look in their own empty namespace). The --step value is
substituted at pipeline-generation time.

Backends
--------
Both use boto3's S3-compatible data path:
  S3 — IAM role on the CI agent; no explicit credentials needed.
  R2 — S3-compatible credentials stored in SSM (access key ID + secret derived from
       SHA256 of the Cloudflare API token). No Cloudflare SDK or temp credential
       minting needed — boto3 talks directly to the R2 S3 endpoint.

Config resolution
-----------------
    env var (ARTIFACTS_*) → SSM parameter → default

Env vars for per-job overrides, SSM for org-wide config, defaults for
plain S3/IAM setups.

Parallelism
-----------
  --parallel (default 4)     files transferred simultaneously (ThreadPoolExecutor)
  --concurrency (default 32) multipart threads per file (boto3 TransferConfig)

Each worker creates its own boto3 client (not thread-safe to share). Combined
defaults yield up to 128 concurrent TCP streams.

Extraction
----------
With --extract, archives are first downloaded to a temporary file on the same
filesystem as the output directory using boto3's transfer manager (parallel ranged
GETs), then extracted from the local file, then the temp file is removed.
  .tar.gz  — tarfile 'r:gz' mode (seekable)
  .tar.zst — zstandard.stream_reader → tarfile 'r|'
  .zip     — zipfile.ZipFile (seekable local file)

Entry filtering
---------------
Patterns like ``*-server.tar.zst!bin/*`` match archives by glob, extract only
entries matching ``bin/*``, and strip the ``bin/`` prefix from output paths.
Test steps use this to pull only executables/libraries from component archives.

Usage
-----
    # upload (build step)
    python3 .buildkite/tools/artifacts.py upload 'artifacts/**/*.tar.zst'

    # download + extract (test step, cross-step, entry-filtered)
    python3 .buildkite/tools/artifacts.py download \\
        --step build-linux-amd64-release --extract --clean --output-dir artifacts \\
        '*-c-api.tar.zst!lib/*' '*-server.tar.zst!bin/*' '*-tests.tar.zst!bin/*'
"""

import datetime
import fnmatch
import glob
import os
import shutil
import sys
import tarfile
import tempfile
import time
import zipfile
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import boto3
import zstandard
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Timeout / retry constants
# ---------------------------------------------------------------------------
READ_TIMEOUT = 300  # 5 minutes — fail fast instead of hanging 40+ minutes
CONNECT_TIMEOUT = 60  # 1 minute
MAX_RETRIES = 10
BACKOFF_BASE = 5  # seconds; doubles each retry
BACKOFF_CAP = 60  # 1 minute max sleep between retries

# ---------------------------------------------------------------------------
# SSM parameter paths
# ---------------------------------------------------------------------------
BACKEND_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/backend"
DESTINATION_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/destination"
ENDPOINT_URL_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/endpoint-url"
R2_ACCOUNT_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/account-id"
R2_ACCESS_KEY_ID_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/access-key-id"
R2_SECRET_ACCESS_KEY_SSM_PARAM = "/services/buildkite/credentials/artifacts/r2/secret-access-key"


@dataclass(frozen=True)  # frozen → safe to share across worker threads
class StoreConfig:
    """Resolved object-store backend config. Populated once by load_store_config()."""

    backend: str
    destination: str
    endpoint_url: str | None = None
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None


@dataclass(frozen=True)
class ObjectAuth:
    """Per-operation S3 credentials. All None for S3 (IAM role handles auth).
    For R2, populated from SSM-stored credentials derived from the Cloudflare API token.
    """

    access_key_id: str | None = None
    secret_access_key: str | None = None


def die(msg):
    print(f"[artifacts] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def log(msg):
    print(f"[artifacts] {msg}", file=sys.stderr)


def _with_retry(fn, description):
    """Execute fn() with exponential backoff on transient errors.

    Retries up to MAX_RETRIES times. Sleep between attempts starts at BACKOFF_BASE
    seconds and doubles each time, capped at BACKOFF_CAP.

    Backoff schedule (defaults): 5 → 10 → 20 → 40 → 60 → 60 → … seconds.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            if attempt == MAX_RETRIES:
                log(f"  FAILED after {MAX_RETRIES} attempts: {description}")
                raise
            sleep = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
            log(f"  attempt {attempt}/{MAX_RETRIES} failed for {description}: {exc}")
            log(f"  retrying in {sleep}s...")
            time.sleep(sleep)


def aws_clients():
    """Return (s3, ssm) clients for config resolution and listing only.
    Per-transfer clients are created separately in each worker thread."""
    s = boto3.session.Session()
    return (
        s.client("s3", config=Config(retries={"mode": "standard", "max_attempts": 10})),
        s.client(
            "ssm",
            config=Config(retries={"mode": "standard", "max_attempts": 5}),
        ),
    )


def _ssm_get_optional(ssm, name, with_decryption=True):
    """Return SSM parameter value or None if absent. Re-raises non-NotFound errors
    so IAM misconfigurations surface explicitly."""
    try:
        return ssm.get_parameter(Name=name, WithDecryption=with_decryption)["Parameter"]["Value"]
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code")
        if code == "ParameterNotFound":
            return None
        msg = exc.response.get("Error", {}).get("Message", str(exc))
        die(f"failed to read SSM parameter {name}: {code} — {msg}")


# Prefixes that indicate a placeholder / unconfigured value rather than a real one.
_PLACEHOLDER_PREFIXES = ("SET_ME", "REPLACE_", "TODO", "CHANGEME", "FIXME")


def _check_placeholder(value, label):
    """Die if value looks like a placeholder that was never replaced with a real one."""
    if value and any(value.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        die(
            f"{label} contains placeholder value {value!r} — "
            f"replace it with the real credential in Terraform tfvars or SSM"
        )


def _env_or_ssm(ssm, env_name, ssm_name, with_decryption=True):
    """Env var with SSM fallback. Env takes precedence for per-job overrides."""
    return os.environ.get(env_name) or _ssm_get_optional(
        ssm, ssm_name, with_decryption=with_decryption
    )


def load_store_config(ssm):
    """Resolve backend config: env vars → SSM → defaults.
    For R2, endpoint URL defaults to https://{account_id}.r2.cloudflarestorage.com."""
    backend = (
        (os.environ.get("ARTIFACTS_BACKEND") or _ssm_get_optional(ssm, BACKEND_SSM_PARAM) or "s3")
        .strip()
        .lower()
    )

    if backend not in {"s3", "r2"}:
        die(
            f"unsupported artifact backend: {backend!r} (expected 's3' or 'r2'). "
            f"Check env var ARTIFACTS_BACKEND or SSM parameter {BACKEND_SSM_PARAM}"
        )

    destination = os.environ.get("ARTIFACTS_DESTINATION") or _ssm_get_optional(
        ssm, DESTINATION_SSM_PARAM
    )
    if not destination:
        die(
            f"artifact destination not configured. "
            f"Set env var ARTIFACTS_DESTINATION or SSM parameter {DESTINATION_SSM_PARAM}"
        )

    # Applies to both backends; mainly for local MinIO / dev setups.
    endpoint_url = os.environ.get("ARTIFACTS_ENDPOINT_URL")

    if backend == "r2":
        endpoint_url = endpoint_url or _ssm_get_optional(ssm, ENDPOINT_URL_SSM_PARAM)

        r2_account_id = _env_or_ssm(ssm, "ARTIFACTS_R2_ACCOUNT_ID", R2_ACCOUNT_ID_SSM_PARAM)
        if not r2_account_id:
            die(
                f"R2 backend requires Cloudflare account ID. "
                f"Set env var ARTIFACTS_R2_ACCOUNT_ID or SSM parameter {R2_ACCOUNT_ID_SSM_PARAM}"
            )
        _check_placeholder(r2_account_id, f"R2 account ID (SSM {R2_ACCOUNT_ID_SSM_PARAM})")

        r2_access_key_id = _env_or_ssm(
            ssm,
            "ARTIFACTS_R2_ACCESS_KEY_ID",
            R2_ACCESS_KEY_ID_SSM_PARAM,
        )
        if not r2_access_key_id:
            die(
                f"R2 backend requires access key ID. "
                f"Set env var ARTIFACTS_R2_ACCESS_KEY_ID or SSM parameter {R2_ACCESS_KEY_ID_SSM_PARAM}"
            )
        _check_placeholder(r2_access_key_id, f"R2 access key ID (SSM {R2_ACCESS_KEY_ID_SSM_PARAM})")

        r2_secret_access_key = _env_or_ssm(
            ssm,
            "ARTIFACTS_R2_SECRET_ACCESS_KEY",
            R2_SECRET_ACCESS_KEY_SSM_PARAM,
            with_decryption=True,
        )
        if not r2_secret_access_key:
            die(
                f"R2 backend requires secret access key. "
                f"Set env var ARTIFACTS_R2_SECRET_ACCESS_KEY or SSM SecureString {R2_SECRET_ACCESS_KEY_SSM_PARAM}"
            )
        _check_placeholder(
            r2_secret_access_key,
            f"R2 secret access key (SSM {R2_SECRET_ACCESS_KEY_SSM_PARAM})",
        )

        if not endpoint_url:
            endpoint_url = f"https://{r2_account_id}.r2.cloudflarestorage.com"

        return StoreConfig(
            backend=backend,
            destination=destination,
            endpoint_url=endpoint_url,
            r2_account_id=r2_account_id,
            r2_access_key_id=r2_access_key_id,
            r2_secret_access_key=r2_secret_access_key,
        )

    return StoreConfig(
        backend=backend,
        destination=destination,
        endpoint_url=endpoint_url,
    )


def parse_s3(uri):
    """Parse s3://bucket/prefix URI into (bucket, prefix).
    Also handles plain bucket names (legacy SSM format)."""
    if not uri.startswith("s3://"):
        return uri.strip("/"), ""
    p = urlparse(uri)
    return p.netloc, p.path.strip("/")


def key_join(*parts):
    """Join S3 key segments, dropping blanks to avoid double slashes."""
    return "/".join(p.strip("/") for p in parts if p.strip("/"))


def _get_scope_context(build_id_override=None, step_override=None, ref_override=None):
    build_id = build_id_override or os.environ.get("BUILDKITE_BUILD_ID")
    if not build_id:
        die(
            "BUILDKITE_BUILD_ID is not set and no --build-id override was given. "
            "Are we running inside a Buildkite job?"
        )

    ref = ref_override
    if not ref_override:
        branch = ref_override or os.environ.get("BUILDKITE_BRANCH")
        tag = os.environ.get("BUILDKITE_TAG")
        if not branch and not tag:
            raise ValueError(
                "BUILDKITE_BRANCH and BUILDKITE_TAG are both empty — are we running inside a Buildkite job?"
            )

        ref = f"refs/tags/{tag}" if tag else f"refs/heads/{branch}"

    step = step_override or os.environ.get("BUILDKITE_STEP_KEY")
    if not step:
        die(
            "BUILDKITE_STEP_KEY is not set and no --step override was given. "
            "Ensure the pipeline step has a 'key' field, or pass --step <key> explicitly"
        )
    return build_id, step, ref


def scope(
    project_id,
    cfg,
    step_override=None,
    build_id_override=None,
    ref_override=None,
    skip_build_id=False,
):
    """Resolve (bucket, key_prefix) for the current build/step context.
    project_id scopes the artifacts to a specific project.
    step_override (--step) lets test steps address a build step's namespace
    instead of their own (which is empty). Resolved at pipeline-generation time.
    build_id_override and ref_override allow pointing to specific pipeline executions.
    skip_build_id skips the build ID in the path (useful for LATEST_SUCCESSFUL pointers)."""

    build_id, step, ref = _get_scope_context(build_id_override, step_override, ref_override)

    bucket, prefix = parse_s3(cfg.destination)

    if skip_build_id:
        return bucket, key_join(prefix, project_id, ref, "variants", step)

    if build_id == "LATEST_SUCCESSFUL":
        return bucket, key_join(prefix, project_id, ref, "variants", step, "LATEST_SUCCESSFUL")

    return bucket, key_join(prefix, project_id, ref, "variants", step, "builds", build_id)


def fmt_size(n):
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TiB"


def resolve_object_auth(ssm, cfg, permission):
    """S3 → empty auth (IAM role chain). R2 → use stored S3-compatible credentials."""
    if cfg.backend == "r2":
        return ObjectAuth(
            access_key_id=cfg.r2_access_key_id,
            secret_access_key=cfg.r2_secret_access_key,
        )
    return ObjectAuth()


def _s3_client(cfg, auth):
    """Create a per-thread S3 client. Called inside each worker because boto3 clients
    are not thread-safe (shared connection pool + credential state would race).

    R2 needs signature_version=s3v4 and addressing_style=path (R2 endpoints are
    account-level, not bucket-level — virtual-host style would produce invalid hostnames).
    """
    client_cfg = Config(
        read_timeout=READ_TIMEOUT,
        connect_timeout=CONNECT_TIMEOUT,
        retries={"mode": "standard", "max_attempts": 10},
    )
    if cfg.backend == "r2":
        client_cfg = Config(
            read_timeout=READ_TIMEOUT,
            connect_timeout=CONNECT_TIMEOUT,
            retries={"mode": "standard", "max_attempts": 10},
            signature_version="s3v4",
            s3={"addressing_style": "path"},
        )

    kwargs = {"config": client_cfg}
    if cfg.backend == "r2":
        kwargs["region_name"] = "auto"
    if cfg.endpoint_url:
        kwargs["endpoint_url"] = cfg.endpoint_url
    if auth.access_key_id:
        kwargs["aws_access_key_id"] = auth.access_key_id
        kwargs["aws_secret_access_key"] = auth.secret_access_key

    return boto3.client("s3", **kwargs)


def _upload_manifest(bucket, prefix, manifest_payload, cfg, auth):
    manifest_key = key_join(prefix, "manifest.json")
    _s3_client(cfg, auth).put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest_payload, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
    )


def upload(project_id, pattern, parallel=4, concurrency=32):
    """Glob files and upload to the current build/step prefix. CWD-relative paths
    become the S3 key suffix, preserving directory structure.
    Also uploads a manifest.json file containing metadata about the uploaded files.

    Creds are minted once before the pool — for R2, ensure TTL covers the full upload.
    """
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")

    bucket, pfx = scope(project_id, cfg)

    tc = TransferConfig(max_concurrency=concurrency)
    cwd = Path.cwd().resolve()
    files = sorted(
        {Path(f).resolve() for f in glob.glob(pattern, recursive=True) if Path(f).is_file()}
    )
    if not files:
        die(f"no files matched: {pattern}")

    total_bytes = sum(f.stat().st_size for f in files)
    log(
        f"Uploading {len(files)} artifact(s) ({fmt_size(total_bytes)}) to "
        f"s3://{bucket}/{pfx}/ via backend={cfg.backend}"
    )
    log(f"  parallel={parallel}, concurrency={concurrency}")

    def _upload_one(f):
        rel = f.relative_to(cwd).as_posix()
        log(f"  {rel} ({fmt_size(f.stat().st_size)})")

        def _do():
            _s3_client(cfg, auth).upload_file(str(f), bucket, key_join(pfx, rel), Config=tc)

        _with_retry(_do, rel)

    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=parallel)
    futs = {pool.submit(_upload_one, f): f for f in files}
    try:
        for fut in as_completed(futs):
            fut.result()

        # After uploads are done, write a manifest file with metadata about the uploaded artifacts.
        # might come handy for debugging / validation / future features
        manifest_payload = {
            "file_count": len(files),
            "files": [f.relative_to(cwd).as_posix() for f in files],
            "uploaded_at": datetime.datetime.now().isoformat() + "Z",
            "total_size_bytes": total_bytes,
        }
        _upload_manifest(bucket, pfx, manifest_payload, cfg, auth)
    except KeyboardInterrupt:
        # Hard exit: ThreadPoolExecutor.shutdown(wait=True) would block for minutes
        # waiting for in-flight transfers. 130 = POSIX SIGINT convention.
        os._exit(130)
    pool.shutdown()

    elapsed = time.monotonic() - t0
    avg_speed = total_bytes / elapsed if elapsed > 0 else 0
    log(
        f"Uploaded {len(files)} file(s), {fmt_size(total_bytes)} in {elapsed:.1f}s "
        f"({fmt_size(avg_speed)}/s)"
    )


def parse_rules(patterns):
    """Parse pattern strings into (archive_glob, entry_filter, strip_prefix) tuples.

    Format: ``archive_glob[!entry_filter]``
      archive_glob  — fnmatch against S3 key filename (e.g. '*-server.tar.zst')
      entry_filter  — optional fnmatch against archive entries (e.g. 'bin/*')
      strip_prefix  — fixed prefix before first glob char, stripped from output paths
                      so 'bin/qdb' extracts to output_dir/qdb, not output_dir/bin/qdb
    """
    rules = []
    for pat in patterns:
        if "!" in pat:
            archive_glob, entry_filter = pat.split("!", 1)
            prefix_end = len(entry_filter)
            for i, ch in enumerate(entry_filter):
                if ch in ("*", "?", "["):
                    prefix_end = i
                    break
            strip_prefix = entry_filter[:prefix_end]
            rules.append((archive_glob, entry_filter, strip_prefix))
        else:
            rules.append((pat, None, None))
    return rules


def match_rules(rel, rules):
    """Return (entry_filter, strip_prefix) for the first matching rule, or (None, None)."""
    for archive_glob, entry_filter, strip_prefix in rules:
        if fnmatch.fnmatch(rel, archive_glob):
            return entry_filter, strip_prefix
    return None, None


def _check_artifacts_exist(s3, bucket, prefix):
    """Check if any objects exist under the given prefix."""
    response = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/", MaxKeys=1)
    return response.get("KeyCount") > 0


def _read_latest_successful(s3, bucket, key):
    """Read the LATEST_SUCCESSFUL file from S3 to get the target build_id."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        return response["Body"].read().decode("utf-8").strip()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None
        raise


def _validate_artifact_path(cfg, auth, bucket, dest_prefix, project_id, ref, step, build_id):
    s3 = _s3_client(cfg, auth)
    master_ref = "refs/heads/master"

    if build_id != "LATEST_SUCCESSFUL":
        # Attempt to find artifacts under the current ref.
        # If not found and ref is not master/main, it falls back to checking refs/heads/master
        _, primary_prefix = scope(
            project_id, cfg, step_override=step, build_id_override=build_id, ref_override=ref
        )
        if _check_artifacts_exist(s3, bucket, primary_prefix):
            return primary_prefix

        if ref != master_ref and ref != "refs/heads/main":
            _, fallback_prefix = scope(
                project_id,
                cfg,
                step_override=step,
                build_id_override=build_id,
                ref_override=master_ref,
            )
            if _check_artifacts_exist(s3, bucket, fallback_prefix):
                return fallback_prefix
    else:
        # Reads the pointer file for the current ref.
        # If found, checks the target path for artifacts. If either the pointer file or target artifacts are missing, and ref is not master/main,
        # fall back to reading the refs/heads/master pointer file and checking that fallback path
        target_build_id = _read_latest_successful(s3, bucket, dest_prefix)

        if target_build_id:
            _, actual_prefix = scope(
                project_id,
                cfg,
                step_override=step,
                build_id_override=target_build_id,
                ref_override=ref,
            )
            if _check_artifacts_exist(s3, bucket, actual_prefix):
                return actual_prefix

        if ref != master_ref and ref != "refs/heads/main":
            _, fallback_pointer_key = scope(
                project_id,
                cfg,
                step_override=step,
                build_id_override="LATEST_SUCCESSFUL",
                ref_override=master_ref,
            )
            fallback_target_build_id = _read_latest_successful(s3, bucket, fallback_pointer_key)
            if fallback_target_build_id:
                _, actual_fallback_prefix = scope(
                    project_id,
                    cfg,
                    step_override=step,
                    build_id_override=fallback_target_build_id,
                    ref_override=master_ref,
                )
                if _check_artifacts_exist(s3, bucket, actual_fallback_prefix):
                    return actual_fallback_prefix

    # Abort if we couldn't find any artifacts
    die(
        f"Artifact path could not be resolved for project={project_id}, ref={ref}, step={step}, build_id={build_id} (and no fallbacks were available)"
    )


def download(
    project_id,
    build_id,
    ref_override,
    rules,
    step_override=None,
    output_dir=".",
    extract=False,
    clean=False,
    parallel=4,
    concurrency=32,
):
    """List matching artifacts under the step prefix and download/extract in parallel.
    Uses project_id to locate the artifacts namespace.
    Resolves build_id (can be LATEST_SUCCESSFUL) and applies fallback logic to find artifacts
    from main/master branch if missing on the current branch.

    --clean wipes output_dir first — needed because Buildkite retries reuse the same
    workspace, and stale artifacts from a failed attempt would corrupt test runs.
    With --extract, archives are downloaded to a temp file via boto3's transfer manager
    (parallel ranged GETs), then extracted locally, then the temp file is removed.
    """
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-only")
    bucket, pfx = scope(project_id, cfg, step_override, build_id)
    actual_build_id, step, ref = _get_scope_context(
        build_id, step_override, ref_override
    )  # validate context early before starting downloads
    pfx = _validate_artifact_path(cfg, auth, bucket, pfx, project_id, ref, step, actual_build_id)

    tc = TransferConfig(max_concurrency=concurrency)
    out = Path(output_dir).resolve()
    if clean and out.exists():
        log(f"Cleaning {out}")
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)

    list_client = _s3_client(cfg, auth)  # listing only; workers create their own
    objects = []
    for page in list_client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{pfx}/"
    ):
        for item in page.get("Contents", []):
            rel = item["Key"][len(pfx) + 1 :]  # path relative to step's artifact root
            if not rel:
                continue
            ef, sp = match_rules(rel, rules)
            matched = any(fnmatch.fnmatch(rel, ag) for ag, _, _ in rules)
            if matched:
                objects.append((item["Key"], rel, item["Size"], ef, sp))

    if not objects:
        rule_strs = [f"{ag}!{ef}" if ef else ag for ag, ef, _ in rules]
        die(f"no artifacts matched: {rule_strs}")

    total_bytes = sum(sz for _, _, sz, _, _ in objects)
    log(
        f"Downloading {len(objects)} artifact(s) ({fmt_size(total_bytes)}) from "
        f"s3://{bucket}/{pfx}/ via backend={cfg.backend}"
    )
    log(f"  parallel={parallel}, concurrency={concurrency}")

    def _download_one(key, rel, sz, entry_filter, strip_prefix):
        log(f"  {rel} ({fmt_size(sz)})")
        ft0 = time.monotonic()

        def _do():
            if extract:
                tmp = tempfile.NamedTemporaryFile(dir=out, suffix=".tmp", delete=False)
                tmp_path = tmp.name
                tmp.close()
                try:
                    _s3_client(cfg, auth).download_file(bucket, key, tmp_path, Config=tc)
                    extract_local_archive(tmp_path, rel, out, entry_filter, strip_prefix)
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
        log(f"           done in {elapsed:.1f}s ({fmt_size(speed)}/s)")

    t0 = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=parallel)
    futs = {pool.submit(_download_one, k, r, sz, ef, sp): r for k, r, sz, ef, sp in sorted(objects)}
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


def set_latest_successful(project_id, step):
    """Update the LATEST_SUCCESSFUL pointer file for the current ref to point to the current build ID.
    This allows test steps to use --build-id LATEST_SUCCESSFUL to always get the latest green build's artifacts."""
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    bucket, pfx = scope(project_id, cfg, step_override=step, skip_build_id=True)

    build_id, _, ref = _get_scope_context(step_override=step)

    target_key = key_join(pfx, "LATEST_SUCCESSFUL")
    _s3_client(cfg, auth).put_object(
        Bucket=bucket,
        Key=target_key,
        Body=build_id.encode("utf-8"),
        ContentType="text/plain",
    )
    log(f"Set {target_key} → {build_id} for ref {ref}")


def _filter_tar_member(member, entry_filter, strip_prefix):
    """Apply filter + strip to a tar member. Returns mutated member or None to skip.
    member.name is mutated in-place (tarfile's path remapping convention)."""
    if entry_filter and not fnmatch.fnmatch(member.name, entry_filter):
        return None
    if strip_prefix and member.name.startswith(strip_prefix):
        member.name = member.name[len(strip_prefix) :]
    if not member.name:
        return None
    return member


def _extract_tar(tar, out, entry_filter, strip_prefix):
    """Extract tar members with optional filtering. filter="data" rejects absolute
    paths, traversal (../), and device files. Streaming mode means sequential-only
    access — skipped members still consume I/O from the stream."""
    if entry_filter is None:
        tar.extractall(path=out, filter="data")
    else:
        for member in tar:
            member = _filter_tar_member(member, entry_filter, strip_prefix)
            if member is None:
                continue
            tar.extract(member, path=out, filter="data")


def extract_local_archive(path, rel, output_dir, entry_filter=None, strip_prefix=None):
    """Extract a locally downloaded archive file.

    .tar.gz  — tarfile 'r:gz' (seekable, more efficient than streaming mode)
    .tar.zst — zstandard.stream_reader → tarfile 'r|'
    .zip     — zipfile.ZipFile (seekable local file)
    """
    out = Path(output_dir)

    if rel.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as tar:
            _extract_tar(tar, out, entry_filter, strip_prefix)

    elif rel.endswith(".tar.zst") or rel.endswith(".tar.zstd"):
        dctx = zstandard.ZstdDecompressor()
        with open(path, "rb") as fh:
            with dctx.stream_reader(fh) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    _extract_tar(tar, out, entry_filter, strip_prefix)

    elif rel.endswith(".zip"):
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                if entry_filter and not fnmatch.fnmatch(name, entry_filter):
                    continue
                if strip_prefix and name.startswith(strip_prefix):
                    name = name[len(strip_prefix) :]
                if not name:
                    continue
                dest = out / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    else:
        die(f"unsupported archive format: {rel}")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args.pop(0)
    parallel = 4
    concurrency = 32
    project_id = None
    build_id = "LATEST_SUCCESSFUL"

    if cmd == "upload":
        pattern = "*.tar.zst"
        i = 0
        while i < len(args):
            if args[i] == "--parallel":
                parallel = int(args[i + 1])
                i += 2
            elif args[i] == "--concurrency":
                concurrency = int(args[i + 1])
                i += 2
            elif args[i].startswith("--project-id"):
                project_id = args[i + 1]
                i += 2
            else:
                pattern = args[i]
                i += 1

        if not project_id:
            die("missing required --project-id argument")
        upload(project_id, pattern, parallel, concurrency)

    elif cmd == "set-latest":
        # this is a separate step because sometimes we want to
        step = None
        i = 0
        while i < len(args):
            if args[i] == "--step":
                step = args[i + 1]
                i += 2
            elif args[i] == "--project-id":
                project_id = args[i + 1]
                i += 2
            else:
                die(f"unknown argument: {args[i]}")

        if not step:
            die("missing required --step argument")
        if not project_id:
            die("missing required --project-id argument")
        set_latest_successful(project_id, step)

    elif cmd == "download":
        step = None
        extract = False
        no_extract = clean = False
        ref = None
        patterns = []
        out = "."
        i = 0
        while i < len(args):
            if args[i] == "--step":
                step = args[i + 1]
                i += 2
            elif args[i] == "--build-id":
                build_id = args[i + 1]
                i += 2
            elif args[i] == "--ref":
                ref = args[i + 1]
                i += 2
            elif args[i] == "--project-id":
                project_id = args[i + 1]
                i += 2
            elif args[i] == "--extract":
                extract = True
                i += 1
            elif args[i] == "--no-extract":
                no_extract = True
                i += 1
            elif args[i] == "--output-dir":
                out = args[i + 1]
                i += 2
            elif args[i] == "--clean":
                clean = True
                i += 1
            elif args[i] == "--parallel":
                parallel = int(args[i + 1])
                i += 2
            elif args[i] == "--concurrency":
                concurrency = int(args[i + 1])
                i += 2
            else:
                patterns.append(args[i])
                i += 1
        if not project_id:
            die("missing required --project-id argument")
        if not patterns:
            patterns = ["*.tar.zst"]
        rules = parse_rules(patterns)
        download(
            project_id,
            build_id,
            ref,
            rules,
            step,
            out,
            extract and not no_extract,
            clean,
            parallel,
            concurrency,
        )

    else:
        die(f"unknown command: {cmd}")
