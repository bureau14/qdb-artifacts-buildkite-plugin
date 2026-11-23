"""boto3 clients for the object store.

Both backends speak the S3 API through boto3, and this module is the only place where they
differ. S3 signs with the agent's IAM role through boto3's default credential chain. R2 signs
with the S3-compatible key pair of the store configuration, against the R2 S3 endpoint.
"""

from __future__ import annotations

import os

import boto3
from botocore.config import Config

READ_TIMEOUT = 300  # 5 minutes -- fail fast instead of hanging 40+ minutes
CONNECT_TIMEOUT = 60  # 1 minute

# R2 endpoints are account-level, not bucket-level: virtual-host addressing would give host
# names that do not exist.
R2_CONFIG = {"signature_version": "s3v4", "s3": {"addressing_style": "path"}}


def ssm_client():
    """The SSM client the store configuration is read with."""
    region = os.environ.get("AWS_DEFAULT_REGION", "eu-west-1")
    retries = {"mode": "standard", "max_attempts": 5}
    return boto3.client("ssm", config=Config(region_name=region, retries=retries))


def s3_client(cfg):
    """A new S3 client for the store of ``cfg``. Every thread makes its own: a boto3 client is
    not safe to share."""
    r2 = cfg.backend == "r2"
    config = Config(
        read_timeout=READ_TIMEOUT,
        connect_timeout=CONNECT_TIMEOUT,
        retries={"mode": "standard", "max_attempts": 10},
        **(R2_CONFIG if r2 else {}),
    )
    credentials = {
        "region_name": "auto",
        "aws_access_key_id": cfg.r2_access_key_id,
        "aws_secret_access_key": cfg.r2_secret_access_key,
    }
    endpoint = {"endpoint_url": cfg.endpoint_url} if cfg.endpoint_url else {}
    return boto3.client("s3", config=config, **(credentials if r2 else {}), **endpoint)
