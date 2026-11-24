"""What a job shows on the build page: the files a step uploaded, with links and sizes, and
the build each download took a step of.

Each is an annotation of the job under a context of its own, so that a job that downloads and
uploads keeps both. An annotation is a convenience for people looking at the build, so a
failure to create one is logged and never fails the job.
"""

import os
import subprocess
from collections import namedtuple

from .forms import name_of, short_commit
from .key import commit_of
from .layout import key_join
from .log import fmt_size, log
from .markdown import build_link, code, codes, collapsed, table
from .resolve import made_by

UPLOAD_CONTEXT = "artifacts"
DOWNLOAD_CONTEXT = "artifacts-downloads"

# One row of the downloads table; ``build`` is the cell that names the build.
Row = namedtuple("Row", "project platform ref key build steps")


def annotate(body, context):
    """Annotate the job with ``body`` under ``context``, as information."""
    if not os.environ.get("BUILDKITE"):
        log(
            "  BUILDKITE env var not set; skipping adding Buildkite annotation, not running in Buildkite?"
        )
        return
    agent = "buildkite-agent.exe" if os.name == "nt" else "buildkite-agent"
    options = ["--context", context, "--style", "info", "--priority", "10", "--scope", "job"]
    args = [agent, "annotate", body, *options]
    # The body is on the build page; the log names where.
    log(f"  annotating the job under the context {context}")
    try:
        subprocess.run(args, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"  :warning Failed to create Buildkite annotation: {e}")


def annotate_upload(artifacts_domain, prefix, files_with_sizes, title):
    """Annotate the job with ``title`` and a link to every file under ``prefix``."""
    if not artifacts_domain:
        log("  Artifacts domain not configured; skipping adding Buildkite annotation")
        return
    file_count = len(files_with_sizes)
    total_bytes = sum(size for _, size in files_with_sizes)
    log(f"  creating Buildkite annotation for {file_count} artifact(s)")
    uri = f"https://{artifacts_domain}"
    body = f"**{title}** ({file_count} files, {fmt_size(total_bytes)} total)\n\n" + "\n".join(
        f"- [{path}]({key_join(uri, prefix, path)}) ({fmt_size(size)})"
        for path, size in files_with_sizes
    )
    annotate(body, UPLOAD_CONTEXT)


def taken_from(step):
    """The build a download took ``step`` of. A step of the own build that a later step
    promotes is only uploaded when the download runs."""
    by, build = made_by(step)
    return build_link(build) + ("" if by == "promoted" else ", not promoted yet")


def download_rows(steps):
    """The steps taken at one key of one platform of a project, side by side: what they share,
    then their step ids."""
    ids = {}
    for s in steps:
        shared = (s.project, s.folder.platform, s.ref, s.key, taken_from(s))
        ids.setdefault(shared, []).append(s.folder.step)
    return [Row(*shared, step_ids) for shared, step_ids in ids.items()]


def downloads_table(rows):
    """One row per project and platform. The branch whose latest file named a step is a column
    only when a download of the job named a branch."""
    columns = [
        ("Project", lambda r: code(r.project)),
        ("Platform", lambda r: code(r.platform)),
        ("Steps", lambda r: codes(r.steps)),
        ("Commit", lambda r: code(short_commit(commit_of(r.key)))),
        ("From", lambda r: r.build),
    ]
    if any(r.ref is not None for r in rows):
        columns.insert(3, ("Branch", lambda r: code(name_of(r.ref)) if r.ref else ""))
    return table(
        [heading for heading, _ in columns], [[cell(r) for _, cell in columns] for r in rows]
    )


def keys_table(rows):
    cells = [[code(r.project), code(r.platform), code(r.key)] for r in rows]
    return table(["Project", "Platform", "Key"], cells)


def downloads_annotation(steps):
    """Every step the job's downloads took, and each key behind a line that opens."""
    rows = download_rows(steps)
    keys = collapsed("Keys", keys_table(rows))
    return f"**Downloaded artifacts**\n\n{downloads_table(rows)}\n\n{keys}"


def annotate_downloads(steps):
    annotate(downloads_annotation(steps), DOWNLOAD_CONTEXT)
