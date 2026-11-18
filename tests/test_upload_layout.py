"""Where an upload puts artifacts and what it writes next to them."""

import datetime
import json

import pytest

PREFIX = "widget-build/refs/heads/master/variants/linux-amd64-release/builds/build-7"


def upload_step(**upload):
    return {"upload": {"variant": "linux-amd64-release", "git_ref": "refs/heads/master", **upload}}


def upload(job, hook, **options):
    outcome = hook(
        "post-command",
        job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(upload_step(**options)),
    )
    assert outcome.code == 0, outcome.output
    return outcome


def test_keys_are_project_ref_variant_build_and_checkout_relative_path(job, store, hook):
    job.write("artifacts/widget-server.tar.zst", b"server")
    job.write("artifacts/nested/widget-utils.tar.zst", b"utils")

    upload(job, hook, files="artifacts/**/*.tar.zst")

    assert store.stored_keys() == [
        f"{PREFIX}/artifacts/nested/widget-utils.tar.zst",
        f"{PREFIX}/artifacts/widget-server.tar.zst",
        f"{PREFIX}/manifest.json",
    ]
    assert store.get(f"{PREFIX}/artifacts/widget-server.tar.zst") == b"server"


def test_slashes_around_a_ref_add_no_empty_key_level(job, store, hook):
    job.write("a.bin", b"a")
    job = job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(
        {"upload": {"variant": "v", "git_ref": "/refs/heads/feature/", "files": "a.bin"}}
    )

    assert hook("post-command", job).code == 0

    assert store.stored_keys() == [
        "widget-build/refs/heads/feature/variants/v/builds/build-7/a.bin",
        "widget-build/refs/heads/feature/variants/v/builds/build-7/manifest.json",
    ]


def test_a_ref_with_slashes_nests_one_key_level_per_segment(job, store, hook):
    job.write("a.bin", b"a")
    job = job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(
        {"upload": {"variant": "v", "git_ref": "refs/heads/feature/widget/x", "files": "a.bin"}}
    )

    assert hook("post-command", job).code == 0

    assert (
        "widget-build/refs/heads/feature/widget/x/variants/v/builds/build-7/a.bin"
        in store.stored_keys()
    )


@pytest.mark.parametrize(
    "destination, key_prefix",
    [
        ("s3://example-artifacts/ci/artifacts/", "ci/artifacts/"),
        ("s3://example-artifacts", ""),
        ("example-artifacts", ""),
        ("example-artifacts/", ""),
    ],
)
def test_destination_names_the_bucket_and_an_optional_key_prefix(
    job, store, hook, destination, key_prefix
):
    job.write("a.bin", b"a")

    upload(job.with_env(ARTIFACTS_DESTINATION=destination), hook, files="a.bin")

    assert store.stored_keys() == [
        f"{key_prefix}{PREFIX}/a.bin",
        f"{key_prefix}{PREFIX}/manifest.json",
    ]


def test_project_and_build_default_to_the_pipeline_slug_and_build_id(job, store, hook):
    job.write("a.bin", b"a")
    job = job.with_env(BUILDKITE_PIPELINE_SLUG="gadget", BUILDKITE_BUILD_ID="b-1").with_plugin(
        upload_step(files="a.bin")
    )

    assert hook("post-command", job).code == 0

    assert (
        "gadget/refs/heads/master/variants/linux-amd64-release/builds/b-1/a.bin"
        in store.stored_keys()
    )


def test_project_id_and_the_undeclared_build_id_option_override_the_job(job, store, hook):
    job.write("a.bin", b"a")
    job = job.with_env(BUILDKITE_PIPELINE_SLUG="gadget", BUILDKITE_BUILD_ID="b-1").with_plugin(
        upload_step(files="a.bin", project_id="gizmo", build_id="b-override")
    )

    assert hook("post-command", job).code == 0

    assert store.stored_keys(prefix="gizmo/") == [
        "gizmo/refs/heads/master/variants/linux-amd64-release/builds/b-override/a.bin",
        "gizmo/refs/heads/master/variants/linux-amd64-release/builds/b-override/manifest.json",
    ]


def test_a_build_id_option_of_latest_successful_names_a_build_like_any_other(job, store, hook):
    # The kind of a key follows from what is written, never from the build id's value.
    job.write("a.bin", b"a")

    upload(job, hook, files="a.bin", build_id="LATEST_SUCCESSFUL")

    builds = "widget-build/refs/heads/master/variants/linux-amd64-release/builds"
    assert store.stored_keys() == [
        f"{builds}/LATEST_SUCCESSFUL/a.bin",
        f"{builds}/LATEST_SUCCESSFUL/manifest.json",
    ]


def test_artifact_is_sent_with_its_file_name_as_disposition_and_no_content_type(job, store, hook):
    job.write("artifacts/widget-server.tar.zst", b"server")

    upload(job, hook, files="artifacts/*")

    puts = [r for r in store.plugin_requests("s3") if r.method == "PUT"]
    assert [r.path for r in puts] == [
        f"/{store.bucket}/{PREFIX}/artifacts/widget-server.tar.zst",
        f"/{store.bucket}/{PREFIX}/manifest.json",
    ]
    assert puts[0].content_disposition == 'attachment; filename="widget-server.tar.zst"'
    assert puts[0].content_type == ""
    assert puts[1].content_type == "application/json"


