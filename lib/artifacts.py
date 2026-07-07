#!/usr/bin/env python3
from __future__ import annotations
import subprocess

"""Artifact upload/download for Buildkite CI pipelines (AWS S3 + Cloudflare R2).

Replaces Buildkite's native artifact handling with backend choice (S3/R2), streaming
in-memory extraction, entry-level filtering, and two-level parallel transfers.

Key layout
----------
    s3://<bucket>/<dest_prefix>/<project_id>/<ref>/variants/<variant_key>/builds/<build_id>/<cwd_relative_path>

Project ID, ref, and build ID isolate pipeline runs; variant key separates different build variants. CWD-relative paths preserve
the original directory structure.

Cross-step sharing
------------------
Build steps upload under variant key. Test steps download with
``--variant linux-amd64-release`` to look in the variant namespace. The --variant value is substituted at pipeline-generation time.

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
  .zip | .jar    — zipfile.ZipFile (seekable local file)

Entry filtering
---------------
Patterns like ``*-server.tar.zst!bin/*`` match archives by glob, extract only
entries matching ``bin/*``, and strip the ``bin/`` prefix from output paths.
Test steps use this to pull only executables/libraries from component archives.

Download exclusions
-------------------
Download project config can set ``exclude`` archive globs. Exclusions are matched
against the artifact path after ``files`` includes match, and excluded artifacts
are not downloaded or extracted.

Upload exclusions
-----------------
Upload config can set ``exclude`` file globs. Exclusions are matched against the
uploaded artifact path after ``files`` includes match, and excluded files are not
uploaded or listed in the manifest/annotation.

Usage
-----
    # upload (build step)
    python3 .buildkite/tools/artifacts.py upload 'artifacts/**/*.tar.zst'

    # download + extract (test step, cross-step, entry-filtered)
    python3 .buildkite/tools/artifacts.py download \\
        --variant build-linux-amd64-release --extract --clean --output-dir artifacts \\
        '*-c-api.tar.zst!lib/*' '*-server.tar.zst!bin/*' '*-tests.tar.zst!bin/*'
"""

import datetime
import fnmatch
import glob
import os
import posixpath
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
ARTIFACTS_DOMAIN_SSM_PARAM = "/services/buildkite/config/artifacts/object-store/r2/artifacts-domain"


@dataclass(frozen=True)  # frozen → safe to share across worker threads
class StoreConfig:
    """Resolved object-store backend config. Populated once by load_store_config()."""

    backend: str
    destination: str
    endpoint_url: str | None = None
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    artifacts_domain: str | None = None


