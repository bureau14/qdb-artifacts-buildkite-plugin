"""Download: find the steps of each entry, take the files their manifests name, fetch them.

An entry takes one or several steps of a project's platform; ``resolve``
finds each at its key. "No file matched" is judged over the whole entry: it
fails only when no step gives a file. Steps are fetched in the order of their
ids, so of two files of one path the last stays. The files travel through
``transfer``, as uploads do.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import transfer
from .clients import aws_clients, resolve_object_auth
from .config import load_store_config
from .extract import extract_local_archive
from .layout import Folder
from .log import fmt_size, log
from .objects import destination_store
from .resolve import (
    entry_name,
    keyed_step,
    log_step,
    nothing_matched,
    resolve_artifacts,
    selected,
    walk_named,
    walk_newest,
)
from .rules import parse_rules


@dataclass(frozen=True)
class Entry:
    """A download entry as the job reads it.

    ``keys`` are the keys the plan step wrote, by step id; ``steps`` the step
    ids the entry names, none when it names none; ``own_key`` the build's key
    for an entry of the build's own project and branch, else None.
    """

    project: str
    variant: str
    files: tuple[str, ...]
    exclude: tuple[str, ...]
    output_dir: str
    extract: bool
    keys: Mapping[str, str]
    steps: tuple[str, ...]
    git_ref: str | None
    strict: bool
    own_key: str | None


def fetch(cfg, auth, bucket, objects, output_dir=".", extract=False, parallel=4, concurrency=32):
    """Download ``objects``, and extract archives with ``extract``, into ``output_dir``.

    With extract, an archive is downloaded to a temp file through boto3's
    transfer manager (parallel ranged GETs), extracted, then removed.
    """
    tc = transfer.transfer_config(concurrency)
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)

    total_bytes = sum(obj.size for obj in objects)
    log(
        f"Downloading {len(objects)} artifact(s) ({fmt_size(total_bytes)}) via backend={cfg.backend}"
    )
    transfer.log_settings(parallel, concurrency)

    def download_one(obj):
        key, rel, sz, entry_rules = obj.key, obj.rel, obj.size, obj.entry_rules
        log(f"  start: {rel} ({fmt_size(sz)})")

        def attempt(client):
            if extract:
                with tempfile.NamedTemporaryFile(dir=out, suffix=".tmp", delete=False) as tmp:
                    tmp_path = tmp.name
                try:
                    client.download_file(bucket, key, tmp_path, Config=tc)
                    extract_local_archive(tmp_path, rel, out, entry_rules=entry_rules)
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
            else:
                dest = out / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                client.download_file(bucket, key, str(dest), Config=tc)

        elapsed = transfer.one_file(cfg, auth, rel, attempt)
        speed = sz / elapsed if elapsed > 0 else 0
        # Loud destination line. rel is repeated so parallel-interleaved logs are still parseable.
        if extract:
            extras = []
            for entry_filter, strip_prefix in entry_rules:
                if entry_filter:
                    extras.append(f"filter={entry_filter}")
                if strip_prefix:
                    extras.append(f"strip={strip_prefix}")
            extra_str = f"  ({', '.join(extras)})" if extras else ""
            log(
                f"  done : {rel} -> extracted into {out}/{extra_str}  "
                f"[{elapsed:.1f}s, {fmt_size(speed)}/s]"
            )
        else:
            log(f"  done : {rel} -> {out / rel}  [{elapsed:.1f}s, {fmt_size(speed)}/s]")

    elapsed = transfer.run(download_one, sorted(objects, key=lambda obj: obj.key), parallel)
    transfer.log_done("Downloaded", len(objects), total_bytes, elapsed)


def download_artifacts(
    cfg,
    auth,
    project_id,
    variant,
    step,
    patterns,
    git_ref=None,
    key=None,
    exclude_patterns=None,
    output_dir=".",
    extract=False,
    require_strict_ref_match=False,
    parallel=4,
    concurrency=32,
):
    """Find one step of a project's platform, as ``resolve_artifacts`` does, and download its
    matching artifacts into ``output_dir``, extracting archives with ``extract``.

    Uses explicit cfg and auth; no Buildkite environment or SSM configuration
    is required. It creates the output directory if needed but does not clean
    it. Returns (bucket, ResolvedStep, objects) after all transfers complete,
    so a caller can record the step and artifacts without resolving them again.
    """
    bucket, resolved, objects = resolve_artifacts(
        cfg=cfg,
        auth=auth,
        project_id=project_id,
        variant=variant,
        step=step,
        patterns=patterns,
        git_ref=git_ref,
        key=key,
        exclude_patterns=exclude_patterns,
        require_strict_ref_match=require_strict_ref_match,
    )
    fetch(cfg, auth, bucket, objects, output_dir, extract, parallel, concurrency)
    return bucket, resolved, objects


def entry_steps(store, prefix, e: Entry, name: str):
    """The steps entry ``e`` takes, in the order of their ids.

    The plan step's keys; else, for an entry of the build's own project and
    branch, the steps it names at the build's key; else the walk, for each
    step it names, or for the newest key of its platform when it names none.
    """
    if e.keys:
        return [
            keyed_step(store, prefix, e.project, key, Folder(e.variant, step), name)
            for step, key in sorted(e.keys.items())
        ]
    if e.own_key is not None:
        return [
            keyed_step(store, prefix, e.project, e.own_key, Folder(e.variant, step), name)
            for step in sorted(e.steps)
        ]
    if e.steps:
        return [
            walk_named(store, prefix, e.project, e.git_ref, e.strict, Folder(e.variant, step))
            for step in sorted(e.steps)
        ]
    return walk_newest(store, prefix, e.project, e.git_ref, e.strict, e.variant)


def download(entries, dirs_to_clean, parallel=4, concurrency=32):
    """Download every entry: its steps, the files of each that match, fetched step by step."""
    for d in dirs_to_clean:
        if d.exists():
            log(f"Cleaning {d}")
            shutil.rmtree(d)

    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-only")
    store, prefix = destination_store(cfg, auth)
    for n, e in enumerate(entries, start=1):
        name = entry_name(n, e.project, e.variant)
        steps = entry_steps(store, prefix, e, name)
        rules = parse_rules(e.files)
        taken = [(s, selected(s, rules, e.exclude)) for s in steps]
        if not any(objects for _, objects in taken):
            raise nothing_matched(name, e.files, e.exclude, steps)
        for s, objects in taken:
            log_step(name, s, objects)
            if objects:
                fetch(
                    cfg, auth, store.bucket, objects, e.output_dir, e.extract, parallel, concurrency
                )
