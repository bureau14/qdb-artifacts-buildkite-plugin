"""Which objects of a resolved build a download takes, and where it writes them."""

import pytest

MASTER = "refs/heads/master"
PREFIX = f"engine/{MASTER}/variants/linux/builds/b-1"


def project(**options):
    return {
        "project_id": "engine",
        "build_id": "b-1",
        "variant": "linux",
        "git_ref": MASTER,
        **options,
    }


def download(job, hook, *projects, **download_options):
    config = {"download": {**download_options, "projects": list(projects)}}
    return hook("pre-command", job.with_plugin(config))


def put_objects(store, rels):
    for rel in rels:
        store.put(f"{PREFIX}/{rel}", rel.encode())


def test_without_projects_the_hook_does_nothing(job, store, hook):
    outcome = hook("pre-command", job.with_plugin({"upload": {"files": "*"}}))

    assert outcome.code == 0, outcome.output
    assert outcome.stdout == "" and outcome.stderr == ""
    assert store.requests == []


def test_a_project_list_that_does_not_start_at_index_0_is_ignored(job, store, hook):
    job = job.with_options(DOWNLOAD_PROJECTS_1_VARIANT="linux", DOWNLOAD_PROJECTS_1_GIT_REF=MASTER)

    outcome = hook("pre-command", job)

    assert outcome.code == 0, outcome.output
    assert store.requests == []


def test_the_project_list_ends_at_the_first_index_without_variant(job, store, hook):
    put_objects(store, ["a.bin"])
    job = job.with_options(
        DOWNLOAD_PROJECTS_0_PROJECT_ID="engine",
        DOWNLOAD_PROJECTS_0_BUILD_ID="b-1",
        DOWNLOAD_PROJECTS_0_VARIANT="linux",
        DOWNLOAD_PROJECTS_0_GIT_REF=MASTER,
        DOWNLOAD_PROJECTS_0_FILES_0="*.bin",
        DOWNLOAD_PROJECTS_0_OUTPUT_DIR="first",
        DOWNLOAD_PROJECTS_2_PROJECT_ID="missing-project",
        DOWNLOAD_PROJECTS_2_VARIANT="linux",
        DOWNLOAD_PROJECTS_2_GIT_REF=MASTER,
        DOWNLOAD_PROJECTS_2_OUTPUT_DIR="third",
    )

    outcome = hook("pre-command", job)

    assert outcome.code == 0, outcome.output
    assert job.files() == ["first/a.bin"]


def test_only_top_manifest_json_and_the_bare_prefix_key_are_never_artifacts(job, store, hook):
    put_objects(store, ["", "a.bin", "manifest.json", "sub/manifest.json", "notes.txt", "dir/"])

    outcome = download(job, hook, project(files=["*"], **{"output-dir": "out"}))

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["a.bin", "dir", "notes.txt", "sub/manifest.json"]
    assert (job.workspace / "out" / "dir").read_bytes() == b"dir/"


def test_a_glob_star_matches_across_directories_and_a_name_is_anchored_at_the_build(
    job, store, hook
):
    put_objects(
        store,
        [
            "artifacts/widget-server.tar.zst",
            "artifacts/widget-utils.tar.zst",
            "widget-docs.tar.zst",
        ],
    )

    outcome = download(
        job, hook, project(files=["*-server.tar.zst", "widget-*.tar.zst"], **{"output-dir": "o"})
    )

    assert outcome.code == 0, outcome.output
    assert job.files("o") == ["artifacts/widget-server.tar.zst", "widget-docs.tar.zst"]


def test_files_default_to_tar_zst_archives(job, store, hook):
    put_objects(store, ["a.tar.zst", "deep/b.tar.zst", "c.zip"])

    outcome = download(job, hook, project(**{"output-dir": "out"}))

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["a.tar.zst", "deep/b.tar.zst"]


def test_files_arrive_as_a_list_or_as_one_string(job, store, hook):
    put_objects(store, ["a.bin", "b.zip"])
    job = job.with_options(
        DOWNLOAD_PROJECTS_0_PROJECT_ID="engine",
        DOWNLOAD_PROJECTS_0_BUILD_ID="b-1",
        DOWNLOAD_PROJECTS_0_VARIANT="linux",
        DOWNLOAD_PROJECTS_0_GIT_REF=MASTER,
        DOWNLOAD_PROJECTS_0_FILES="*.zip",
        DOWNLOAD_PROJECTS_0_OUTPUT_DIR="out",
    )

    outcome = hook("pre-command", job)

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["b.zip"]


def test_exclude_removes_matched_objects_by_their_path_in_the_build(job, store, hook):
    put_objects(store, ["a-server.tar.zst", "debug/a-server.tar.zst", "b-server.tar.zst"])

    outcome = download(
        job,
        hook,
        project(files=["*-server.tar.zst"], exclude=["debug/*", "b-*"], **{"output-dir": "out"}),
    )

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["a-server.tar.zst"]


