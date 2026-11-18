"""Small objects of the store, one request each: the text of a key, and whether a
prefix holds any object at all."""

from botocore.exceptions import ClientError


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
