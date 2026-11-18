"""An upload in a build with a snapshot key: its manifest also says where the files
were made (source) and which builds the job downloaded by its own download
options (downloads). Without a key the manifest has the five fields every upload
writes and nothing else."""

import json

import pytest

KEY = "v1-0123456789abcdef0123456789abcdef"
PREFIX = "widget-build/refs/heads/master/variants/linux/builds/01900000-0000-7000-8000-000000000001"
MANIFEST = f"{PREFIX}/manifest.json"
ENGINE_BUILD = "01900000-0000-7000-8000-0000000000e1"
TESTS_BUILD = "01900000-0000-7000-8000-0000000000e2"
FIVE_FIELDS = ["file_count", "files", "uploaded_at", "total_size_bytes", "base_dir"]


def upload_block(**upload):
    return {"variant": "linux", "git_ref": "refs/heads/master", "files": "a.bin", **upload}


def download_project(**project):
    return {"variant": "linux", "git_ref": "refs/heads/master", "files": ["*.tar.zst"], **project}


DOWNLOADS = {
    "projects": [
        download_project(project_id="engine", build_id=ENGINE_BUILD),
        download_project(variant="linux-tests", build_id=TESTS_BUILD),
        download_project(project_id="gadget", build_id="LATEST_SUCCESSFUL"),
        download_project(project_id="gizmo"),
    ]
}


def upload(job, hook, upload_options, downloads=None):
    job.write("a.bin", b"a")
    config = {"upload": upload_options}
    if downloads is not None:
        config["download"] = downloads
    return hook("post-command", job.with_plugin(config))


def manifest_of(store):
    body = store.get(MANIFEST)
    return body, json.loads(body)


def test_the_manifest_gains_source_and_downloads_after_the_five_fields(job, store, hook):
    outcome = upload(job, hook, upload_block(snapshot=KEY), DOWNLOADS)

    assert outcome.code == 0, outcome.output
    body, manifest = manifest_of(store)
    assert list(manifest) == [*FIVE_FIELDS, "source", "downloads"]
    assert manifest["source"] == {
        "schema": 1,
        "repository": job.env["BUILDKITE_REPO"],
        "commit": job.env["FAKE_GIT_HEAD"],
        "pipeline": "widget-build",
        "build_number": 512,
        "build_id": job.env["BUILDKITE_BUILD_ID"],
        "job_id": job.env["BUILDKITE_JOB_ID"],
        "snapshot": KEY,
    }
    assert manifest["downloads"] == [
        {
            "project_id": "engine",
            "variant": "linux",
            "git_ref": "refs/heads/master",
            "build_id": ENGINE_BUILD,
        },
        {
            "project_id": "widget-build",
            "variant": "linux-tests",
            "git_ref": "refs/heads/master",
            "build_id": TESTS_BUILD,
        },
    ]
    assert body == json.dumps(manifest, separators=(",", ":")).encode()


def test_without_a_key_the_manifest_has_the_five_fields_alone_and_git_is_not_asked(
    job, store, hook, git
):
    outcome = upload(job, hook, upload_block(), DOWNLOADS)

    assert outcome.code == 0, outcome.output
    body, manifest = manifest_of(store)
    five_fields = (
        '{"file_count":1,"files":["a.bin"],'
        f'"uploaded_at":"{manifest["uploaded_at"]}","total_size_bytes":1,"base_dir":null}}'
    )
    assert body == five_fields.encode()
    assert git.calls() == []


@pytest.mark.parametrize(
    "repository, recorded",
    [
        (
            "https://x-access-token:not-a-real-token@git.example.test/example/widget.git",
            "https://git.example.test/example/widget.git",
        ),
        (
            "https://not-a-real-token@git.example.test/example/widget.git",
            "https://git.example.test/example/widget.git",
        ),
        (
            "ssh://git@git.example.test:2222/example/widget.git",
            "ssh://git.example.test:2222/example/widget.git",
        ),
        ("git@git.example.test:example/widget.git", "git@git.example.test:example/widget.git"),
        (
            "https://git.example.test/example/widget.git",
            "https://git.example.test/example/widget.git",
        ),
    ],
    ids=["url-user-and-password", "url-token-as-user", "ssh-url-user", "scp-form", "url"],
)
def test_the_source_names_the_repository_without_the_user_of_its_url(
    job, store, hook, repository, recorded
):
    outcome = upload(job.with_env(BUILDKITE_REPO=repository), hook, upload_block(snapshot=KEY))

    assert outcome.code == 0, outcome.output
    body, manifest = manifest_of(store)
    assert manifest["source"]["repository"] == recorded
    assert b"not-a-real-token" not in body