NINE_MIB = bytes(range(256)) * (36 * 1024)


def test_a_large_artifact_is_uploaded_in_parts_with_its_disposition(job, store, hook):
    job.write("big.bin", NINE_MIB)

    upload(job, hook, files="big.bin")

    starts = [r for r in store.plugin_requests("s3") if r.method == "POST" and "uploads" in r.query]
    assert len(starts) == 1
    assert starts[0].content_disposition == 'attachment; filename="big.bin"'
    assert store.get(f"{PREFIX}/big.bin") == NINE_MIB
    assert store.head(f"{PREFIX}/big.bin")["ContentDisposition"] == 'attachment; filename="big.bin"'


def test_concurrency_lets_the_parts_of_one_artifact_travel_at_the_same_time(job, store, hook):
    job.write("big.bin", NINE_MIB)
    key = f"{PREFIX}/big.bin"
    first_part = store.hold(
        "PUT",
        key,
        query=[("partNumber", "1")],
        until=("PUT", key, (("partNumber", "2"),)),
        # Only parts sent one after the other wait this out; a loaded agent may be slow.
        timeout=30,
    )

    upload(job, hook, files="big.bin", parallel=1, concurrency=2)

    assert first_part.ended_by == "until"


def test_a_concurrency_of_one_sends_the_parts_of_one_artifact_one_after_the_other(job, store, hook):
    job.write("big.bin", NINE_MIB)
    key = f"{PREFIX}/big.bin"
    first_part = store.hold(
        "PUT",
        key,
        query=[("partNumber", "1")],
        until=("PUT", key, (("partNumber", "2"),)),
        # Parts sent at the same time end the hold at once; this is ample for them.
        timeout=3,
    )

    upload(job, hook, files="big.bin", parallel=1, concurrency=1)

    assert first_part.ended_by == "timeout"


def test_parallel_lets_files_travel_at_the_same_time(job, store, hook):
    job.write("a.bin", b"a")
    job.write("b.bin", b"b")
    first_file = store.hold(
        "PUT",
        f"{PREFIX}/a.bin",
        until=("PUT", f"{PREFIX}/b.bin", ()),
        # Only files sent one after the other wait this out; a loaded agent may be slow.
        timeout=30,
    )

    upload(job, hook, files="*.bin", parallel=2)

    assert first_file.ended_by == "until"


def test_manifest_is_compact_json_with_five_fields_in_fixed_order(job, store, hook):
    job.write("artifacts/b.tar.zst", b"bbbb")
    job.write("artifacts/a.tar.zst", b"aa")
    before = datetime.datetime.now(datetime.timezone.utc)

    upload(job, hook, files="artifacts/*.tar.zst")

    after = datetime.datetime.now(datetime.timezone.utc)
    body = store.get(f"{PREFIX}/manifest.json").decode()
    uploaded_at = json.loads(body)["uploaded_at"]
    assert body == (
        '{"file_count":2,"files":["artifacts/a.tar.zst","artifacts/b.tar.zst"],'
        f'"uploaded_at":"{uploaded_at}","total_size_bytes":6,"base_dir":null}}'
    )
    stamp = datetime.datetime.fromisoformat(uploaded_at)
    assert uploaded_at.endswith("+00:00")
    assert before <= stamp <= after


def test_a_retried_upload_overwrites_and_adds_but_never_deletes(job, store, hook):
    job.write("a.bin", b"first")
    job.write("b.bin", b"first")
    upload(job, hook, files="*.bin")
    (job.workspace / "b.bin").unlink()
    job.write("a.bin", b"retry")

    upload(job, hook, files="*.bin")

    assert store.stored_keys() == [f"{PREFIX}/a.bin", f"{PREFIX}/b.bin", f"{PREFIX}/manifest.json"]
    assert store.get(f"{PREFIX}/a.bin") == b"retry"
    assert json.loads(store.get(f"{PREFIX}/manifest.json"))["files"] == ["a.bin"]


def test_the_manifest_follows_the_last_artifact_and_is_stamped_after_it(job, store, hook):
    job.write("a.bin", b"a")
    # A slow artifact makes the order and the stamp visible without racing on timing.
    slow = store.hold("PUT", f"{PREFIX}/a.bin", timeout=1)

    upload(job, hook, files="a.bin")

    (manifest_put,) = [r for r in store.plugin_requests("s3") if r.key == f"{PREFIX}/manifest.json"]
    assert manifest_put.arrived_at > slow.released_at
    manifest = json.loads(store.get(f"{PREFIX}/manifest.json"))
    assert datetime.datetime.fromisoformat(manifest["uploaded_at"]) > slow.released_at


