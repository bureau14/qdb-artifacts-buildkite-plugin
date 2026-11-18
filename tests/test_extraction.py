"""How a download with extract unpacks archives, which entries it keeps, what it refuses."""

import stat
import tarfile
import time

import pytest
import zstandard

MASTER = "refs/heads/master"
PREFIX = f"engine/{MASTER}/variants/linux/builds/b-1"
COMPONENT = {"bin/qdbd": b"server", "lib/libqdb.so": b"library", "README": b"readme"}

# Python 3.9.17+, 3.10.12+, 3.11.4+ and 3.12+ have tarfile.data_filter; 3.9.6 has not.
HAS_DATA_FILTER = hasattr(tarfile, "data_filter")


def extract(job, hook, files, output_dir="out"):
    project = {
        "project_id": "engine",
        "build_id": "b-1",
        "variant": "linux",
        "git_ref": MASTER,
        "files": files,
        "extract": True,
        "output-dir": output_dir,
    }
    return hook("pre-command", job.with_plugin({"download": {"projects": [project]}}))


def read(job, rel):
    return (job.workspace / rel).read_bytes()


def file_mode(job, rel):
    return stat.S_IMODE((job.workspace / rel).stat().st_mode)


@pytest.mark.parametrize("suffix", [".tar.zst", ".tar.zstd", ".tar.gz", ".zip", ".jar"])
def test_each_supported_format_is_unpacked_into_the_output_dir(job, store, hook, archive, suffix):
    store.put(f"{PREFIX}/artifacts/widget{suffix}", archive(suffix, COMPONENT))

    outcome = extract(job, hook, ["*"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["README", "bin/qdbd", "lib/libqdb.so"]
    assert read(job, "out/bin/qdbd") == b"server"


def test_extraction_without_output_dir_unpacks_into_the_checkout(job, store, hook, archive):
    store.put(f"{PREFIX}/widget.tar.zst", archive(".tar.zst", COMPONENT))

    outcome = extract(job, hook, ["*.tar.zst"], output_dir=".")

    assert outcome.code == 0, outcome.output
    assert job.files() == ["README", "bin/qdbd", "lib/libqdb.so"]


def test_an_entry_filter_keeps_matching_entries_and_strips_its_fixed_prefix(
    job, store, hook, archive
):
    members = {**COMPONENT, "bin/sub/tool": b"tool", "docs/bin/x": b"x"}
    store.put(f"{PREFIX}/widget-server.tar.zst", archive(".tar.zst", members))

    outcome = extract(job, hook, ["*-server.tar.zst!bin/*"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["qdbd", "sub/tool"]


def test_several_filters_on_one_archive_extract_their_union_from_one_download(
    job, store, hook, archive
):
    store.put(f"{PREFIX}/widget-c-api.zip", archive(".zip", COMPONENT))

    outcome = extract(job, hook, ["*-c-api.zip!bin/*", "*-c-api.zip!lib/*"])

    assert outcome.code == 0, outcome.output
    assert job.files("out") == ["libqdb.so", "qdbd"]
    gets = [
        r
        for r in store.plugin_requests("s3")
        if r.method == "GET" and r.path.endswith("widget-c-api.zip")
    ]
    assert len(gets) == 1


@pytest.mark.parametrize(
    "files, expected",
    [
        (["w.tar.gz!lib/*", "w.tar.gz!*"], ["README", "bin/qdbd", "libqdb.so"]),
        (["w.tar.gz!*", "w.tar.gz!lib/*"], ["README", "bin/qdbd", "lib/libqdb.so"]),
        (["w.tar.gz", "w.tar.gz!lib/*"], ["README", "bin/qdbd", "lib/libqdb.so"]),
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
        ("include/qdb/*.h", "include/qdb/client.h", "client.h"),
        ("lib/libqdb?.so", "lib/libqdb1.so", "1.so"),
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
        {"name": "lib/libqdb.so.3", "data": b"so"},
        {"name": "lib/libqdb.so", "type": "symlink", "target": "libqdb.so.3"},
    ]
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", entries))

    outcome = extract(job, hook, ["*.tar.zst"])

    assert outcome.code == 0, outcome.output
    link = job.workspace / "out" / "lib" / "libqdb.so"
    assert link.is_symlink() and read(job, "out/lib/libqdb.so") == b"so"


def test_tar_modes_keep_the_executable_bit_and_lose_group_write_only_with_data_filter(
    job, store, hook, archive
):
    entries = [
        {"name": "bin/qdbd", "data": b"x", "mode": 0o755},
        {"name": "bin/wide", "data": b"w", "mode": 0o777},
    ]
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", entries))

    outcome = extract(job, hook, ["*.tar.zst"])

    assert outcome.code == 0, outcome.output
    assert file_mode(job, "out/bin/qdbd") == 0o755
    assert file_mode(job, "out/bin/wide") == (0o755 if HAS_DATA_FILTER else 0o777)


def test_the_downloaded_archive_and_its_temporary_file_are_not_kept(job, store, hook, archive):
    store.put(f"{PREFIX}/w.tar.zst", archive(".tar.zst", COMPONENT))

    outcome = extract(job, hook, ["*.tar.zst"])

    assert outcome.code == 0, outcome.output
    assert not [f for f in job.files("out") if f.endswith((".tmp", ".tar.zst"))]


def test_an_unsupported_format_fails_after_download_and_leaves_no_temporary_file(job, store, hook):
    store.put(f"{PREFIX}/notes.txt", b"plain text")

    outcome = extract(job, hook, ["*"])

    assert outcome.code == 1
    assert "notes.txt" in outcome.stderr
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
# Unsafe tar members. They fail inside the transfer retry loop, which sleeps
# 375 s in total; these run in-process with time.sleep recorded instead.
# ---------------------------------------------------------------------------

UNSAFE_TAR_MEMBERS = {
    "parent-path": ({"name": "../escape", "data": b"e"}, "OutsideDestinationError"),
    "link-outside": (
        {"name": "link", "type": "symlink", "target": "../../outside"},
        "LinkOutsideDestinationError",
    ),
    "fifo": ({"name": "pipe", "type": "fifo"}, "SpecialFileError"),
}


@pytest.fixture
def recorded_sleeps(monkeypatch):
    sleeps = []
    monkeypatch.setattr(time, "sleep", sleeps.append)
    return sleeps


def download_in_process(library, store, tmp_path, variant):
    return library.download_artifacts(
        cfg=library.StoreConfig(
            backend="s3", destination=f"s3://{store.bucket}", endpoint_url=store.endpoint
        ),
        auth=library.ObjectAuth(),
        project_id="engine",
        build_id="b-1",
        git_ref=MASTER,
        variant=variant,
        patterns=["*.tar.zst"],
        output_dir=str(tmp_path / "out"),
        extract=True,
    )


@pytest.mark.parametrize("case", sorted(UNSAFE_TAR_MEMBERS))
def test_an_unsafe_tar_member_fails_the_download_after_ten_attempts(
    library, library_environment, store, archive, tmp_path, recorded_sleeps, case
):
    entry, error_with_data_filter = UNSAFE_TAR_MEMBERS[case]
    store.put_build("engine", MASTER, "linux", "b-1", {"w.tar.zst": archive(".tar.zst", [entry])})
    expected = getattr(tarfile, error_with_data_filter) if HAS_DATA_FILTER else AttributeError

    with pytest.raises(expected) as raised:
        download_in_process(library, store, tmp_path, "linux")

    if not HAS_DATA_FILTER:
        assert "FilterError" in str(raised.value)
    assert recorded_sleeps == [5, 10, 20, 40, 60, 60, 60, 60, 60]
    assert not (tmp_path / "escape").exists()


def test_an_absolute_tar_member_is_rooted_in_the_output_dir_only_with_data_filter(
    library, library_environment, store, archive, tmp_path, recorded_sleeps
):
    entries = [{"name": "/abs/file", "data": b"abs"}]
    store.put_build("engine", MASTER, "linux", "b-1", {"w.tar.zst": archive(".tar.zst", entries)})

    if HAS_DATA_FILTER:
        download_in_process(library, store, tmp_path, "linux")
        assert (tmp_path / "out" / "abs" / "file").read_bytes() == b"abs"
        assert recorded_sleeps == []
    else:
        with pytest.raises(AttributeError):
            download_in_process(library, store, tmp_path, "linux")
        assert len(recorded_sleeps) == 9


def test_a_corrupt_archive_fails_the_download_after_ten_attempts(
    library, library_environment, store, tmp_path, recorded_sleeps
):
    store.put_build("engine", MASTER, "linux", "b-1", {"w.tar.zst": b"not zstd at all"})

    with pytest.raises(zstandard.ZstdError):
        download_in_process(library, store, tmp_path, "linux")

    assert recorded_sleeps == [5, 10, 20, 40, 60, 60, 60, 60, 60]