def test_a_job_that_downloads_no_named_build_records_no_downloads(job, store, hook):
    downloads = {"projects": [download_project(project_id="gadget")]}

    assert upload(job, hook, upload_block(snapshot=KEY), downloads).code == 0

    assert manifest_of(store)[1]["downloads"] == []


def test_a_job_without_download_options_records_no_downloads(job, store, hook):
    assert upload(job, hook, upload_block(snapshot=KEY)).code == 0

    assert manifest_of(store)[1]["downloads"] == []


@pytest.mark.parametrize(
    "build_commit",
    ["HEAD", "0123456789abcdef0123456789abcdef01234567"],
    ids=["build-of-a-branch", "checkout-moved-off-the-builds-commit"],
)
def test_the_commit_of_the_source_is_the_head_of_the_jobs_checkout(
    job, store, hook, git, build_commit
):
    job = job.with_env(
        FAKE_GIT_HEAD="fedcba9876543210fedcba9876543210fedcba98", BUILDKITE_COMMIT=build_commit
    )

    assert upload(job, hook, upload_block(snapshot=KEY)).code == 0

    assert manifest_of(store)[1]["source"]["commit"] == "fedcba9876543210fedcba9876543210fedcba98"
    (call,) = git.calls()
    assert (call.argv, call.cwd) == (["rev-parse", "HEAD"], str(job.workspace.resolve()))


def test_a_build_id_option_of_another_build_fails_an_upload_with_a_key_before_any_request(
    job, store, hook, agent
):
    build = "01900000-0000-7000-8000-0000000000b0"

    outcome = upload(job, hook, upload_block(snapshot=KEY, build_id=build))

    assert outcome.code == 1
    assert "upload.build_id" in outcome.error and build in outcome.error
    assert store.requests == []
    assert agent.calls() == []


def test_a_build_id_option_that_names_the_jobs_own_build_is_accepted_with_a_key(job, store, hook):
    build = job.env["BUILDKITE_BUILD_ID"]

    assert upload(job, hook, upload_block(snapshot=KEY, build_id=build)).code == 0

    assert manifest_of(store)[1]["source"]["build_id"] == build


def test_the_upload_of_a_failed_command_records_its_source_too(job, store, hook):
    job = job.with_env(BUILDKITE_COMMAND_EXIT_STATUS="2")

    assert upload(job, hook, upload_block(snapshot=KEY)).code == 0

    assert manifest_of(store)[1]["source"]["snapshot"] == KEY


@pytest.mark.parametrize(
    "files, exclude",
    [("missing/*", []), ("a.bin", ["*.bin"])],
    ids=["nothing-matched", "all-excluded"],
)
def test_an_allowed_empty_upload_with_a_key_asks_git_nothing_and_needs_no_checkout(
    job, store, hook, git, files, exclude
):
    block = upload_block(snapshot=KEY, files=files, exclude=exclude, allow_empty=True)

    outcome = upload(job.with_env(FAKE_GIT_HEAD=None), hook, block)

    assert outcome.code == 0, outcome.output
    assert store.requests == []
    assert git.calls() == []


@pytest.mark.parametrize(
    "env, named",
    [
        ({"FAKE_GIT_HEAD": None}, "git rev-parse HEAD"),
        ({"BUILDKITE_REPO": None}, "BUILDKITE_REPO"),
        ({"BUILDKITE_JOB_ID": None}, "BUILDKITE_JOB_ID"),
        ({"BUILDKITE_PIPELINE_SLUG": None}, "BUILDKITE_PIPELINE_SLUG"),
        ({"BUILDKITE_BUILD_NUMBER": "five"}, "BUILDKITE_BUILD_NUMBER"),
    ],
    ids=["no-checkout", "no-repository", "no-job-id", "no-pipeline", "build-number-not-a-number"],
)
def test_a_source_that_cannot_be_told_fails_the_upload_before_any_request(
    job, store, hook, agent, env, named
):
    block = upload_block(snapshot=KEY, project_id="widget-build")

    outcome = upload(job.with_env(**env), hook, block)

    assert outcome.code == 1
    assert named in outcome.error
    assert store.requests == []
    assert agent.calls() == []
