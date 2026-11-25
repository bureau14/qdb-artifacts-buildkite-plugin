"""What an entry filter extracts, and under which name: the filter's fixed directory is
stripped from the output path, and the rest of the entry's name stays."""

from __future__ import annotations

import gzip
import io
import tarfile
import zipfile

import pytest
import zstandard

from lib.extract import extract_local_archive
from lib.rules import match_rules, parse_rules

ENTRIES = ("README", "dir/README", "dir/other.txt")


def as_tar(entries: tuple) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name in entries:
            info = tarfile.TarInfo(name)
            info.size = len(name)
            archive.addfile(info, io.BytesIO(name.encode()))
    return raw.getvalue()


def as_zip(entries: tuple) -> bytes:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        for name in entries:
            archive.writestr(name, name)
    return raw.getvalue()


ARCHIVES = {
    "w.tar.gz": lambda e: gzip.compress(as_tar(e)),
    "w.tar.zst": lambda e: zstandard.ZstdCompressor().compress(as_tar(e)),
    "w.zip": as_zip,
}

# entry filter -> {the path where an extracted entry lands: the name of the entry, which is its content}
FILTERS = {
    "README": {"README": "README"},
    "dir/README": {"README": "dir/README"},
}


def extracted(tmp_path, archive_name, entry_filter):
    archive = tmp_path / archive_name
    archive.write_bytes(ARCHIVES[archive_name](ENTRIES))
    out = tmp_path / "out"
    out.mkdir()
    rules = match_rules(archive_name, parse_rules([f"{archive_name}!{entry_filter}"]))
    extract_local_archive(str(archive), archive_name, out, rules)
    return {str(p.relative_to(out)): p.read_text() for p in out.rglob("*") if p.is_file()}


@pytest.mark.parametrize("archive_name", sorted(ARCHIVES))
@pytest.mark.parametrize("entry_filter", sorted(FILTERS))
def test_an_entry_filter_without_glob_characters_extracts_its_entry(
    tmp_path, archive_name, entry_filter
):
    """An entry filter that names one entry extracts that entry, and only it, under the entry's
    own name without the directories of the filter."""
    assert extracted(tmp_path, archive_name, entry_filter) == FILTERS[entry_filter]
