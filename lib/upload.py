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
from .annotation import create_buildkite_annotation
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


def check_below(files, base_path, base_dir):
    """Every file must lie below ``base_dir``, whose path its object key drops."""
    for f in files:
        try:
            f.relative_to(base_path)
        except ValueError:
            raise ArtifactsError(f"file {f} is not under base_dir {base_dir}") from None


def kept_files(files, base_path, exclude_patterns):
    """``files`` less those ``exclude_patterns`` drop by their path below ``base_path``."""
    return [
        f for f in files if not is_excluded(f.relative_to(base_path).as_posix(), exclude_patterns)
    ]


def empty_upload(why, allow_empty):
    """An upload without files: skipped with allow_empty, else an error."""
    if not allow_empty:
        raise ArtifactsError(why)
    log(f"{why}; skipping artifact upload (allow_empty=true)")
    return False


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
    base_path = Path(base_dir).resolve() if base_dir else Path.cwd().resolve()
    if base_dir and not base_path.is_dir():
        raise ArtifactsError(f"--base-dir {base_dir!r} does not exist or is not a directory")
    matched = matched_files(patterns)
    if not matched:
        return empty_upload(f"no files matched: {patterns}", allow_empty)
    if base_dir:
        check_below(matched, base_path, base_dir)
    files = kept_files(matched, base_path, exclude_patterns)
    if not files:
        why = f"no files left after applying exclude patterns: {exclude_patterns or []}"
        return empty_upload(why, allow_empty)

    own = key_of_build()
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    store, dest_prefix = destination_store(cfg, auth)
    bucket = store.bucket
    location = layout.step_folder(dest_prefix, project, own.key, folder)
    tc = transfer.transfer_config(concurrency)
    relative = [(f.relative_to(base_path).as_posix(), f.stat().st_size) for f in files]
    total_bytes = sum(size for _, size in relative)

    def upload_one(f):
        rel = f.relative_to(base_path).as_posix()
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

    elapsed = []

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
        elapsed.append(transfer.run(upload_one, files, parallel))

    held = capture_step(store, location, write_files, captured(relative, uploaded))
    if held is not None:
        log_promoted(folder, own.key, held)
        shown = [(f.path, f.size) for f in held.files]
        title = f"Artifacts promoted by build {held.promoted.build.number}"
    else:
        shown, title = relative, "Uploaded artifacts"
    if annotate:
        create_buildkite_annotation(cfg.artifacts_domain, location, shown, title)
    else:
        log("  annotate=false; skipping Buildkite annotation")
    if held is not None:
        return False
    transfer.log_done("Uploaded", len(files), total_bytes, elapsed[0])
    return True
