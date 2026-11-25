"""Download: find the steps of each entry, take the files their manifests name, fetch them.

Every entry is resolved before a file travels, so a step that cannot be found fails the job
before it downloads. "No file matched" is judged over a whole entry: it fails only when no
step gives a file. Steps are fetched in the order of their ids, so of two files of one path
the last stays. The log names the build each step is taken of as soon as the step is found;
after the last entry the build page names them all.
"""

from __future__ import annotations

import functools
import os
import shutil
import tempfile
from pathlib import Path

from . import transfer
from .annotation import annotate_downloads
from .extract import extract_local_archive
from .job import Job
from .key import own_key
from .log import fmt_size, log
from .objects import open_destination
from .options import CONCURRENCY, PARALLEL, Download, Entry, Options
from .resolve import (
    ResolvedArtifact,
    ResolvedStep,
    entry_name,
    entry_steps,
    log_step,
    nothing_matched,
    resolve_artifacts,
    selected,
)
from .rules import parse_rules


def shown_rules(entry_rules) -> str:
    """The entry filters and stripped prefixes of an extracted archive, for its log line."""
    extras = [
        text
        for entry_filter, strip_prefix in entry_rules
        for text in (
            f"filter={entry_filter}" if entry_filter else None,
            f"strip={strip_prefix}" if strip_prefix else None,
        )
        if text
    ]
    return f"  ({', '.join(extras)})" if extras else ""


def fetch_extracted(client, bucket: str, obj: ResolvedArtifact, out: Path, tc) -> None:
    """Download the archive ``obj`` to a temporary file in ``out``, extract it, remove it."""
    with tempfile.NamedTemporaryFile(dir=out, suffix=".tmp", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        client.download_file(bucket, obj.key, tmp_path, Config=tc)
        extract_local_archive(tmp_path, obj.rel, out, obj.entry_rules)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def fetch_file(client, bucket: str, obj: ResolvedArtifact, out: Path, tc) -> None:
    dest = out / obj.rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    client.download_file(bucket, obj.key, str(dest), Config=tc)


def fetch(cfg, bucket, objects, output_dir, extract, parallel, concurrency):
    """Download ``objects``, and extract archives with ``extract``, into ``output_dir``."""
    tc = transfer.transfer_config(concurrency)
    out = Path(output_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(obj.size for obj in objects)
    log(
        f"Downloading {len(objects)} artifact(s) ({fmt_size(total_bytes)}) via backend={cfg.backend}"
    )
    transfer.log_settings(parallel, concurrency)
    fetch_one = fetch_extracted if extract else fetch_file

    def download_one(obj):
        log(f"  start: {obj.rel} ({fmt_size(obj.size)})")
        elapsed = transfer.one_file(
            cfg, obj.rel, lambda client: fetch_one(client, bucket, obj, out, tc)
        )
        speed = fmt_size(obj.size / elapsed if elapsed > 0 else 0)
        # rel is repeated so that lines of parallel transfers can be told apart.
        where = f"extracted into {out}/{shown_rules(obj.entry_rules)}" if extract else out / obj.rel
        log(f"  done : {obj.rel} -> {where}  [{elapsed:.1f}s, {speed}/s]")

    elapsed = transfer.run(download_one, sorted(objects, key=lambda obj: obj.key), parallel)
    transfer.log_done("Downloaded", len(objects), total_bytes, elapsed)


def download_artifacts(
    cfg,
    project_id,
    variant,
    step,
    build,
    patterns,
    exclude_patterns=None,
    output_dir=".",
    extract=False,
    parallel=PARALLEL,
    concurrency=CONCURRENCY,
):
    """``resolve_artifacts``, then download what it selected into ``output_dir``, extracting
    archives with ``extract``. Returns what ``resolve_artifacts`` returns, after all transfers.
    Creates the output directory if needed and does not clean it."""
    bucket, resolved, objects = resolve_artifacts(
        cfg, project_id, variant, step, build, patterns, exclude_patterns
    )
    fetch(cfg, bucket, objects, output_dir, extract, parallel, concurrency)
    return bucket, resolved, objects


def taken(
    store, n: int, e: Entry, job: Job, own_key
) -> list[tuple[ResolvedStep, list[ResolvedArtifact]]]:
    """Each step the ``n``-th entry ``e`` takes, with its files that match."""
    name = entry_name(n, e.project_id or job.project, e.variant)
    steps = entry_steps(store, e, job, own_key, name)
    for s in steps:
        log_step(name, s)
    rules = parse_rules(e.files)
    found = [(s, selected(s, rules, e.exclude)) for s in steps]
    if not any(files for _, files in found):
        raise nothing_matched(name, e.files, e.exclude, steps)
    return found


def clean_outputs(d: Download) -> None:
    """Remove the output folder of every entry, each once."""
    for out in {Path(e.output_dir).resolve() for e in d.projects}:
        if out.exists():
            log(f"Cleaning {out}")
            shutil.rmtree(out)


def download(job: Job, o: Options) -> None:
    """Download every entry of the step's download block, then annotate the build with every
    step taken."""
    d = o.download
    if d.clean:
        clean_outputs(d)
    at = open_destination()
    own = functools.cache(lambda: own_key(job, o.key).key)
    found = [taken(at.store, n, e, job, own) for n, e in enumerate(d.projects, start=1)]
    parallel, concurrency = int(d.parallel), int(d.concurrency)
    for e, steps in zip(d.projects, found):
        for objects in (files for _, files in steps if files):
            bucket = at.store.bucket
            fetch(at.cfg, bucket, objects, e.output_dir, e.extract, parallel, concurrency)
    annotate_downloads([s for steps in found for s, _ in steps])
