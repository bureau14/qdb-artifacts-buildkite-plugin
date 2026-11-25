"""Which store the plugin uses: StoreConfig, read from the environment, then SSM.

Each value comes from an ARTIFACTS_* variable when the job sets a non-empty one, else from an
SSM parameter, else from a default: variables for per-job overrides, SSM for the configuration
all agents share, defaults for a plain S3 setup with an IAM role.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from botocore.exceptions import ClientError

from .errors import ArtifactsError

SSM = "/services/buildkite/config/artifacts/object-store"
BACKEND_SSM_PARAM = f"{SSM}/backend"
DESTINATION_SSM_PARAM = f"{SSM}/destination"
ENDPOINT_URL_SSM_PARAM = f"{SSM}/endpoint-url"
ARTIFACTS_DOMAIN_SSM_PARAM = f"{SSM}/r2/artifacts-domain"

CREDENTIALS = "/services/buildkite/credentials/artifacts"
# What the R2 backend needs: the field, its variable, its SSM parameter, and its name in a
# message.
R2_REQUIRED = (
    ("r2_account_id", "ARTIFACTS_R2_ACCOUNT_ID", f"{SSM}/r2/account-id", "account ID"),
    ("r2_access_key_id", "ARTIFACTS_R2_ACCESS_KEY_ID", f"{SSM}/r2/access-key-id", "access key ID"),
    (
        "r2_secret_access_key",
        "ARTIFACTS_R2_SECRET_ACCESS_KEY",
        f"{CREDENTIALS}/r2/secret-access-key",
        "secret access key",
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


@dataclass(frozen=True)
class ObjectAuth:
    """Explicit credentials for R2, or an empty pair for the S3 IAM credential chain."""

    access_key_id: str | None = None
    secret_access_key: str | None = field(default=None, repr=False)


def resolve_object_auth(ssm, cfg, permission):
    """Expose the credentials already resolved by load_store_config for URL signing.

    The configured R2 pair determines access. The permission argument describes the
    operation and cannot grant or narrow the permissions on that pair.
    """
    if cfg.backend == "r2":
        return ObjectAuth(cfg.r2_access_key_id, cfg.r2_secret_access_key)
    return ObjectAuth()


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


def load_store_config(ssm) -> StoreConfig:
    """The store configuration: variables, then SSM, then defaults. For R2 the endpoint
    defaults to https://{account_id}.r2.cloudflarestorage.com."""
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
    r2 = {field: r2_value(ssm, *rest) for field, *rest in R2_REQUIRED}
    default = f"https://{r2['r2_account_id']}.r2.cloudflarestorage.com"
    return StoreConfig(**{**shared, **r2, "endpoint_url": endpoint_url or default})
