"""Upload: capture a step's files in its folder at the build's key.

    <prefix>/<project>/<key>/<platform>/<step id>/<files>, manifest.json

The upload writes the files and then the manifest, which names them and the build that
uploaded them, unless a promote holds the folder. It reads the manifest before the files and
again right before it writes its own, so an upload that began before a promote does not undo
it. An upload into a promoted folder writes nothing and counts as empty, so the promote of its
step does not run. In a build without a plan step it first creates ``key.json`` of the key the
job computed.
"""

from __future__ import annotations

import glob
from pathlib import Path

from . import layout, transfer
from .annotation import annotate_upload
from .config import WRITE
from .errors import ArtifactsError
from .job import Job
from .key import create_key_json, own_key
from .log import fmt_size, log
from .manifest import Manifest, Uploaded, captured, is_made, read_manifest_at, write_manifest
from .objects import Opened, open_destination
from .options import Options, Upload, uploaded_folder
from .rules import is_excluded


def matched_files(patterns) -> list[Path]:
    """The files ``patterns`` match below the checkout, sorted."""
    found = {Path(f).resolve() for p in patterns for f in glob.glob(p, recursive=True)}
    return sorted(f for f in found if f.is_file())


def base_folder(base_dir: str | None) -> Path:
    """The folder whose path the object keys drop: ``base_dir``, else the checkout."""
    if not base_dir:
        return Path.cwd().resolve()
    base_path = Path(base_dir).resolve()
    if not base_path.is_dir():
        raise ArtifactsError(f"--base-dir {base_dir!r} does not exist or is not a directory")
    return base_path


def below(f: Path, base_path: Path, base_dir: str | None) -> str:
    """The path of ``f`` below ``base_path``, which its object key keeps."""
    try:
        return f.relative_to(base_path).as_posix()
    except ValueError:
        raise ArtifactsError(f"file {f} is not under base_dir {base_dir}") from None


def empty_upload(why: str, allow_empty: bool) -> list:
    """An upload without files: nothing to upload with allow_empty, else an error."""
    if not allow_empty:
        raise ArtifactsError(why)
    log(f"{why}; skipping artifact upload (allow_empty=true)")
    return []


def selected_files(u: Upload) -> list[tuple[Path, str]]:
    """Each file to upload with its path below the base folder: those the patterns match, less
    those an exclude drops by that path. None to upload fails, unless ``allow_empty``."""
    base_path = base_folder(u.base_dir)
    matched = matched_files(u.files)
    if not matched:
        return empty_upload(f"no files matched: {list(u.files)}", u.allow_empty)
    relative = [(f, below(f, base_path, u.base_dir)) for f in matched]
    kept = [(f, rel) for f, rel in relative if not is_excluded(rel, u.exclude)]
    if not kept:
        why = f"no files left after applying exclude patterns: {list(u.exclude)}"
        return empty_upload(why, u.allow_empty)
    return kept


def capture_step(store, location: str, write_files, manifest: Manifest) -> Manifest | None:
    """Write a step's files (``write_files()``), then its manifest, unless a promote holds the
    folder; the promoted manifest that stopped it, before the files or after them, else None.

    A promote that lands after the second read is overwritten by this manifest: the next build
    of the key builds the step again.
    """
    held = read_manifest_at(store, location)
    if is_made(held):
        return held
    write_files()
    held = read_manifest_at(store, location)
    if is_made(held):
        return held
    write_manifest(store, location, manifest)
    return None


def annotated(held: Manifest | None, relative) -> tuple[str, list[tuple[str, int]]]:
    """The title of the upload's annotation and the files it links: those of the build that
    promoted the folder when it ``held`` it, else those this upload wrote."""
    if held is None:
        return "Uploaded artifacts", relative
    files = [(f.path, f.size) for f in held.files]
    return f"Artifacts promoted by build {held.promoted.build.number}", files


def put_file(at: Opened, location: str, tc, f: Path, rel: str) -> None:
    """Upload ``f`` as ``rel`` below ``location``; a browser saves it under its own name."""
    log(f"  {rel} ({fmt_size(f.stat().st_size)})")
    transfer.one_file(
        at.cfg,
        rel,
        lambda client: client.upload_file(
            str(f),
            at.store.bucket,
            layout.key_join(location, rel),
            ExtraArgs={"ContentDisposition": f'attachment; filename="{f.name}"'},
            Config=tc,
        ),
    )


def put_files(at: Opened, location: str, u: Upload, files: list[tuple[Path, str]]) -> None:
    """Upload ``files`` below ``location``, ``u.parallel`` at a time, and log what was done."""
    total = sum(f.stat().st_size for f, _ in files)
    log(
        f"Uploading {len(files)} artifact(s) ({fmt_size(total)}) to "
        f"s3://{at.store.bucket}/{location}/ via backend={at.cfg.backend}"
    )
    transfer.log_settings(u.parallel, u.concurrency)
    if u.exclude:
        log(f"  exclude={list(u.exclude)}")
    tc = transfer.transfer_config(u.concurrency)
    seconds = transfer.run(lambda x: put_file(at, location, tc, *x), files, u.parallel)
    transfer.log_done("Uploaded", len(files), total, seconds)


def upload(job: Job, o: Options) -> bool:
    """Capture the files of the step's upload block at the build's key; True when it wrote
    them, False when there were none or a promote holds the folder."""
    u = o.upload
    files = selected_files(u)
    if not files:
        return False
    # Asked only now: an upload that allow_empty skips asks git nothing.
    own = own_key(job, o.key)
    folder = uploaded_folder(o)
    at = open_destination(WRITE)
    location = layout.step_folder(at.store.prefix, job.project, own.key, folder)
    relative = [(rel, f.stat().st_size) for f, rel in files]

    def write_files():
        if own.stands_for is not None:
            create_key_json(at.store, own.key, own.stands_for)
        put_files(at, location, u, files)

    manifest = captured(relative, Uploaded(job.branch, job.build))
    held = capture_step(at.store, location, write_files, manifest)
    if held is not None:
        b = held.promoted.build
        log(
            f"The files of {folder} at {own.key} were promoted by build {b.number} ({b.url}), "
            "so this upload writes nothing."
        )
    if u.annotate:
        title, shown = annotated(held, relative)
        annotate_upload(at.cfg.artifacts_domain, location, shown, title)
    else:
        log("  annotate=false; skipping Buildkite annotation")
    return held is None
