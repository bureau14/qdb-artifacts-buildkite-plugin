"""What differs between the s3 and r2 backends on the wire, and what does not."""

import urllib.parse

import pytest

PREFIX = "widget-build/refs/heads/master/variants/linux/builds/build-7"


def round_trip(job, hook):
    """Upload and promote in one job, then download the promoted build in another."""
    job.write("artifacts/widget.bin", b"widget")
    builder = job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(
        {
            "upload": {"variant": "linux", "git_ref": "refs/heads/master", "files": "artifacts/*"},
            "promote": {"variant": "linux", "git_ref": "refs/heads/master"},
        }
    )
    consumer = job.with_env(BUILDKITE_PIPELINE_SLUG="consumer").with_plugin(
        {
            "download": {
                "projects": [
                    {
                        "project_id": "widget-build",
                        "variant": "linux",
                        "git_ref": "refs/heads/master",
                        "files": ["artifacts/*"],
                        "output-dir": "in",
                    }
                ]
            }
        }
    )
    return hook("post-command", builder), hook("pre-command", consumer)


def expected_signature(job, backend):
    if backend == "r2":
        return job.env["ARTIFACTS_R2_ACCESS_KEY_ID"], "auto"
    return job.env["AWS_ACCESS_KEY_ID"], job.env["AWS_DEFAULT_REGION"]


@pytest.mark.parametrize("backend", ["s3", "r2"])
def test_both_backends_store_and_find_artifacts_under_the_same_keys(job, store, hook, backend):
    uploaded, downloaded = round_trip(job, hook)

    assert uploaded.code == 0, uploaded.output
    assert downloaded.code == 0, downloaded.output
    assert store.stored_keys() == [
        "widget-build/refs/heads/master/variants/linux/LATEST_SUCCESSFUL",
        f"{PREFIX}/artifacts/widget.bin",
        f"{PREFIX}/manifest.json",
    ]
    assert (job.workspace / "in" / "artifacts" / "widget.bin").read_bytes() == b"widget"
    assert f"backend={backend}" in uploaded.stderr
    assert f"backend={backend}" in downloaded.stderr


@pytest.mark.parametrize("backend", ["s3", "r2"])
def test_each_backend_signs_with_its_own_credentials_and_region(job, store, hook, backend):
    round_trip(job, hook)

    signatures = {(r.access_key_id, r.region) for r in store.plugin_requests("s3")}
    assert signatures == {expected_signature(job, backend)}


@pytest.mark.parametrize("backend", ["r2"])
def test_r2_addresses_the_bucket_in_the_path_of_an_endpoint_with_a_host_name(
    job, store, hook, instant_clock, backend
):
    # With an IP address as endpoint botocore puts the bucket in the path whatever
    # the client asks for, so only a host name shows the plugin's own choice. The
    # s3 backend leaves that choice to botocore and is not pinned here.
    named = f"http://localhost:{urllib.parse.urlsplit(store.endpoint).port}"
    job = job.with_env(ARTIFACTS_ENDPOINT_URL=named, **instant_clock.env)

    uploaded, downloaded = round_trip(job, hook)

    assert uploaded.code == 0, uploaded.output
    assert downloaded.code == 0, downloaded.output
    requests = store.plugin_requests("s3")
    assert {r.host for r in requests} == {urllib.parse.urlsplit(named).netloc}
    assert {r.path.split("/")[1] for r in requests} == {store.bucket}


@pytest.mark.parametrize("backend", ["r2"])
def test_r2_ignores_the_credentials_of_the_aws_default_chain(job, store, hook, backend):
    uploaded, _ = round_trip(job.with_env(AWS_ACCESS_KEY_ID=None, AWS_SECRET_ACCESS_KEY=None), hook)

    assert uploaded.code == 0, uploaded.output
    assert {r.access_key_id for r in store.plugin_requests("s3")} == {
        job.env["ARTIFACTS_R2_ACCESS_KEY_ID"]
    }
