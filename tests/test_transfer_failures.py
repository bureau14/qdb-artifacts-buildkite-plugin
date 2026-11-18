"""What an upload that fails or is interrupted leaves in the store, and how the hook ends."""

import os

import pytest

PREFIX = "widget-build/refs/heads/master/variants/linux/builds/build-7"
RETRY_SLEEPS = [5, 10, 20, 40, 60, 60, 60, 60, 60]


def build_and_promote(job, files):
    return job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(
        {
            "upload": {"variant": "linux", "git_ref": "refs/heads/master", "files": files},
            "promote": {"variant": "linux", "git_ref": "refs/heads/master"},
        }
    )


def puts_of(store, key):
    return [r for r in store.plugin_requests("s3") if r.method == "PUT" and r.key == key]


def test_an_artifact_the_store_refuses_fails_the_upload_after_ten_attempts(
    job, store, hook, agent, instant_clock
):
    job.write("a.bin", b"a")
    job.write("b.bin", b"b")
    store.refuse("PUT", f"{PREFIX}/a.bin", 403, "AccessDenied")

    outcome = hook("post-command", build_and_promote(job, "*.bin").with_env(**instant_clock.env))

    assert outcome.code == 1
    assert "AccessDenied" in outcome.stderr
    assert len(puts_of(store, f"{PREFIX}/a.bin")) == 10
    assert instant_clock.sleeps() == RETRY_SLEEPS
    assert store.stored_keys() == [f"{PREFIX}/b.bin"]
    assert agent.calls() == []


def test_a_manifest_the_store_refuses_fails_the_upload_before_any_annotation(
    job, store, hook, agent
):
    job.write("a.bin", b"a")
    store.refuse("PUT", f"{PREFIX}/manifest.json", 403, "AccessDenied")

    outcome = hook("post-command", build_and_promote(job, "a.bin"))

    assert outcome.code == 1
    assert "AccessDenied" in outcome.stderr
    assert len(puts_of(store, f"{PREFIX}/manifest.json")) == 1
    assert store.stored_keys() == [f"{PREFIX}/a.bin"]
    assert agent.calls() == []


@pytest.mark.skipif(os.name == "nt", reason="Ctrl-C is sent as SIGINT to a process group")
def test_an_upload_interrupted_by_ctrl_c_ends_the_hook_with_130_and_promotes_nothing(
    job, store, interrupted_hook
):
    job.write("a.bin", b"a")
    transfer = store.hold("PUT", f"{PREFIX}/a.bin")

    outcome = interrupted_hook("post-command", build_and_promote(job, "a.bin"), transfer.arrived)

    assert outcome.code == 130, outcome.output
    assert store.stored_keys() == []