@dataclass(frozen=True)
class ObjectAuth:
    """Per-operation S3 credentials. All None for S3 (IAM role handles auth).
    For R2, populated from SSM-stored credentials derived from the Cloudflare API token.
    """

    access_key_id: str | None = None
    secret_access_key: str | None = None


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
    region_name = os.environ.get("AWS_DEFAULT_REGION", "eu-west-1")
    return (
        s.client("s3", config=Config(retries={"mode": "standard", "max_attempts": 10})),
        s.client(
            "ssm",
            config=Config(
                region_name=region_name,
                retries={
                    "mode": "standard",
                    "max_attempts": 5,
                },
            ),
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

    artifacts_domain = os.environ.get("ARTIFACTS_DOMAIN") or _ssm_get_optional(
        ssm, ARTIFACTS_DOMAIN_SSM_PARAM
    )

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
            artifacts_domain=artifacts_domain,
        )

    return StoreConfig(
        backend=backend,
        destination=destination,
        endpoint_url=endpoint_url,
        artifacts_domain=artifacts_domain,
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


def scope(
    cfg,
    project_id,
    build_id,
    variant,
    git_ref,
    skip_build_id=False,
):
    """Resolve (bucket, key_prefix) for the current build/variant context.
    project_id scopes the artifacts to a specific project.
    variant (--variant) specifies os/arch/etc. to use. Resolved at pipeline-generation time.
    git_ref (--git-ref) specifies the Git reference (branch or tag) to use.
    skip_build_id skips the build ID in the path (used for LATEST_SUCCESSFUL pointers)."""

    bucket, prefix = parse_s3(cfg.destination)

    if skip_build_id:
        return bucket, key_join(prefix, project_id, git_ref, "variants", variant)

    if build_id == "LATEST_SUCCESSFUL":
        return bucket, key_join(
            prefix, project_id, git_ref, "variants", variant, "LATEST_SUCCESSFUL"
        )

    return bucket, key_join(prefix, project_id, git_ref, "variants", variant, "builds", build_id)


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


def _create_buildkite_annotation(artifacts_domain, prefix, files):
    if not os.environ.get("BUILDKITE"):
        log(
            "  BUILDKITE env var not set; skipping adding Buildkite annotation, not running in Buildkite?"
        )
        return
    if not artifacts_domain:
        log("  Artifacts domain not configured; skipping adding Buildkite annotation")
        return
    log(f"  creating Buildkite annotation for {len(files)} uploaded artifact(s)")
    job_label = os.environ.get("BUILDKITE_LABEL", "unknown job")
    uri = f"https://{artifacts_domain}"
    body = f"## {job_label} \n ### Uploaded artifacts:\n\n" + "\n".join(
        f"- [{file}]({key_join(uri, prefix, file)})" for file in files
    )

    # We could use REST API calls here and avoid depending on the binary (and calling it from subprocess) but REST API has some limitations that make it less ideal:
    # - auth tokens are per user not per organization, token will stop working when account is removed
    # - API rate limits are low, you can hit them if you have a lot of builds uploading artifacts at the same time, would need a backoff and retry mechanism
    buildkite_agent_exec = "buildkite-agent"
    if os.name == "nt":
        buildkite_agent_exec += ".exe"
    args = [
        buildkite_agent_exec,
        "annotate",
        body,
        "--context",
        "artifacts",
        "--style",
        "info",
        "--priority",
        "10",
        "--scope",
        "job",
    ]
    log(f"  running command: {' '.join(args)}")
    try:
        subprocess.run(args, check=True)
    except Exception as e:
        log(f"  :warning Failed to create Buildkite annotation: {e}")


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
):
    """Glob files and upload to the current build/variant prefix. CWD-relative paths
    become the S3 key suffix, preserving directory structure.
    Uploads a manifest.json file containing metadata about the uploaded files once all uploads are complete.

    Creds are minted once before the pool — for R2, ensure TTL covers the full upload.
    """
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")

    bucket, pfx = scope(cfg, project_id, build_id, variant, git_ref)

    tc = TransferConfig(max_concurrency=concurrency)
    cwd = Path.cwd().resolve()

    base_path = cwd
    if base_dir:
        base_path = Path(base_dir).resolve()
        if not base_path.is_dir():
            die(f"--base-dir {base_dir!r} does not exist or is not a directory")

    matched_files = sorted(
        {
            Path(f).resolve()
            for p in patterns
            for f in glob.glob(p, recursive=True)
            if Path(f).is_file()
        }
    )
    if not matched_files:
        die(f"no files matched: {patterns}")

    if base_dir:
        for f in matched_files:
            try:
                f.relative_to(base_path)
            except ValueError:
                die(f"file {f} is not under base_dir {base_dir}")

    files = [
        f
        for f in matched_files
        if not is_excluded(f.relative_to(base_path).as_posix(), exclude_patterns)
    ]
    if not files:
        die(f"no files left after applying exclude patterns: {exclude_patterns or []}")

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
    relative_files_path = [f.relative_to(base_path).as_posix() for f in files]
    try:
        for fut in as_completed(futs):
            fut.result()

        # After uploads are done, write a manifest file with metadata about the uploaded artifacts.
        # might come handy for debugging / validation / future features
        manifest_payload = {
            "file_count": len(files),
            "files": relative_files_path,
            "uploaded_at": datetime.datetime.now().isoformat() + "Z",
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
        _create_buildkite_annotation(cfg.artifacts_domain, pfx, relative_files_path)
    else:
        log("  annotate=false; skipping Buildkite annotation")
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


def is_excluded(rel, exclude_patterns):
    """Return True when rel matches any configured exclude archive glob."""
    return any(fnmatch.fnmatch(rel, pat) for pat in exclude_patterns or [])


def _check_artifacts_exist(s3, bucket, prefix):
    """Check if any objects exist under the given prefix."""
    response = s3.list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/", MaxKeys=1)
    return response.get("KeyCount") > 0


def _read_latest_successful(s3, bucket, key):
    """Read the LATEST_SUCCESSFUL pointer. Returns (build_id, pointer_lastmod) or
    (None, None) if absent. The pointer mtime is useful as a build-age proxy
    when the build itself has no manifest.json."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
        bid = response["Body"].read().decode("utf-8").strip()
        return bid, response.get("LastModified")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            return None, None
        raise


def _read_manifest(s3, bucket, prefix):
    """Best-effort fetch of <prefix>/manifest.json. Returns dict or None on any
    error -- this is purely informational and must never break the download."""
    try:
        response = s3.get_object(Bucket=bucket, Key=key_join(prefix, "manifest.json"))
        return json.loads(response["Body"].read())
    except Exception:
        return None


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


def _validate_artifact_path(cfg, auth, bucket, dest_prefix, project_id, ref, variant, build_id):
    """Resolve the artifact prefix, logging every attempt as it goes.

    Walks [requested_ref, refs/heads/master, refs/heads/main] in order, resolving
    LATEST_SUCCESSFUL pointers when needed and skipping refs whose prefix is empty.
    Returns a ResolvedBuild describing the winning attempt. On failure, dies with
    a structured trace listing every attempt that was tried and why each failed.
    """
    s3 = _s3_client(cfg, auth)
    master_refs = ("refs/heads/master", "refs/heads/main")

    refs_to_check = [ref]
    if ref not in master_refs:
        refs_to_check += list(master_refs)

    # Each entry: dict(ref, resolved_build_id_or_None, prefix_or_None, status, detail)
    attempts = []

    for current_ref in refs_to_check:
        log(f"resolve attempt: ref={current_ref} build={build_id}")
        pointer_lastmod = None
        effective_build_id = build_id

        if build_id == "LATEST_SUCCESSFUL":
            # For the primary ref, dest_prefix already points at the LATEST_SUCCESSFUL
            # pointer key (scope() appends LATEST_SUCCESSFUL). For fallbacks, recompute.
            pointer_key = (
                dest_prefix
                if current_ref == ref
                else scope(cfg, project_id, build_id, variant, current_ref)[1]
            )
            effective_build_id, pointer_lastmod = _read_latest_successful(s3, bucket, pointer_key)
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

        _, prefix = scope(cfg, project_id, effective_build_id, variant, current_ref)

        if _check_artifacts_exist(s3, bucket, prefix):
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
        f"Artifact path could not be resolved for project={project_id}, "
        f"variant={variant}, requested_ref={ref}, requested_build_id={build_id}.",
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
    die("\n".join(lines))


def _download(
    project_id,
    build_id,
    git_ref,
    rules,
    variant,
    exclude_patterns=None,
    output_dir=".",
    extract=False,
    parallel=4,
    concurrency=32,
):
    """List matching artifacts under the variant prefix and download/extract in parallel.
    Uses project_id to locate the artifacts namespace. If omitted, project_id defaults
    to the current BUILDKITE_PIPELINE_SLUG.
    Resolves build_id (can be LATEST_SUCCESSFUL) and applies fallback logic to find artifacts
    from main/master branch if missing on the current branch. If build_id is omitted, it defaults
    to BUILDKITE_BUILD_ID if downloading from the current pipeline and ref, otherwise LATEST_SUCCESSFUL.

    --clean wipes output_dir first — needed because Buildkite retries reuse the same
    workspace, and stale artifacts from a failed attempt would corrupt test runs.
    With --extract, archives are downloaded to a temp file via boto3's transfer manager
    (parallel ranged GETs), then extracted locally, then the temp file is removed.
    """
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-only")
    bucket, pfx = scope(cfg, project_id, build_id, variant, git_ref)
    resolved = _validate_artifact_path(
        cfg, auth, bucket, pfx, project_id, git_ref, variant, build_id
    )
    pfx = resolved.prefix

    list_client = _s3_client(cfg, auth)  # listing only; workers create their own

    if resolved.fallback_applied:
        log(
            f"WARNING: falling back from ref={resolved.requested_ref} to "
            f"ref={resolved.ref} (no artifacts on the requested ref)"
        )

    # Canonical resolved-build summary -- emitted once per project, before any transfer.
    manifest = _read_manifest(list_client, bucket, pfx)
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

    tc = TransferConfig(max_concurrency=concurrency)
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    objects = []
    present_keys = []  # everything at the prefix except manifest.json, for diagnostics
    for page in list_client.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{pfx}/"
    ):
        for item in page.get("Contents", []):
            rel = item["Key"][len(pfx) + 1 :]  # path relative to variant's artifact root
            if not rel or rel == "manifest.json":
                continue
            present_keys.append(rel)
            matched = any(fnmatch.fnmatch(rel, ag) for ag, _, _ in rules)
            if matched and not is_excluded(rel, exclude_patterns):
                ef, sp = match_rules(rel, rules)
                objects.append((item["Key"], rel, item["Size"], ef, sp))

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
            # _validate_artifact_path only returns a prefix where _check_artifacts_exist
            # was True, so reaching here means the only object was manifest.json.
            lines.append("  present  : (only manifest.json -- this build uploaded no artifacts)")
        die("\n".join(lines))

    total_bytes = sum(sz for _, _, sz, _, _ in objects)
    log(
        f"Downloading {len(objects)} artifact(s) ({fmt_size(total_bytes)}) from "
        f"s3://{bucket}/{pfx}/ via backend={cfg.backend}"
    )
    log(f"  parallel={parallel}, concurrency={concurrency}")

    def _download_one(key, rel, sz, entry_filter, strip_prefix):
        log(f"  start: {rel} ({fmt_size(sz)})")
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
        # Loud destination line. rel is repeated so parallel-interleaved logs are still parseable.
        if extract:
            extras = []
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


def download(projects_config, dirs_to_clean, parallel=4, concurrency=32):
    for d in dirs_to_clean:
        if d.exists():
            log(f"Cleaning {d}")
            shutil.rmtree(d)

    for p in projects_config:
        rules = parse_rules(p["files"])
        _download(
            project_id=p["resolved_project_id"],
            build_id=p["resolved_build_id"],
            git_ref=p["git_ref"],
            rules=rules,
            variant=p["variant"],
            exclude_patterns=p.get("exclude", []),
            output_dir=p["output_dir"],
            extract=p["extract"],
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


def _assert_tar_path_is_safe(out, name):
    normalized = posixpath.normpath(name)
    if normalized in ("", ".") or normalized.startswith("../") or normalized == "..":
        raise tarfile.FilterError(f"refusing unsafe tar member path: {name}")
    if posixpath.isabs(normalized):
        raise tarfile.FilterError(f"refusing absolute tar member path: {name}")

    out = out.resolve()
    target = (out / normalized).resolve()
    try:
        target.relative_to(out)
    except ValueError as exc:
        raise tarfile.FilterError(f"refusing tar member outside output dir: {name}") from exc
    return target


def _safe_tar_member(member, out):
    """Apply Python 3.12-style data filtering without the 3.12-only
    TarFile.extract(..., filter=...) keyword.

    Python 3.9 agents do not accept that keyword, so pre-filter members and call
    extract() with the older signature. On Python versions with
    tarfile.data_filter, delegate to the stdlib implementation. Otherwise use a
    conservative fallback that rejects traversal/absolute paths, device files,
    and links escaping the output directory.
    """
    data_filter = getattr(tarfile, "data_filter", None)
    if data_filter is not None:
        return data_filter(member, str(out))

    target = _assert_tar_path_is_safe(out, member.name)
    if member.isdev():
        raise tarfile.FilterError(f"refusing special tar member: {member.name}")

    if member.issym() or member.islnk():
        if posixpath.isabs(member.linkname):
            raise tarfile.FilterError(f"refusing absolute tar link target: {member.name}")
        link_base = out.resolve() if member.islnk() else target.parent
        try:
            (link_base / member.linkname).resolve().relative_to(out.resolve())
        except ValueError as exc:
            raise tarfile.FilterError(
                f"refusing tar link outside output dir: {member.name}"
            ) from exc

    return member


def _extract_tar(tar, out, entry_filter, strip_prefix):
    """Extract tar members with optional filtering. Data filtering rejects
    absolute paths, traversal (../), and device files. Streaming mode means
    sequential-only access — skipped members still consume I/O from the stream."""
    for member in tar:
        member = _filter_tar_member(member, entry_filter, strip_prefix)
        if member is None:
            continue
        member = _safe_tar_member(member, out)
        if member is None:
            continue
        tar.extract(member, path=out)


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

    elif rel.endswith(".zip") or rel.endswith(".jar"):
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


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args.pop(0)
    cmd_project_id = None
    cmd_build_id = None
    variant = None
    git_ref = None

    if cmd == "upload":
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

        upload(
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
        )

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
