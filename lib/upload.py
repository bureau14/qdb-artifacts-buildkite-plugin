"""Upload: capture a step's files in its folder at the build's key.

    <prefix>/<project>/<key>/<platform>/<step id>/<files>, manifest.json

The upload selects the files, then captures them: it writes them and then the
manifest, which names them and the build that uploaded them, unless a promote
holds the folder. It reads the manifest before the files and again right
before it writes its own, so an upload that began before a promote does not
undo it. An upload into a promoted folder writes nothing and counts as empty,
so the promote of its step does not run. In a build without a plan step it
first creates ``key.json`` of the key the job computed.
"""

import glob
from pathlib import Path

from . import layout, transfer
from .annotation import annotate_upload
from .clients import aws_clients, resolve_object_auth
from .config import load_store_config
from .errors import ArtifactsError
from .key import create_key_json
from .log import fmt_size, log
from .manifest import captured, is_made, read_manifest_at, write_manifest
from .objects import destination_store
from .rules import is_excluded


def matched_files(patterns):
    """The files ``patterns`` match below the checkout, sorted."""
    return sorted(
        {
            Path(f).resolve()
            for p in patterns
            for f in glob.glob(p, recursive=True)
            if Path(f).is_file()
        }
    )


def base_folder(base_dir):
    """The folder whose path the object keys of the files drop: ``base_dir``, else the
    checkout."""
    if not base_dir:
        return Path.cwd().resolve()
    base_path = Path(base_dir).resolve()
    if not base_path.is_dir():
        raise ArtifactsError(f"--base-dir {base_dir!r} does not exist or is not a directory")
    return base_path


def check_below(files, base_path, base_dir):
    """Every file must lie below ``base_dir``, whose path its object key drops."""
    for f in files:
        try:
            f.relative_to(base_path)
        except ValueError:
            raise ArtifactsError(f"file {f} is not under base_dir {base_dir}") from None


def empty_upload(why, allow_empty):
    """An upload without files: nothing to upload with allow_empty, else an error."""
    if not allow_empty:
        raise ArtifactsError(why)
    log(f"{why}; skipping artifact upload (allow_empty=true)")
    return []


def selected_files(patterns, base_dir, exclude_patterns, allow_empty):
    """Each file to upload with its path below ``base_dir``, else below the checkout: those
    ``patterns`` match, less those ``exclude_patterns`` drop by that path. None to upload
    fails, unless ``allow_empty``."""
    base_path = base_folder(base_dir)
    matched = matched_files(patterns)
    if not matched:
        return empty_upload(f"no files matched: {patterns}", allow_empty)
    if base_dir:
        check_below(matched, base_path, base_dir)
    below = [(f, f.relative_to(base_path).as_posix()) for f in matched]
    kept = [(f, rel) for f, rel in below if not is_excluded(rel, exclude_patterns)]
    if not kept:
        why = f"no files left after applying exclude patterns: {exclude_patterns or []}"
        return empty_upload(why, allow_empty)
    return kept


def capture_step(store, location, write_files, manifest):
    """Write a step's files (``write_files()``), then its manifest, unless a promote holds the
    folder; the promoted manifest that stopped it, before the files or after them, else None.

    A promote that lands after the second read is overwritten by this manifest,
    which says captured: the next build of the key builds the step again.
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


def log_promoted(folder, key, held):
    b = held.promoted.build
    log(
        f"The files of {folder} at {key} were promoted by build {b.number} ({b.url}), so this "
        "upload writes nothing."
    )


def annotated(held, relative):
    """The title of the upload's annotation and the files it links (path and size): those of
    the build that promoted the folder when it ``held`` it, else those this upload wrote."""
    if held is None:
        return "Uploaded artifacts", relative
    files = [(f.path, f.size) for f in held.files]
    return f"Artifacts promoted by build {held.promoted.build.number}", files


def upload(
    project,
    key_of_build,
    folder,
    uploaded,
    patterns,
    parallel=4,
    concurrency=32,
    base_dir=None,
    annotate=True,
    exclude_patterns=None,
    allow_empty=False,
):
    """Capture the files ``patterns`` match as the step ``folder`` (platform and step id) of
    ``project`` at the build's key; True when it wrote them, False when it wrote nothing.

    Paths relative to the checkout, or to ``base_dir``, become the object key
    suffix below the folder. ``key_of_build()`` gives the build's key, and the
    ``key.json`` to create when the job computed it; it is called once there
    are files, so an upload that allow_empty skips asks git nothing.
    ``uploaded`` is the build and branch the manifest names.

    1. Select the files; none, with allow_empty, is an empty upload.
    2. A promoted folder: log who promoted it, annotate its files, write nothing.
    3. Without a plan step, create key.json.
    4. Transfer the files, read the manifest again, write it.
    5. Annotate the build with links to the files.

    Creds are minted once before the pool -- for R2, ensure TTL covers the full upload.
    """
    files = selected_files(patterns, base_dir, exclude_patterns, allow_empty)
    if not files:
        return False

    own = key_of_build()
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    store, dest_prefix = destination_store(cfg, auth)
    bucket = store.bucket
    location = layout.step_folder(dest_prefix, project, own.key, folder)
    tc = transfer.transfer_config(concurrency)
    relative = [(rel, f.stat().st_size) for f, rel in files]
    total_bytes = sum(size for _, size in relative)

    def upload_one(item):
        f, rel = item
        log(f"  {rel} ({fmt_size(f.stat().st_size)})")

        def attempt(client):
            client.upload_file(
                str(f),
                bucket,
                layout.key_join(location, rel),
                ExtraArgs={"ContentDisposition": f'attachment; filename="{f.name}"'},
                Config=tc,
            )

        transfer.one_file(cfg, auth, rel, attempt)

    def write_files():
        if own.stands_for is not None:
            create_key_json(store, dest_prefix, own.key, own.stands_for)
        log(
            f"Uploading {len(files)} artifact(s) ({fmt_size(total_bytes)}) to "
            f"s3://{bucket}/{location}/ via backend={cfg.backend}"
        )
        transfer.log_settings(parallel, concurrency)
        if exclude_patterns:
            log(f"  exclude={exclude_patterns}")
        seconds = transfer.run(upload_one, files, parallel)
        transfer.log_done("Uploaded", len(files), total_bytes, seconds)

    held = capture_step(store, location, write_files, captured(relative, uploaded))
    if held is not None:
        log_promoted(folder, own.key, held)
    if annotate:
        title, shown = annotated(held, relative)
        annotate_upload(cfg.artifacts_domain, location, shown, title)
    else:
        log("  annotate=false; skipping Buildkite annotation")
    return held is None
