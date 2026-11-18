"""The options promote.snapshot and upload.snapshot: a snapshot key, written by
the plan step, or nothing. Any other value fails the step and names the option."""

import pytest

KEY = "v1-0123456789abcdef0123456789abcdef"
POINTER = "widget-build/refs/heads/master/variants/linux/LATEST_SUCCESSFUL"

NOT_KEYS = [
    "v1-0123456789abcdef",
    "v1-0123456789abcdef0123456789abcde",
    "v1-0123456789abcdef0123456789abcdef0",
    "v1-0123456789ABCDEF0123456789ABCDEF",
    "v2-0123456789abcdef0123456789abcdef",
    f" {KEY}",
    f"{KEY}\n",
    "LATEST_SUCCESSFUL",
]


def promote_step(**promote):
    return {"promote": {"variant": "linux", "git_ref": "refs/heads/master", **promote}}


def upload_step(**upload):
    return {
        "upload": {"variant": "linux", "git_ref": "refs/heads/master", "files": "a.bin", **upload}
    }


@pytest.mark.parametrize("value", NOT_KEYS)
def test_a_promote_snapshot_that_is_not_a_key_fails_the_step_before_any_request(
    job, store, hook, value
):
    outcome = hook("post-command", job.with_plugin(promote_step(snapshot=value)))

    assert outcome.code == 1
    assert "promote.snapshot" in outcome.error and repr(value) in outcome.error
    assert store.requests == []


@pytest.mark.parametrize("value", NOT_KEYS)
def test_an_upload_snapshot_that_is_not_a_key_fails_the_step_before_any_request(
    job, store, hook, agent, value
):
    job.write("a.bin", b"a")

    outcome = hook("post-command", job.with_plugin(upload_step(snapshot=value)))

    assert outcome.code == 1
    assert "upload.snapshot" in outcome.error and repr(value) in outcome.error
    assert store.requests == []
    assert agent.calls() == []


def test_an_empty_snapshot_option_is_no_snapshot_key(job, store, hook):
    job.write("a.bin", b"a")
    config = {**upload_step(snapshot=""), **promote_step(snapshot="")}

    outcome = hook("post-command", job.with_plugin(config))

    assert outcome.code == 0, outcome.output
    assert POINTER in store.stored_keys()
    assert not [k for k in store.stored_keys() if k.startswith("_snapshot/")]


def test_the_promote_snapshot_of_a_failed_command_is_not_read(job, store, hook):
    job = job.with_env(BUILDKITE_COMMAND_EXIT_STATUS="1")

    outcome = hook("post-command", job.with_plugin(promote_step(snapshot="not a key")))

    assert outcome.code == 0, outcome.output
    assert store.requests == []
