"""The command line the hooks call: its phases, the options they read, defaults and exit codes."""

import pytest

PREFIX = "widget-build/refs/heads/master/variants/linux/builds/build-7"


def upload_options(job, **extra):
    return job.with_env(BUILDKITE_BUILD_ID="build-7").with_options(
        UPLOAD_VARIANT="linux", UPLOAD_GIT_REF="refs/heads/master", **extra
    )


def before_quoted_configuration(error):
    """The error up to the plugin configuration it may quote, which names every option."""
    return error.split("{", 1)[0]


def test_an_unknown_phase_fails(job, store, cli):
    outcome = cli(job, "frobnicate")

    assert outcome.code == 1
    assert "frobnicate" in outcome.error


@pytest.mark.parametrize(
    "removed, facts",
    [
        ({"BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_GIT_REF": None}, ["git_ref"]),
        ({"BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_VARIANT": None}, ["variant"]),
        ({"BUILDKITE_PIPELINE_SLUG": None}, ["project_id", "BUILDKITE_PIPELINE_SLUG"]),
        ({"BUILDKITE_BUILD_ID": None}, ["BUILDKITE_BUILD_ID", "build_id"]),
    ],
    ids=["no-git-ref", "no-variant", "no-project", "no-build"],
)
def test_upload_refuses_an_incomplete_configuration(job, store, cli, removed, facts):
    job.write("a.bin", b"a")

    outcome = cli(upload_options(job, UPLOAD_FILES="a.bin").with_env(**removed), "post-command")

    assert outcome.code == 1
    for fact in facts:
        assert fact in before_quoted_configuration(outcome.error)
    assert store.requests == []


def test_a_non_empty_upload_allowed_to_be_empty_is_uploaded_annotated_and_promoted(
    job, store, cli, agent
):
    job.write("a.bin", b"a")
    job = upload_options(
        job,
        UPLOAD_FILES="a.bin",
        UPLOAD_ALLOW_EMPTY="true",
        PROMOTE_VARIANT="linux",
        PROMOTE_GIT_REF="refs/heads/master",
    )

    outcome = cli(job, "post-command")

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == [
        "widget-build/refs/heads/master/variants/linux/LATEST_SUCCESSFUL",
        f"{PREFIX}/a.bin",
        f"{PREFIX}/manifest.json",
    ]
    assert [call.argv[0] for call in agent.calls()] == ["annotate"]


def test_the_pre_command_phase_without_download_projects_fails(job, store, cli):
    outcome = cli(job.with_options(DOWNLOAD_CLEAN="true"), "pre-command")

    assert outcome.code == 1
    assert "projects" in outcome.error


def test_download_refuses_a_project_without_git_ref(job, store, cli):
    outcome = cli(job.with_options(DOWNLOAD_PROJECTS_0_VARIANT="linux"), "pre-command")

    assert outcome.code == 1
    assert "git_ref" in before_quoted_configuration(outcome.error)
    assert store.requests == []


def test_download_without_project_id_or_pipeline_slug_fails(job, store, cli):
    job = job.with_env(BUILDKITE_PIPELINE_SLUG=None).with_options(
        DOWNLOAD_PROJECTS_0_VARIANT="linux", DOWNLOAD_PROJECTS_0_GIT_REF="refs/heads/master"
    )

    outcome = cli(job, "pre-command")

    assert outcome.code == 1
    assert "project_id" in outcome.error and "BUILDKITE_PIPELINE_SLUG" in outcome.error
    assert store.requests == []


@pytest.mark.parametrize("option", ["DOWNLOAD_PARALLEL", "DOWNLOAD_CONCURRENCY"])
def test_a_download_transfer_option_that_is_not_a_number_fails_with_a_traceback(
    job, store, cli, option
):
    job = job.with_options(
        **{option: "many"},
        DOWNLOAD_PROJECTS_0_VARIANT="linux",
        DOWNLOAD_PROJECTS_0_GIT_REF="refs/heads/master",
    )

    outcome = cli(job, "pre-command")

    assert outcome.code == 1
    assert "ValueError" in outcome.stderr
    assert store.requests == []
