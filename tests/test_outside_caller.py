"""The library as its outside caller uses it: vendored at thirdparty/qdb_artifacts,
imported through that namespace path by a fresh interpreter with a clean sys.path."""

import json
from pathlib import PurePath

import pytest

MASTER = "refs/heads/master"
PREFIX = f"widget-build/{MASTER}/variants/linux-aarch64-release/builds/b-5"
REQUEST = {
    "project_id": "widget-build",
    "build_id": "LATEST_SUCCESSFUL",
    "git_ref": MASTER,
    "variant": "linux-aarch64-release",
    "patterns": ["artifacts/*-aarch64-server.tar.zst", "artifacts/*-aarch64-utils.tar.zst"],
}


def put_promoted_build(store, ref=MASTER):
    files = {
        "artifacts/widget-1.0-aarch64-server.tar.zst": b"server",
        "artifacts/widget-1.0-aarch64-utils.tar.zst": b"utils",
        "artifacts/widget-1.0-aarch64-c-api.tar.zst": b"c-api",
    }
    store.put_build("widget-build", ref, "linux-aarch64-release", "b-5", files)
    store.put_pointer("widget-build", ref, "linux-aarch64-release", "b-5")


@pytest.mark.parametrize("backend", ["s3", "r2"])
def test_the_vendored_import_resolves_the_promoted_build(outside_caller, store, backend):
    put_promoted_build(store)

    outcome = outside_caller(REQUEST)

    assert outcome.code == 0, outcome.output
    result = json.loads(outcome.stdout)
    assert PurePath(result["package_file"]).parts[-4:] == (
        "thirdparty",
        "qdb_artifacts",
        "lib",
        "__init__.py",
    )
    assert (result["bucket"], result["build_id"]) == (store.bucket, "b-5")
    assert result["artifacts"] == [
        {
            "key": f"{PREFIX}/artifacts/widget-1.0-aarch64-server.tar.zst",
            "filename": "widget-1.0-aarch64-server.tar.zst",
        },
        {
            "key": f"{PREFIX}/artifacts/widget-1.0-aarch64-utils.tar.zst",
            "filename": "widget-1.0-aarch64-utils.tar.zst",
        },
    ]
    assert result["cfg"] == {"backend": backend, "destination": f"s3://{store.bucket}"}
    assert result["types"] == [True, True]


@pytest.mark.parametrize("backend", ["r2"])
def test_the_caller_gets_the_r2_key_pair_and_endpoint_to_sign_its_own_urls(
    outside_caller, job, store, backend
):
    put_promoted_build(store)

    result = json.loads(outside_caller(REQUEST).stdout)

    assert result["auth"] == [
        job.env["ARTIFACTS_R2_ACCESS_KEY_ID"],
        job.env["ARTIFACTS_R2_SECRET_ACCESS_KEY"],
    ]
    assert result["endpoint_url"] == job.env["ARTIFACTS_ENDPOINT_URL"]


def test_a_build_missing_on_the_requested_branch_fails_the_caller(outside_caller, store):
    put_promoted_build(store, ref=MASTER)

    outcome = outside_caller({**REQUEST, "git_ref": "refs/heads/topic", "build_id": "b-5"})

    assert outcome.code == 1
    assert "refs/heads/topic" in outcome.error and "widget-build" in outcome.error
    assert outcome.stdout == ""
