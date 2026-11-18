"""How a download with extract unpacks archives, which entries it keeps, what it refuses."""

import stat
import tarfile

import pytest

MASTER = "refs/heads/master"
PREFIX = f"engine/{MASTER}/variants/linux/builds/b-1"
COMPONENT = {"bin/widgetd": b"server", "lib/libwidget.so": b"library", "README": b"readme"}
RETRY_SLEEPS = [5, 10, 20, 40, 60, 60, 60, 60, 60]

# Python 3.9.17+, 3.10.12+, 3.11.4+ and 3.12+ have tarfile.data_filter; 3.9.6 has not.
HAS_DATA_FILTER = hasattr(tarfile, "data_filter")


def extracting(job, files, output_dir="out", extract=True):
    project = {
        "project_id": "engine",
        "build_id": "b-1",
        "variant": "linux",
        "git_ref": MASTER,
        "files": files,
        "extract": extract,
        "output-dir": output_dir,
    }
    return job.with_plugin({"download": {"projects": [project]}})


def extract(job, hook, files, output_dir="out"):
    return hook("pre-command", extracting(job, files, output_dir))


def archive_reads(store, rel):
    return [
        r for r in store.plugin_requests("s3") if r.method == "GET" and r.key == f"{PREFIX}/{rel}"
    ]


def read(job, rel):
    return (job.workspace / rel).read_bytes()


def file_mode(job, rel):
    return stat.S_IMODE((job.workspace / rel).stat().st_mode)


