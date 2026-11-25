"""Unpack a downloaded archive into the output dir, refusing members that would
land outside it.

The download puts an archive in a temporary file in the output dir, through
boto3's transfer manager (parallel ranged GETs), and this module unpacks it
from there:

    .tar.gz             tarfile, seekable
    .tar.zst, .tar.zstd zstandard's stream reader into tarfile's stream mode
    .zip, .jar          zipfile, seekable

A refused member raises ArtifactsError: another attempt of the transfer would
meet the same member, so the transfer does not try again.
"""

import shutil
import tarfile
import zipfile
from pathlib import Path

import zstandard

from .errors import ArtifactsError
from .rules import select_entry_path


def extract_tar(tar, out, rel, entry_rules):
    """Extract the members of the archive ``rel`` that ``entry_rules`` select, through
    tarfile's data filter: it roots absolute paths in the output dir, and refuses special
    files and the members and links that would land outside it. In stream mode a skipped
    member is still read."""
    for member in tar:
        # tarfile writes a member where its name says.
        member.name = select_entry_path(member.name, entry_rules)
        if not member.name:
            continue
        try:
            tar.extract(member, path=out, filter="data")
        except tarfile.FilterError as exc:
            raise ArtifactsError(f"{rel}: {exc}") from exc


def zip_destination(out, rel, name):
    """Where the zip or jar member ``name`` of the archive ``rel`` lands in ``out``. A member
    whose path leaves ``out``, through a parent path or as an absolute name, is refused."""
    dest = (out / name).resolve()
    if not dest.is_relative_to(out.resolve()):
        raise ArtifactsError(
            f"{rel}: {name!r} would be extracted to {str(dest)!r}, which is outside the destination"
        )
    return dest


def extract_zip(zf, out, rel, entry_rules):
    for info in zf.infolist():
        is_folder = info.filename.endswith("/")
        name = None if is_folder else select_entry_path(info.filename, entry_rules)
        if not name:
            continue
        dest = zip_destination(out, rel, name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)


def extract_local_archive(path, rel, output_dir, entry_rules):
    """Extract the archive at ``path``, which the store names ``rel``, into ``output_dir``:
    the entries that ``entry_rules`` select, at the paths they give."""
    out = Path(output_dir)
    if rel.endswith(".tar.gz"):
        with tarfile.open(path, mode="r:gz") as tar:
            extract_tar(tar, out, rel, entry_rules)
    elif rel.endswith((".tar.zst", ".tar.zstd")):
        with (
            open(path, "rb") as fh,
            zstandard.ZstdDecompressor().stream_reader(fh) as reader,
            tarfile.open(fileobj=reader, mode="r|") as tar,
        ):
            extract_tar(tar, out, rel, entry_rules)
    elif rel.endswith((".zip", ".jar")):
        with zipfile.ZipFile(path, "r") as zf:
            extract_zip(zf, out, rel, entry_rules)
    else:
        raise ArtifactsError(f"unsupported archive format: {rel}")
