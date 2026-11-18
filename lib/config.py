"""Which store the plugin uses: StoreConfig, read from the environment, then SSM.

Each value comes from an ARTIFACTS_* variable when the job sets a non-empty
one, else from an SSM parameter, else from a default: variables for per-job
overrides, SSM for the configuration all agents share, defaults for a plain S3
setup with an IAM role.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from botocore.exceptions import ClientError

from .errors import die

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


@dataclass(frozen=True)  # frozen -> safe to share across worker threads
class StoreConfig:
    """Resolved object-store backend config. Populated once by load_store_config()."""

    backend: str
    destination: str
    endpoint_url: str | None = None
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    artifacts_domain: str | None = None


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
        die(f"failed to read SSM parameter {name}: {code} \N{EM DASH} {msg}")


# Prefixes that indicate a placeholder / unconfigured value rather than a real one.
_PLACEHOLDER_PREFIXES = ("SET_ME", "REPLACE_", "TODO", "CHANGEME", "FIXME")


def _check_placeholder(value, label):
    """Die if value looks like a placeholder that was never replaced with a real one."""
    if value and any(value.startswith(p) for p in _PLACEHOLDER_PREFIXES):
        die(
            f"{label} contains placeholder value {value!r} \N{EM DASH} "
            f"replace it with the real credential in Terraform tfvars or SSM"
        )


def _env_or_ssm(ssm, env_name, ssm_name, with_decryption=True):
    """Env var with SSM fallback. Env takes precedence for per-job overrides."""
    return os.environ.get(env_name) or _ssm_get_optional(
        ssm, ssm_name, with_decryption=with_decryption
    )


def load_store_config(ssm):
    """Resolve backend config: env vars -> SSM -> defaults.
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
