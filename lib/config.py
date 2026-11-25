"""Which store the plugin uses: StoreConfig, read from the environment, then SSM.

Each value comes from an ARTIFACTS_* variable when the job sets a non-empty one, else from an
SSM parameter, else from a default: variables for per-job overrides, SSM for the configuration
all agents share, defaults for a plain S3 setup with an IAM role.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from botocore.exceptions import ClientError

from .errors import ArtifactsError
from .log import log

SSM = "/services/buildkite/config/artifacts/object-store"
BACKEND_SSM_PARAM = f"{SSM}/backend"
DESTINATION_SSM_PARAM = f"{SSM}/destination"
ENDPOINT_URL_SSM_PARAM = f"{SSM}/endpoint-url"
ARTIFACTS_DOMAIN_SSM_PARAM = f"{SSM}/r2/artifacts-domain"

CREDENTIALS = "/services/buildkite/credentials/artifacts"
# What a caller does with the store: the key pair of an R2 store depends on it.
READ = "read"
WRITE = "write"

# What the R2 backend needs of every caller: the field, its variable, its SSM parameter, and its
# name in a message.
R2_ACCOUNT = ("r2_account_id", "ARTIFACTS_R2_ACCOUNT_ID", f"{SSM}/r2/account-id", "account ID")

# The key pair of a caller that writes, and the key pair of one that only reads, which holds
# no permission to write.
R2_READ_WRITE = (
    ("r2_access_key_id", "ARTIFACTS_R2_ACCESS_KEY_ID", f"{SSM}/r2/access-key-id", "access key ID"),
    (
        "r2_secret_access_key",
        "ARTIFACTS_R2_SECRET_ACCESS_KEY",
        f"{CREDENTIALS}/r2/secret-access-key",
        "secret access key",
    ),
)
R2_READ_ONLY = (
    (
        "r2_access_key_id",
        "ARTIFACTS_R2_READ_ONLY_ACCESS_KEY_ID",
        f"{SSM}/r2/read-only-access-key-id",
        "read-only access key ID",
    ),
    (
        "r2_secret_access_key",
        "ARTIFACTS_R2_READ_ONLY_SECRET_ACCESS_KEY",
        f"{CREDENTIALS}/r2/read-only-secret-access-key",
        "read-only secret access key",
    ),
)

# Prefixes of a value that was never replaced with a real one.
PLACEHOLDER_PREFIXES = ("SET_ME", "REPLACE_", "TODO", "CHANGEME", "FIXME")


@dataclass(frozen=True)  # frozen -> safe to share across worker threads
class StoreConfig:
    backend: str
    destination: str
    endpoint_url: str | None = None
    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    artifacts_domain: str | None = None


def ssm_value(ssm, name: str) -> str | None:
    """The value of an SSM parameter, None when there is none. Any other error is raised, so
    that a missing IAM permission shows."""
    try:
        return ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except ClientError as exc:
        error = exc.response.get("Error", {})
        if error.get("Code") == "ParameterNotFound":
            return None
        message = error.get("Message", str(exc))
        raise ArtifactsError(
            f"failed to read SSM parameter {name}: {error.get('Code')} -- {message}"
        ) from exc


def env_or_ssm(ssm, env_name: str, ssm_name: str) -> str | None:
    """The variable when the job sets it, else the SSM parameter."""
    return os.environ.get(env_name) or ssm_value(ssm, ssm_name)


def r2_value(ssm, env_name: str, param: str, label: str) -> str:
    """One value the R2 backend needs; an absent one and a placeholder are errors."""
    value = env_or_ssm(ssm, env_name, param)
    if not value:
        raise ArtifactsError(
            f"R2 backend requires its {label}. Set env var {env_name} or SSM parameter {param}"
        )
    if value.startswith(PLACEHOLDER_PREFIXES):
        raise ArtifactsError(
            f"R2 {label} (SSM {param}) contains placeholder value {value!r} -- "
            "replace it with the real credential in Terraform tfvars or SSM"
        )
    return value


def r2_read_only_pair(ssm) -> dict[str, str] | None:
    """The read-only key pair, None when the store has none. Half a pair is an error: it
    would sign with a key that belongs to no secret."""
    found = {field: env_or_ssm(ssm, env, param) for field, env, param, _ in R2_READ_ONLY}
    if not any(found.values()):
        return None
    if not all(found.values()):
        raise ArtifactsError(
            "R2 read-only key pair is incomplete. Set both or neither of "
            + " and ".join(f"{env} (SSM {param})" for _, env, param, _ in R2_READ_ONLY)
        )
    return {field: r2_value(ssm, *rest) for field, *rest in R2_READ_ONLY}


def r2_key_pair(ssm, access: str) -> dict[str, str]:
    """The R2 key pair for a caller that does ``access``. A reader takes the read-only pair, and
    until the store has one it takes the read-write pair, which the log says, so that a
    plugin that is rolled out before the pair exists keeps working."""
    if access == READ:
        read_only = r2_read_only_pair(ssm)
        if read_only is not None:
            return read_only
        log(
            "WARNING: no read-only R2 key pair is configured, so this read uses the read-write "
            f"pair. Set {R2_READ_ONLY[0][1]} and {R2_READ_ONLY[1][1]}, or their SSM parameters"
        )
    return {field: r2_value(ssm, *rest) for field, *rest in R2_READ_WRITE}


def load_store_config(ssm, access: str = READ) -> StoreConfig:
    """The store configuration: variables, then SSM, then defaults. For R2 the endpoint
    defaults to https://{account_id}.r2.cloudflarestorage.com, and the key pair is the one for
    ``access``: READ, the default, for a caller that only reads, WRITE for one that writes."""
    if access not in (READ, WRITE):
        raise ValueError(f"access must be {READ!r} or {WRITE!r}, got {access!r}")
    backend = (env_or_ssm(ssm, "ARTIFACTS_BACKEND", BACKEND_SSM_PARAM) or "s3").strip().lower()
    if backend not in {"s3", "r2"}:
        raise ArtifactsError(
            f"unsupported artifact backend: {backend!r} (expected 's3' or 'r2'). "
            f"Check env var ARTIFACTS_BACKEND or SSM parameter {BACKEND_SSM_PARAM}"
        )
    destination = env_or_ssm(ssm, "ARTIFACTS_DESTINATION", DESTINATION_SSM_PARAM)
    if not destination:
        raise ArtifactsError(
            "artifact destination not configured. "
            f"Set env var ARTIFACTS_DESTINATION or SSM parameter {DESTINATION_SSM_PARAM}"
        )
    shared = {
        "backend": backend,
        "destination": destination,
        # The variable applies to both backends; mainly for local MinIO and development.
        "endpoint_url": os.environ.get("ARTIFACTS_ENDPOINT_URL"),
        "artifacts_domain": env_or_ssm(ssm, "ARTIFACTS_DOMAIN", ARTIFACTS_DOMAIN_SSM_PARAM),
    }
    if backend == "s3":
        return StoreConfig(**shared)
    endpoint_url = shared["endpoint_url"] or ssm_value(ssm, ENDPOINT_URL_SSM_PARAM)
    account = {R2_ACCOUNT[0]: r2_value(ssm, *R2_ACCOUNT[1:])}
    r2 = {**account, **r2_key_pair(ssm, access)}
    default = f"https://{r2['r2_account_id']}.r2.cloudflarestorage.com"
    return StoreConfig(**{**shared, **r2, "endpoint_url": endpoint_url or default})
