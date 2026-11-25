"""Use the library the way a program outside Buildkite does, as the README shows.

Such a caller vendors the plugin as ``vendor/qdb_artifacts`` in its own tree and
imports through that path, reads the store's configuration from the environment
and SSM with a client of its own, downloads one step of a project's platform with
keyword arguments, in a branch with strict ref matching or at a key, and reads the
manifest of the step it got.

Usage: python -I outside_caller.py <directory holding vendor/> <request JSON> <output dir>

The request holds ``project_id``, ``variant``, ``step``, ``git_ref`` or ``key``,
``patterns``, ``exclude_patterns`` and ``extract``. Prints the key downloaded at and
the file count of its manifest, as JSON.
"""

import json
import sys


def download(tree, request, output_dir):
    sys.path.insert(0, tree)
    import boto3
    from vendor.qdb_artifacts.lib import (
        Branch,
        Key,
        download_artifacts,
        load_store_config,
        read_manifest,
        resolve_artifacts,
    )

    ssm = boto3.client("ssm")
    cfg = load_store_config(ssm)
    asked = dict(request)
    key, git_ref = asked.pop("key", None), asked.pop("git_ref", None)
    build = Key(key) if key is not None else Branch(git_ref, strict=True)
    selection = {k: v for k, v in asked.items() if k != "extract"}
    _, found, _ = resolve_artifacts(cfg=cfg, build=build, **selection)
    _, resolved, _ = download_artifacts(cfg=cfg, build=build, output_dir=output_dir, **asked)
    assert resolved.key == found.key
    manifest = read_manifest(resolved)
    return {"key": resolved.key, "file_count": len(manifest["files"])}


if __name__ == "__main__":
    print(json.dumps(download(sys.argv[1], json.loads(sys.argv[2]), sys.argv[3])))