def test_without_output_dir_files_land_in_the_checkout_under_their_build_path(job, store, hook):
    put_objects(store, ["artifacts/x/a.bin"])

    outcome = download(job, hook, project(files=["*.bin"]))

    assert outcome.code == 0, outcome.output
    assert job.files() == ["artifacts/x/a.bin"]
    assert (job.workspace / "artifacts/x/a.bin").read_bytes() == b"artifacts/x/a.bin"


@pytest.mark.parametrize("key", ["output-dir", "output_dir"])
def test_output_dir_reaches_the_plugin_with_hyphen_or_underscore(job, store, hook, key):
    put_objects(store, ["a.bin"])

    outcome = download(job, hook, project(files=["*.bin"], **{key: "deep/out"}))

    assert outcome.code == 0, outcome.output
    assert job.files() == ["deep/out/a.bin"]


def test_an_existing_file_is_overwritten_and_other_files_are_kept(job, store, hook):
    put_objects(store, ["a.bin"])
    job.write("out/a.bin", b"stale")
    job.write("out/keep.txt", b"keep")

    outcome = download(job, hook, project(files=["*.bin"], **{"output-dir": "out"}))

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["a.bin", "keep.txt"]
    assert (job.workspace / "out/a.bin").read_bytes() == b"a.bin"


def test_clean_empties_every_project_output_dir_before_the_first_download(job, store, hook):
    put_objects(store, ["a.bin"])
    job.write("first/stale.txt", b"s")
    job.write("second/stale.txt", b"s")
    job.write("untouched/stale.txt", b"s")

    outcome = download(
        job,
        hook,
        project(files=["*.bin"], **{"output-dir": "first"}),
        project(files=["*.nothing"], **{"output-dir": "second"}),
        clean=True,
    )

    assert outcome.code == 1
    assert job.files() == ["first/a.bin", "untouched/stale.txt"]


def test_clean_without_output_dir_deletes_the_checkout_and_then_fails(job, store, hook):
    put_objects(store, ["a.bin"])
    job.write("source.c", b"int main;")

    outcome = download(job, hook, project(files=["*.bin"]), clean=True)

    assert outcome.code == 1
    assert "FileNotFoundError" in outcome.stderr
    assert not job.workspace.exists()


def test_a_large_artifact_downloads_intact(job, store, hook):
    body = bytes(range(256)) * (36 * 1024)
    store.put(f"{PREFIX}/big.bin", body)

    outcome = download(job, hook, project(files=["*.bin"], **{"output-dir": "out"}))

    assert outcome.code == 0, outcome.output
    assert (job.workspace / "out/big.bin").read_bytes() == body


@pytest.mark.parametrize(
    "options, logged",
    [
        ({}, "parallel=4, concurrency=32"),
        ({"parallel": 1, "concurrency": 2}, "parallel=1, concurrency=2"),
    ],
)
def test_parallel_and_concurrency_are_accepted(job, store, hook, options, logged):
    put_objects(store, ["a.bin", "b.bin", "c.bin"])

    outcome = download(job, hook, project(files=["*.bin"], **{"output-dir": "o"}), **options)

    assert outcome.code == 0, outcome.output
    assert logged in outcome.stderr
    assert job.files("o") == ["a.bin", "b.bin", "c.bin"]


def test_a_download_that_matches_nothing_fails_and_shows_what_is_there(job, store, hook):
    put_objects(store, [f"file-{i:02d}.zip" for i in range(23)])

    outcome = download(
        job, hook, project(files=["*.tar.zst", "*-c-api.tar.gz!lib/*"], exclude=["*-debug*"])
    )

    assert outcome.code == 1
    for fact in [
        "'*.tar.zst'",
        "'*-c-api.tar.gz!lib/*'",
        "'*-debug*'",
        f"s3://example-artifacts/{PREFIX}/",
        "23",
        "file-00.zip",
        "file-19.zip",
        "3 more",
    ]:
        assert fact in outcome.stderr
    assert "file-20.zip" not in outcome.stderr
    assert job.files() == []


def test_a_download_whose_matches_are_all_excluded_fails(job, store, hook):
    put_objects(store, ["a.bin"])

    outcome = download(job, hook, project(files=["*.bin"], exclude=["*"]))

    assert outcome.code == 1
    assert "a.bin" in outcome.stderr
    assert job.files() == []


def test_a_failed_project_stops_the_hook_after_the_projects_before_it(job, store, hook):
    put_objects(store, ["a.bin"])

    outcome = download(
        job,
        hook,
        project(files=["*.bin"], **{"output-dir": "first"}),
        project(build_id="b-missing", files=["*.bin"], **{"output-dir": "second"}),
        project(files=["*.bin"], **{"output-dir": "third"}),
    )

    assert outcome.code == 1
    assert job.files() == ["first/a.bin"]
