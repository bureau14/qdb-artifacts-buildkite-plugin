"""Unpack a downloaded archive into the output dir, refusing tar members that
would land outside it.

The download puts an archive in a temporary file in the output dir, through
boto3's transfer manager (parallel ranged GETs), and this module unpacks it
from there:

    .tar.gz             tarfile, seekable
    .tar.zst, .tar.zstd zstandard's stream reader into tarfile's stream mode
    .zip, .jar          zipfile, seekable
"""

import shutil
import tarfile
import zipfile
from pathlib import Path

import zstandard

from .errors import ArtifactsError
from .rules import select_entry_path


def _filter_tar_member(member, entry_rules):
    """Apply matching entry rules to a tar member, or return None to skip.
    member.name is mutated in-place (tarfile's path remapping convention)."""
    member.name = select_entry_path(member.name, entry_rules)
    return member if member.name else None


def _extract_tar(tar, out, entry_filter=None, strip_prefix=None, entry_rules=None):
    """Extract tar members with optional filtering, through tarfile's data filter:
    it roots absolute paths in the output dir, and refuses special files and the
    members and links that would land outside it. Streaming mode means
    sequential-only access -- skipped members still consume I/O from the stream."""
    entry_rules = entry_rules or [(entry_filter, strip_prefix)]
    for member in tar:
        member = _filter_tar_member(member, entry_rules)
        if member is None:
            continue
        tar.extract(member, path=out, filter="data")


def extract_local_archive(
    path, rel, output_dir, entry_filter=None, strip_prefix=None, entry_rules=None
):
    """Extract a locally downloaded archive file.

    .tar.gz  -- tarfile 'r:gz' (seekable, more efficient than streaming mode)
    .tar.zst -- zstandard.stream_reader -> tarfile 'r|'
    .zip     -- zipfile.ZipFile (seekable local file)
    """
    out = Path(output_dir)
    entry_rules = entry_rules or [(entry_filter, strip_prefix)]

    if rel.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as tar:
            _extract_tar(tar, out, entry_rules=entry_rules)

    elif rel.endswith((".tar.zst", ".tar.zstd")):
        dctx = zstandard.ZstdDecompressor()
        with (
            open(path, "rb") as fh,
            dctx.stream_reader(fh) as reader,
            tarfile.open(fileobj=reader, mode="r|") as tar,
        ):
            _extract_tar(tar, out, entry_rules=entry_rules)

    elif rel.endswith((".zip", ".jar")):
        with zipfile.ZipFile(path, "r") as zf:
            for info in zf.infolist():
                name = info.filename
                if name.endswith("/"):
                    continue
                name = select_entry_path(name, entry_rules)
                if not name:
                    continue
                dest = out / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    else:
        raise ArtifactsError(f"unsupported archive format: {rel}")
