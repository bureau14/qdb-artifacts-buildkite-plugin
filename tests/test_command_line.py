"""The command line the hooks call: commands, arguments, defaults and exit codes."""

import pytest

PREFIX = "widget-build/refs/heads/master/variants/linux/builds/build-7"


def upload_options(job, **extra):
    return job.with_env(BUILDKITE_BUILD_ID="build-7").with_options(
        UPLOAD_VARIANT="linux", UPLOAD_GIT_REF="refs/heads/master", **extra
    )


def test_an_unknown_command_fails(job, store, cli):
    outcome = cli(job, "frobnicate")

    assert outcome.code == 1
    assert "frobnicate" in outcome.stderr


def test_no_command_fails_with_a_traceback(job, store, cli):
    outcome = cli(job)

    assert outcome.code == 1
    assert "IndexError" in outcome.stderr


def test_upload_without_files_option_takes_tar_zst_files_of_the_checkout(job, store, cli):
    job.write("widget.tar.zst", b"w")
    job.write("widget.zip", b"z")
    job.write("sub/deeper.tar.zst", b"d")

    outcome = cli(upload_options(job), "upload")

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == [f"{PREFIX}/manifest.json", f"{PREFIX}/widget.tar.zst"]


def test_upload_accepts_only_the_report_empty_argument(job, store, cli):
    job.write("a.bin", b"a")

    outcome = cli(upload_options(job, UPLOAD_FILES="a.bin"), "upload", "--force")

    assert outcome.code == 1
    assert "--force" in outcome.stderr
    assert store.requests == []


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

    outcome = cli(upload_options(job, UPLOAD_FILES="a.bin").with_env(**removed), "upload")

    assert outcome.code == 1
    for fact in facts:
        assert fact in outcome.stderr
    assert store.requests == []


@pytest.mark.parametrize("args, code", [([], 0), (["--report-empty"], 3)])
def test_an_allowed_empty_upload_exits_3_only_when_asked_to_report_it(job, store, cli, args, code):
    job = upload_options(job, UPLOAD_FILES="missing/*", UPLOAD_ALLOW_EMPTY="true")

    outcome = cli(job, "upload", *args)

    assert outcome.code == code, outcome.output
    assert store.requests == []


def test_a_non_empty_upload_exits_0_even_when_asked_to_report_empty(job, store, cli):
    job.write("a.bin", b"a")
    job = upload_options(job, UPLOAD_FILES="a.bin", UPLOAD_ALLOW_EMPTY="true")

    outcome = cli(job, "upload", "--report-empty")

    assert outcome.code == 0, outcome.output
    assert f"{PREFIX}/a.bin" in store.stored_keys()


def test_download_without_projects_fails(job, store, cli):
    outcome = cli(job.with_options(DOWNLOAD_CLEAN="true"), "download")

    assert outcome.code == 1
    assert "no download projects configured" in outcome.stderr


def test_download_refuses_a_project_without_git_ref(job, store, cli):
    outcome = cli(job.with_options(DOWNLOAD_PROJECTS_0_VARIANT="linux"), "download")

    assert outcome.code == 1
    assert "git_ref" in outcome.stderr
    assert store.requests == []


def test_download_without_project_id_or_pipeline_slug_fails(job, store, cli):
    job = job.with_env(BUILDKITE_PIPELINE_SLUG=None).with_options(
        DOWNLOAD_PROJECTS_0_VARIANT="linux", DOWNLOAD_PROJECTS_0_GIT_REF="refs/heads/master"
    )

    outcome = cli(job, "download")

    assert outcome.code == 1
    assert "project_id" in outcome.stderr and "BUILDKITE_PIPELINE_SLUG" in outcome.stderr
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

    outcome = cli(job, "download")

    assert outcome.code == 1
    assert "ValueError" in outcome.stderr
    assert store.requests == []
