"""Use the library the way a program outside Buildkite does, as the README shows.

Such a caller vendors the plugin as ``vendor/qdb_artifacts`` in its own tree and
imports through that path, reads the store's configuration from the environment
and SSM with a client of its own, downloads one project's artifacts with keyword
arguments and strict ref matching, and reads the manifest of the build it got.

Usage: python -I outside_caller.py <directory holding vendor/> <request JSON> <output dir>

The request holds ``project_id``, ``build_id``, ``git_ref``, ``variant``,
``patterns``, ``exclude_patterns`` and ``extract``. Prints the build downloaded
and the file count of its manifest, as JSON.
"""

import json
import sys


def download(tree, request, output_dir):
    sys.path.insert(0, tree)
    import boto3
    from vendor.qdb_artifacts.lib import (
        download_artifacts,
        load_store_config,
        read_manifest,
        resolve_object_auth,
    )

    ssm = boto3.client("ssm")
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-only")
    bucket, resolved, _ = download_artifacts(
        cfg=cfg, auth=auth, output_dir=output_dir, require_strict_ref_match=True, **request
    )
    manifest = read_manifest(cfg, auth, bucket, resolved)
    return {"build_id": resolved.build_id, "file_count": manifest["file_count"]}


if __name__ == "__main__":
    print(json.dumps(download(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3])))
