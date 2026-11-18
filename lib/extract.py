"""Unpack a downloaded archive into the output dir, refusing tar members that
would land outside it.

The download puts an archive in a temporary file in the output dir, through
boto3's transfer manager (parallel ranged GETs), and this module unpacks it
from there:

    .tar.gz             tarfile, seekable
    .tar.zst, .tar.zstd zstandard's stream reader into tarfile's stream mode
    .zip, .jar          zipfile, seekable
"""

import posixpath
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


def _assert_tar_path_is_safe(out, name):
    normalized = posixpath.normpath(name)
    if normalized in ("", ".") or normalized.startswith("../") or normalized == "..":
        raise tarfile.FilterError(f"refusing unsafe tar member path: {name}")
    if posixpath.isabs(normalized):
        raise tarfile.FilterError(f"refusing absolute tar member path: {name}")

    out = out.resolve()
    target = (out / normalized).resolve()
    try:
        target.relative_to(out)
    except ValueError as exc:
        raise tarfile.FilterError(f"refusing tar member outside output dir: {name}") from exc
    return target


def _safe_tar_member(member, out):
    """Apply Python 3.12-style data filtering without the 3.12-only
    TarFile.extract(..., filter=...) keyword.

    Python 3.9 agents do not accept that keyword, so pre-filter members and call
    extract() with the older signature. On Python versions with
    tarfile.data_filter, delegate to the stdlib implementation. Otherwise use a
    conservative fallback that rejects traversal/absolute paths, device files,
    and links escaping the output directory.
    """
    data_filter = getattr(tarfile, "data_filter", None)
    if data_filter is not None:
        return data_filter(member, str(out))

    target = _assert_tar_path_is_safe(out, member.name)
    if member.isdev():
        raise tarfile.FilterError(f"refusing special tar member: {member.name}")

    if member.issym() or member.islnk():
        if posixpath.isabs(member.linkname):
            raise tarfile.FilterError(f"refusing absolute tar link target: {member.name}")
        link_base = out.resolve() if member.islnk() else target.parent
        try:
            (link_base / member.linkname).resolve().relative_to(out.resolve())
        except ValueError as exc:
            raise tarfile.FilterError(
                f"refusing tar link outside output dir: {member.name}"
            ) from exc

    return member


def _extract_tar(tar, out, entry_filter=None, strip_prefix=None, entry_rules=None):
    """Extract tar members with optional filtering. Data filtering rejects
    absolute paths, traversal (../), and device files. Streaming mode means
    sequential-only access -- skipped members still consume I/O from the stream."""
    entry_rules = entry_rules or [(entry_filter, strip_prefix)]
    for member in tar:
        member = _filter_tar_member(member, entry_rules)
        if member is None:
            continue
        member = _safe_tar_member(member, out)
        if member is None:
            continue
        tar.extract(member, path=out)


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
