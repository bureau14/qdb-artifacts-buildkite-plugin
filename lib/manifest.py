"""manifest.json of a build: what an upload wrote, as compact JSON.

An upload writes it last, after every file of the build has arrived, so a build
whose manifest exists holds the whole upload. A download reads it for the
build's log lines only; a caller outside the plugin reads it through
read_manifest.
"""

import json

from botocore.exceptions import ClientError

from . import layout
from .clients import s3_client


def upload_manifest(bucket, prefix, manifest_payload, cfg, auth):
    manifest_key = layout.manifest(prefix)
    s3_client(cfg, auth).put_object(
        Bucket=bucket,
        Key=manifest_key,
        Body=json.dumps(manifest_payload, separators=(",", ":")).encode("utf-8"),
        ContentType="application/json",
    )


def read_manifest_at(s3, bucket, prefix):
    """Best-effort fetch of <prefix>/manifest.json. Returns dict or None on any
    error -- this is purely informational and must never break the download."""
    try:
        response = s3.get_object(Bucket=bucket, Key=layout.manifest(prefix))
        return json.loads(response["Body"].read())
    except (ClientError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def read_manifest(cfg, auth, bucket, resolved):
    """The manifest.json of a resolved build as a dict, or None when there is none
    that reads as a JSON object: missing, refused by the store, not JSON, or not
    an object.

    It reads the manifest the way a download does. A caller must expect None: an
    upload that failed before its end leaves its files without a manifest.
    """
    manifest = read_manifest_at(s3_client(cfg, auth), bucket, resolved.prefix)
    return manifest if isinstance(manifest, dict) else None
