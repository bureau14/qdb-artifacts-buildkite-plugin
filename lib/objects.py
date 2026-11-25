"""Small objects of the store, behind one interface over a bucket and its client.

A manifest, a latest file, ``key.json``, a failure record and the listing of
one level of a prefix each take one request (a listing one per page). Files of
a step travel through boto3's transfer manager instead, in ``upload`` and
``download``.

An object that is not there is not an error: ``get`` says so with None,
because a missing manifest or latest file is an answer the callers act on. Nor
is an object that is there already when ``create`` wants to write it: create
says so with False. Every other error of the store is raised as boto3 raised
it.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass

from botocore.exceptions import ClientError

from . import layout
from .clients import s3_client, ssm_client
from .config import READ, StoreConfig, load_store_config

ABSENT = ("NoSuchKey", "404")
TAKEN = ("PreconditionFailed",)


@dataclass(frozen=True)
class Listed:
    """An object a listing names: its key, and when it was last written."""

    key: str
    last_modified: datetime.datetime


def error_code(error: ClientError) -> str | None:
    return error.response.get("Error", {}).get("Code")


@dataclass(frozen=True)
class Store:
    """A bucket, the client that reaches it, and the key prefix the destination names, which
    ``layout`` puts before every key."""

    client: object
    bucket: str
    prefix: str = ""

    def get(self, key: str) -> bytes | None:
        """The body of the object at key, or None when the bucket has none there."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=key)
            return response["Body"].read()
        except ClientError as error:
            if error_code(error) in ABSENT:
                return None
            raise

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType=content_type)

    def create(self, key: str, body: bytes, content_type: str) -> bool:
        """Put body at key only when the bucket holds no object there; whether this
        call created it. The store decides (If-None-Match: *), so of two writers
        the first wins and the second learns it, and nothing is overwritten."""
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                IfNoneMatch="*",
            )
            return True
        except ClientError as error:
            if error_code(error) in TAKEN:
                return False
            raise

    def list(self, prefix: str) -> list[Listed]:
        """Every object directly under ``prefix``, which ends with "/", in key order.

        The delimiter "/" leaves out what lies deeper, so a listing of the latest
        files of one platform holds those files and no branch whose name goes on
        below it."""
        pages = self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix, Delimiter="/"
        )
        return [
            Listed(item["Key"], item["LastModified"])
            for page in pages
            for item in page.get("Contents", [])
        ]


def destination_store(cfg: StoreConfig) -> Store:
    """The store of the destination ``cfg`` names."""
    return Store(s3_client(cfg), *layout.parse_destination(cfg.destination))


@dataclass(frozen=True)
class Opened:
    """The destination as a job opens it: its configuration, its store, and the SSM client the
    configuration was read with."""

    cfg: StoreConfig
    store: Store
    ssm: object


def open_destination(access: str = READ) -> Opened:
    """Open the destination a job's configuration names, for a caller that does ``access``
    (``config.READ`` or ``config.WRITE``), which decides the R2 key pair."""
    ssm = ssm_client()
    cfg = load_store_config(ssm, access)
    return Opened(cfg, destination_store(cfg), ssm)