@pytest.mark.parametrize("suffix", [".tar.zst", ".tar.zstd", ".tar.gz", ".zip", ".jar"])
def test_each_supported_format_is_unpacked_into_the_output_dir(job, store, hook, archive, suffix):
    store.put(f"{PREFIX}/artifacts/widget{suffix}", archive(suffix, COMPONENT))

    outcome = extract(job, hook, ["*"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["README", "bin/widgetd", "lib/libwidget.so"]
    assert read(job, "out/bin/widgetd") == b"server"


def test_extraction_without_output_dir_unpacks_into_the_checkout(job, store, hook, archive):
    store.put(f"{PREFIX}/widget.tar.zst", archive(".tar.zst", COMPONENT))

    outcome = extract(job, hook, ["*.tar.zst"], output_dir=".")

    assert outcome.code == 0, outcome.output
    assert job.files() == ["README", "bin/widgetd", "lib/libwidget.so"]


def test_an_entry_filter_keeps_matching_entries_and_strips_its_fixed_prefix(
    job, store, hook, archive
):
    members = {**COMPONENT, "bin/sub/tool": b"tool", "docs/bin/x": b"x"}
    store.put(f"{PREFIX}/widget-server.tar.zst", archive(".tar.zst", members))

    outcome = extract(job, hook, ["*-server.tar.zst!bin/*"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["sub/tool", "widgetd"]


def test_several_filters_on_one_archive_extract_their_union_from_one_download(
    job, store, hook, archive
):
    store.put(f"{PREFIX}/widget-c-api.zip", archive(".zip", COMPONENT))

    outcome = extract(job, hook, ["*-c-api.zip!bin/*", "*-c-api.zip!lib/*"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["libwidget.so", "widgetd"]
    gets = [
        r
        for r in store.plugin_requests("s3")
        if r.method == "GET" and r.path.endswith("widget-c-api.zip")
    ]
    assert len(gets) == 1


@pytest.mark.parametrize(
    "files, expected",
    [
        (["w.tar.gz!lib/*", "w.tar.gz!*"], ["README", "bin/widgetd", "libwidget.so"]),
        (["w.tar.gz!*", "w.tar.gz!lib/*"], ["README", "bin/widgetd", "lib/libwidget.so"]),
        (["w.tar.gz", "w.tar.gz!lib/*"], ["README", "bin/widgetd", "lib/libwidget.so"]),
    ],
    ids=["strip-first", "keep-first", "unfiltered-first"],
)
def test_the_first_matching_rule_in_configuration_order_decides_an_entry_path(
    job, store, hook, archive, files, expected
):
    store.put(f"{PREFIX}/w.tar.gz", archive(".tar.gz", COMPONENT))

    outcome = extract(job, hook, files)

    assert outcome.code == 0, outcome.output
    assert job.files("out") == expected


@pytest.mark.parametrize(
    "rule, entry, expected",
    [
        ("include/widget/*.h", "include/widget/client.h", "client.h"),
        ("lib/libwidget?.so", "lib/libwidget1.so", "1.so"),
        ("doc/[a-z]*", "doc/guide.html", "guide.html"),
    ],
)
def test_the_stripped_prefix_ends_at_the_first_glob_character(
    job, store, hook, archive, rule, entry, expected
):
    store.put(f"{PREFIX}/w.zip", archive(".zip", {entry: b"e"}))

    outcome = extract(job, hook, [f"w.zip!{rule}"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == [expected]


def test_a_filter_without_glob_characters_extracts_nothing(job, store, hook, archive):
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", COMPONENT))

    outcome = extract(job, hook, ["w.tar.zst!README"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == []


def test_archive_directories_appear_only_through_tar(job, store, hook, archive):
    entries = [{"name": "empty", "type": "dir"}, {"name": "data/a", "data": b"a"}]
    store.put(f"{PREFIX}/t.tar.gz", archive(".tar.gz", entries))
    store.put(f"{PREFIX}/z.zip", archive(".zip", [{"name": "zempty/"}, *entries[1:]]))

    tar_outcome = extract(job, hook, ["t.tar.gz"], output_dir="t")
    zip_outcome = extract(job, hook, ["z.zip"], output_dir="z")

    assert tar_outcome.code == 0, tar_outcome.output
    assert zip_outcome.code == 0, zip_outcome.output
    assert (job.workspace / "t" / "empty").is_dir()
    assert not (job.workspace / "z" / "zempty").exists()


def test_a_tar_symlink_inside_the_output_dir_is_kept(job, store, hook, archive):
    entries = [
        {"name": "lib/libwidget.so.3", "data": b"so"},
        {"name": "lib/libwidget.so", "type": "symlink", "target": "libwidget.so.3"},
    ]
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", entries))

    outcome = extract(job, hook, ["*.tar.zst"])

    assert outcome.code == 0, outcome.output
    link = job.workspace / "out" / "lib" / "libwidget.so"
    assert link.is_symlink() and read(job, "out/lib/libwidget.so") == b"so"


def test_tar_modes_keep_the_executable_bit_and_lose_group_write_only_with_data_filter(
    job, store, hook, archive
):
    entries = [
        {"name": "bin/widgetd", "data": b"x", "mode": 0o755},
        {"name": "bin/wide", "data": b"w", "mode": 0o777},
    ]
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", entries))

    outcome = extract(job, hook, ["*.tar.zst"])

    assert outcome.code == 0, outcome.output
    assert file_mode(job, "out/bin/widgetd") == 0o755
    assert file_mode(job, "out/bin/wide") == (0o755 if HAS_DATA_FILTER else 0o777)


def test_the_downloaded_archive_and_its_temporary_file_are_not_kept(job, store, hook, archive):
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", COMPONENT))

    outcome = extract(job, hook, ["*.tar.zst"])

    assert outcome.code == 0, outcome.output
    assert not [f for f in job.files("out") if f.endswith((".tmp", ".tar.zst"))]


def test_an_archive_is_downloaded_to_a_temporary_file_inside_the_output_dir(
    job, store, hook_midway, archive
):
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", COMPONENT))
    # The transfer asks for the size first, after the temporary file exists.
    size_request = store.hold("HEAD", f"{PREFIX}/w.tar.zst")
    seen = []

    def look_then_release(process):
        seen.extend(job.files("out"))
        store.release(size_request)

    outcome = hook_midway(
        "pre-command", extracting(job, ["*.tar.zst"]), size_request.arrived, look_then_release
    )

    assert outcome.code == 0, outcome.output
    assert len(seen) == 1 and seen[0].endswith(".tmp")
    assert job.files("out") == ["README", "bin/widgetd", "lib/libwidget.so"]


def test_extract_false_keeps_an_archive_as_it_is(job, store, hook, archive):
    body = archive(".tar.zst", COMPONENT)
    store.put(f"{PREFIX}/w.tar.zst", body)

    outcome = hook("pre-command", extracting(job, ["*.tar.zst"], extract=False))

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["w.tar.zst"]
    assert read(job, "out/w.tar.zst") == body


def test_an_unsupported_format_fails_at_once_after_download_and_leaves_no_temporary_file(
    job, store, hook, instant_clock
):
    store.put(f"{PREFIX}/notes.txt", b"plain text")

    outcome = extract(job.with_env(**instant_clock.env), hook, ["*"])

    assert outcome.code == 1
    assert "notes.txt" in outcome.error
    assert len(archive_reads(store, "notes.txt")) == 1
    assert instant_clock.sleeps() == []
    assert job.files("out") == []


def test_a_zip_entry_may_write_outside_the_output_dir(job, store, hook, archive, tmp_path):
    escape_target = tmp_path / "absolute-escape.txt"
    entries = {"../escaped.txt": b"up", str(escape_target): b"absolute", "ok.txt": b"ok"}
    store.put(f"{PREFIX}/w.zip", archive(".zip", entries))

    outcome = extract(job, hook, ["*.zip"])

    assert outcome.code == 0, outcome.output
    assert read(job, "escaped.txt") == b"up"
    assert escape_target.read_bytes() == b"absolute"
    assert job.files("out") == ["ok.txt"]


# ---------------------------------------------------------------------------
# Archives that fail inside the transfer retry loop. Each attempt downloads the
# archive again; the job gets the instant clock, so the 375 s of waits cost
# nothing and are recorded.
# ---------------------------------------------------------------------------

UNSAFE_TAR_MEMBERS = {
    "parent-path": ({"name": "../escape", "data": b"e"}, "OutsideDestinationError"),
    "link-outside": (
        {"name": "link", "type": "symlink", "target": "../../outside"},
        "LinkOutsideDestinationError",
    ),
    "fifo": ({"name": "pipe", "type": "fifo"}, "SpecialFileError"),
}


def extract_on_the_instant_clock(job, store, hook, instant_clock, body, files=("*.tar.zst",)):
    store.put(f"{PREFIX}/w.tar.zst", body)
    return extract(job.with_env(**instant_clock.env), hook, list(files))


def assert_failed_after_ten_attempts(store, instant_clock, outcome):
    assert outcome.code == 1
    assert instant_clock.sleeps() == RETRY_SLEEPS
    assert len(archive_reads(store, "w.tar.zst")) == 10


@pytest.mark.parametrize("case", sorted(UNSAFE_TAR_MEMBERS))
def test_an_unsafe_tar_member_fails_the_download_after_ten_attempts(
    job, store, hook, archive, instant_clock, case
):
    entry, error_with_data_filter = UNSAFE_TAR_MEMBERS[case]

    outcome = extract_on_the_instant_clock(
        job, store, hook, instant_clock, archive(".tar.zst", [entry])
    )

    assert_failed_after_ten_attempts(store, instant_clock, outcome)
    if HAS_DATA_FILTER:
        assert error_with_data_filter in outcome.stderr
    else:
        assert "AttributeError" in outcome.stderr and "FilterError" in outcome.stderr
    assert job.files() == []


def test_an_absolute_tar_member_is_rooted_in_the_output_dir_only_with_data_filter(
    job, store, hook, archive, instant_clock
):
    body = archive(".tar.zst", [{"name": "/abs/file", "data": b"abs"}])

    outcome = extract_on_the_instant_clock(job, store, hook, instant_clock, body)

    if HAS_DATA_FILTER:
        assert outcome.code == 0, outcome.output
        assert read(job, "out/abs/file") == b"abs"
        assert instant_clock.sleeps() == []
    else:
        assert_failed_after_ten_attempts(store, instant_clock, outcome)
        assert "AttributeError" in outcome.stderr


def test_a_corrupt_archive_fails_the_download_after_ten_attempts(job, store, hook, instant_clock):
    outcome = extract_on_the_instant_clock(job, store, hook, instant_clock, b"not zstd at all")

    assert_failed_after_ten_attempts(store, instant_clock, outcome)
    assert "ZstdError" in outcome.stderr


HARD_LINKED = [
    {"name": "dir/a", "data": b"x"},
    {"name": "dir/b", "type": "hardlink", "target": "dir/a"},
]


def test_a_tar_hard_link_is_extracted_as_a_second_file(job, store, hook, archive):
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", HARD_LINKED))

    outcome = extract(job, hook, ["*.tar.zst"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["dir/a", "dir/b"]
    assert read(job, "out/dir/b") == b"x"


def test_a_tar_hard_link_under_a_prefix_stripping_filter_fails_the_download(
    job, store, hook, archive, instant_clock
):
    body = archive(".tar.zst", HARD_LINKED)

    outcome = extract_on_the_instant_clock(
        job, store, hook, instant_clock, body, files=["*.tar.zst!dir/*"]
    )

    assert_failed_after_ten_attempts(store, instant_clock, outcome)
    assert "KeyError" in outcome.stderr and "dir/a" in outcome.stderr
