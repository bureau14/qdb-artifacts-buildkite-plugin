"""Which manifest.json contents a download tolerates, and which end it."""

import json

import pytest

MASTER = "refs/heads/master"
PREFIX = f"engine/{MASTER}/variants/linux/builds/b-1"


def download_with_manifest(job, store, hook, manifest):
    store.put(f"{PREFIX}/a.bin", b"a")
    store.put(f"{PREFIX}/manifest.json", manifest)
    project = {
        "project_id": "engine",
        "build_id": "b-1",
        "variant": "linux",
        "git_ref": MASTER,
        "files": ["*.bin"],
        "output-dir": "out",
    }
    return hook("pre-command", job.with_plugin({"download": {"projects": [project]}}))


@pytest.mark.parametrize(
    "manifest",
    [
        b"not json",
        b"\xff\xfe",
        b"null",
        b"[]",
        b"{}",
        json.dumps({"uploaded_at": "2026-01-02T03:04:05Z", "file_count": 1}).encode(),
        json.dumps({"uploaded_at": "yesterday"}).encode(),
        json.dumps({"total_size_bytes": 10, "snapshot": {"key": "v1"}, "extra": [1]}).encode(),
    ],
    ids=[
        "invalid-json",
        "not-utf8",
        "null",
        "empty-list",
        "empty-object",
        "zulu-time",
        "unparsable-time",
        "unknown-fields",
    ],
)
def test_a_manifest_the_reader_tolerates_does_not_change_the_download(job, store, hook, manifest):
    outcome = download_with_manifest(job, store, hook, manifest)

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["a.bin"]


@pytest.mark.parametrize(
    "manifest, error",
    [
        (b"[1]", "AttributeError"),
        (json.dumps({"uploaded_at": 5}).encode(), "AttributeError"),
        (json.dumps({"total_size_bytes": None}).encode(), "TypeError"),
        (json.dumps({"total_size_bytes": "10"}).encode(), "TypeError"),
    ],
    ids=["non-empty-list", "numeric-time", "null-size", "string-size"],
)
def test_a_manifest_with_unexpected_types_ends_the_download_with_a_traceback(
    job, store, hook, manifest, error
):
    outcome = download_with_manifest(job, store, hook, manifest)

    assert outcome.code == 1
    assert error in outcome.stderr
    assert job.files() == []
