"""boto3 clients for the object store, and the credentials they use.

Both backends speak the S3 API through boto3, and this module is the only place
where they differ. S3 signs with the agent's IAM role through boto3's default
credential chain. R2 signs with an S3-compatible key pair kept in the store
configuration, so no Cloudflare SDK and no minting of temporary credentials is
needed: boto3 talks to the R2 S3 endpoint directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import boto3
from botocore.config import Config

READ_TIMEOUT = 300  # 5 minutes -- fail fast instead of hanging 40+ minutes
CONNECT_TIMEOUT = 60  # 1 minute


@dataclass(frozen=True)
class ObjectAuth:
    """Per-operation S3 credentials. All None for S3 (IAM role handles auth).
    For R2, populated from SSM-stored credentials derived from the Cloudflare API token.
    """

    access_key_id: str | None = None
    secret_access_key: str | None = None


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


def resolve_object_auth(ssm, cfg, permission):
    """S3 -> empty auth (IAM role chain). R2 -> use stored S3-compatible credentials."""
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
    account-level, not bucket-level -- virtual-host style would produce invalid hostnames).
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
