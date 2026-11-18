"""The Buildkite annotation an upload creates through buildkite-agent."""

import os

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the fake agent is a POSIX script")

BUILD_URL = (
    "https://artifacts.example.test/widget-build/refs/heads/master/variants/linux/builds/build-7"
)


def upload(job, hook, files="artifacts/*", **options):
    config = {"upload": {"variant": "linux", "git_ref": "refs/heads/master", "files": files}}
    config["upload"].update(options)
    return hook("post-command", job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(config))


def test_one_annotate_call_lists_every_file_with_its_link_and_size(job, store, hook, agent):
    job.write("artifacts/b.tar.zst", b"b" * 1536)
    job.write("artifacts/a.tar.zst", b"a" * 3)

    outcome = upload(job, hook)

    assert outcome.code == 0, outcome.output
    body = (
        "## :hammer: Build widget\n"
        "### Uploaded artifacts (2 files, 1.5 KiB total):\n"
        "\n"
        f"- [artifacts/a.tar.zst]({BUILD_URL}/artifacts/a.tar.zst) (3.0 B)\n"
        f"- [artifacts/b.tar.zst]({BUILD_URL}/artifacts/b.tar.zst) (1.5 KiB)"
    )
    (call,) = agent.calls()
    assert call.argv == [
        "annotate",
        body,
        "--context",
        "artifacts",
        "--style",
        "info",
        "--priority",
        "10",
        "--scope",
        "job",
    ]
    assert call.stdin == ""


def test_the_job_log_shows_the_annotate_command_with_the_links(job, store, hook, agent):
    job.write("artifacts/a.tar.zst", b"a")

    outcome = upload(job, hook)

    assert "running command: buildkite-agent annotate" in outcome.stderr
    assert f"- [artifacts/a.tar.zst]({BUILD_URL}/artifacts/a.tar.zst)" in outcome.stderr


def test_links_follow_base_dir_and_the_destination_prefix(job, store, hook, agent):
    job.write("dist/a.tar.zst", b"a")
    job = job.with_env(ARTIFACTS_DESTINATION="s3://example-artifacts/ci")

    outcome = upload(job, hook, files="dist/*", base_dir="dist")

    assert outcome.code == 0, outcome.output
    (call,) = agent.calls()
    assert (
        "(https://artifacts.example.test/ci/widget-build/refs/heads/master/variants/linux/"
        "builds/build-7/a.tar.zst)" in call.argv[1]
    )


def test_the_heading_names_an_unknown_job_without_a_label(job, store, hook, agent):
    job.write("artifacts/a.tar.zst", b"a")

    upload(job.with_env(BUILDKITE_LABEL=None), hook)

    (call,) = agent.calls()
    assert call.argv[1].startswith("## unknown job\n")


@pytest.mark.parametrize(
    "env, options",
    [
        ({"BUILDKITE": None}, {}),
        ({"ARTIFACTS_DOMAIN": None}, {}),
        ({}, {"annotate": False}),
    ],
    ids=["not-in-buildkite", "no-domain", "annotate-false"],
)
def test_no_annotation_outside_buildkite_without_a_domain_or_when_turned_off(
    job, store, hook, agent, env, options
):
    job.write("artifacts/a.tar.zst", b"a")

    outcome = upload(job.with_env(**env), hook, **options)

    assert outcome.code == 0, outcome.output
    assert agent.calls() == []
    assert len(store.stored_keys()) == 2


def test_the_domain_can_come_from_ssm(job, store, hook, agent):
    store.put_parameter(
        "/services/buildkite/config/artifacts/object-store/r2/artifacts-domain", "ssm.example.test"
    )
    job.write("artifacts/a.tar.zst", b"a")

    upload(job.with_env(ARTIFACTS_DOMAIN=None), hook)

    (call,) = agent.calls()
    assert "(https://ssm.example.test/widget-build/" in call.argv[1]


def test_a_failing_agent_does_not_fail_the_upload_but_its_status_is_logged(job, store, hook, agent):
    job.write("artifacts/a.tar.zst", b"a")

    outcome = upload(job.with_env(FAKE_BUILDKITE_AGENT_EXIT_STATUS="47"), hook)

    assert outcome.code == 0, outcome.output
    assert len(agent.calls()) == 1
    assert len(store.stored_keys()) == 2
    # The agent's status reaches the log in Python's own words for a failed command.
    assert "exit status 47" in outcome.stderr


def test_a_missing_agent_does_not_fail_the_upload(job, store, hook, agent):
    job.write("artifacts/a.tar.zst", b"a")
    path = os.pathsep.join(d for d in job.env["PATH"].split(os.pathsep) if d != str(agent.bin_dir))

    outcome = upload(job.with_env(PATH=path), hook)

    assert outcome.code == 0, outcome.output
    assert len(store.stored_keys()) == 2