def test_manifest_is_written_after_every_artifact(job, store, hook):
    for name in ("a", "b", "c", "d", "e"):
        job.write(f"{name}.bin", name.encode())

    upload(job, hook, files="*.bin")

    puts = [r.path for r in store.plugin_requests("s3") if r.method == "PUT"]
    assert len(puts) == 6
    assert puts[-1] == f"/{store.bucket}/{PREFIX}/manifest.json"


def test_manifest_lists_files_in_path_order_not_string_order(job, store, hook):
    job.write("a-z.bin", b"1")
    job.write("a/y.bin", b"2")

    upload(job, hook, files=["*.bin", "a/*.bin"])

    assert json.loads(store.get(f"{PREFIX}/manifest.json"))["files"] == ["a/y.bin", "a-z.bin"]


def test_base_dir_strips_its_prefix_from_keys_while_globs_stay_checkout_relative(job, store, hook):
    job.write("dist/pkg/widget.tar.zst", b"w")

    upload(job, hook, files="dist/**/*.tar.zst", base_dir="dist/")

    assert store.stored_keys() == [f"{PREFIX}/manifest.json", f"{PREFIX}/pkg/widget.tar.zst"]
    manifest = json.loads(store.get(f"{PREFIX}/manifest.json"))
    assert manifest["files"] == ["pkg/widget.tar.zst"]
    assert manifest["base_dir"] == "dist/"


def test_base_dir_reaches_the_plugin_with_hyphen_or_underscore(job, store, hook):
    job.write("dist/widget.tar.zst", b"w")

    upload(job, hook, files="dist/*", **{"base-dir": "dist"})

    assert f"{PREFIX}/widget.tar.zst" in store.stored_keys()


def test_globs_are_recursive_and_select_regular_files_only(job, store, hook):
    job.write("out/top.bin", b"t")
    job.write("out/deep/er/low.bin", b"l")
    job.write("out/.hidden.bin", b"h")
    (job.workspace / "out" / "dir.bin").mkdir()

    upload(job, hook, files=["out/**/*.bin", "out/*"])

    assert store.stored_keys() == [
        f"{PREFIX}/manifest.json",
        f"{PREFIX}/out/deep/er/low.bin",
        f"{PREFIX}/out/top.bin",
    ]


def test_exclude_matches_the_key_path_and_its_star_crosses_directories(job, store, hook):
    job.write("dist/keep.tar.zst", b"k")
    job.write("dist/logs/build.log", b"l")
    job.write("dist/debug/keep.tar.zst", b"d")

    upload(job, hook, files="dist/**/*", base_dir="dist", exclude=["*.log", "debug/*"])

    assert store.stored_keys() == [f"{PREFIX}/keep.tar.zst", f"{PREFIX}/manifest.json"]
    assert json.loads(store.get(f"{PREFIX}/manifest.json"))["files"] == ["keep.tar.zst"]


@pytest.mark.parametrize(
    "options",
    [{"files": "a.bin"}, {"files": "missing/*", "allow_empty": True}],
    ids=["files-matched", "nothing-matched-but-allowed"],
)
def test_a_missing_base_dir_fails_the_upload(job, store, hook, options):
    job.write("a.bin", b"a")
    job = job.with_plugin(upload_step(base_dir="missing-dir", **options))

    outcome = hook("post-command", job)

    assert outcome.code == 1
    assert "missing-dir" in outcome.error
    assert store.requests == []


def test_a_file_outside_base_dir_fails_the_upload(job, store, hook):
    job.write("dist/a.bin", b"a")
    job.write("other/b.bin", b"b")
    job = job.with_plugin(upload_step(files=["dist/*", "other/*"], base_dir="dist"))

    outcome = hook("post-command", job)

    assert outcome.code == 1
    assert "other/b.bin" in outcome.error and "dist" in outcome.error
    assert store.stored_keys() == []


def test_a_file_outside_the_checkout_without_base_dir_fails_with_a_traceback(
    job, store, hook, tmp_path
):
    (tmp_path / "outside.bin").write_bytes(b"o")
    job = job.with_plugin(upload_step(files="../outside.bin"))

    outcome = hook("post-command", job)

    assert outcome.code == 1
    assert "Traceback" in outcome.stderr
    assert "ValueError" in outcome.stderr
    assert store.stored_keys() == []


@pytest.mark.parametrize(
    "options, logged",
    [
        ({}, "parallel=4, concurrency=32"),
        ({"parallel": 2, "concurrency": 3}, "parallel=2, concurrency=3"),
    ],
)
def test_parallel_and_concurrency_are_accepted(job, store, hook, options, logged):
    job.write("a.bin", b"a")
    job.write("b.bin", b"b")

    outcome = upload(job, hook, files="*.bin", **options)

    assert logged in outcome.stderr
    assert len(store.stored_keys()) == 3


def test_a_parallel_value_that_is_not_a_number_fails_with_a_traceback(job, store, hook):
    job.write("a.bin", b"a")
    job = job.with_plugin(upload_step(files="a.bin", parallel="four"))

    outcome = hook("post-command", job)

    assert outcome.code == 1
    assert "ValueError" in outcome.stderr
    assert store.stored_keys() == []
