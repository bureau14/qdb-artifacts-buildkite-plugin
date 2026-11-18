"""Use the library the way a caller outside the plugin does.

Such a caller vendors the plugin as vendor/qdb_artifacts inside its own tree (the
layout the README shows), imports through that nested namespace path, reads the
store configuration with an SSM client of its own, calls with keyword arguments
only and strict ref matching, and reads only bucket, build_id, key and rel of the
result.

Usage: python -I -B outside_caller.py <directory holding vendor/> <request JSON>
Prints the parts of the result the caller uses as one JSON object.
"""

import json
import sys
import types


def import_vendored_library(namespace_root):
    """The caller's own imports, once the root of its tree is on sys.path."""
    sys.path.insert(0, namespace_root)
    from vendor.qdb_artifacts.lib import (
        ObjectAuth,
        StoreConfig,
        load_store_config,
        resolve_object_auth,
    )
    from vendor.qdb_artifacts.lib import resolve_artifacts as resolve_project_artifacts

    return types.SimpleNamespace(
        ObjectAuth=ObjectAuth,
        StoreConfig=StoreConfig,
        load_store_config=load_store_config,
        resolve_object_auth=resolve_object_auth,
        resolve_project_artifacts=resolve_project_artifacts,
    )


def resolve_for_caller(namespace_root, request):
    import boto3

    lib = import_vendored_library(namespace_root)
    ssm = boto3.client("ssm")
    cfg = lib.load_store_config(ssm)
    auth = lib.resolve_object_auth(ssm, cfg, permission="object-read-only")
    bucket, resolved, objects = lib.resolve_project_artifacts(
        cfg=cfg,
        auth=auth,
        project_id=request["project_id"],
        build_id=request["build_id"],
        git_ref=request["git_ref"],
        variant=request["variant"],
        patterns=request["patterns"],
        require_strict_ref_match=True,
    )
    return {
        "package_file": sys.modules["vendor.qdb_artifacts.lib"].__file__,
        "bucket": bucket,
        "build_id": resolved.build_id,
        "artifacts": [{"key": o.key, "filename": o.rel.rsplit("/", 1)[-1]} for o in objects],
        "cfg": {"backend": cfg.backend, "destination": cfg.destination},
        "endpoint_url": cfg.endpoint_url,
        "auth": [auth.access_key_id, auth.secret_access_key],
        "types": [isinstance(cfg, lib.StoreConfig), isinstance(auth, lib.ObjectAuth)],
    }


if __name__ == "__main__":
    print(json.dumps(resolve_for_caller(sys.argv[1], json.loads(sys.argv[2]))))
