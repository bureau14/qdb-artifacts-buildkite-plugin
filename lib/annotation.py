"""What a job shows on the build page: the files a step uploaded, with links and sizes, and
the build each download took a step of.

Each is an annotation of the job under a context of its own, so that a job that downloads and
uploads keeps both. An annotation is a convenience for people looking at the build, so a
failure to create one is logged and never fails the job.
"""

import os
import subprocess

from .forms import name_of
from .key import commit_of
from .layout import key_join
from .log import fmt_size, log
from .resolve import made_by

UPLOAD_CONTEXT = "artifacts"
DOWNLOAD_CONTEXT = "artifacts-downloads"


def annotate(body, context):
    """Annotate the job with ``body`` under ``context``, as information."""
    if not os.environ.get("BUILDKITE"):
        log(
            "  BUILDKITE env var not set; skipping adding Buildkite annotation, not running in Buildkite?"
        )
        return
    # We could use REST API calls here and avoid depending on the binary (and calling it from subprocess) but REST API has some limitations that make it less ideal:
    # - auth tokens are per user not per organization, token will stop working when account is removed
    # - API rate limits are low, you can hit them if you have a lot of builds uploading artifacts at the same time, would need a backoff and retry mechanism
    buildkite_agent_exec = "buildkite-agent"
    if os.name == "nt":
        buildkite_agent_exec += ".exe"
    args = [
        buildkite_agent_exec,
        "annotate",
        body,
        "--context",
        context,
        "--style",
        "info",
        "--priority",
        "10",
        "--scope",
        "job",
    ]
    # The body is on the build page; the log names where.
    log(f"  annotating the job under the context {context}")
    try:
        subprocess.run(args, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"  :warning Failed to create Buildkite annotation: {e}")


def job_heading():
    """The heading of a job's annotation: the job's label."""
    return f"## {os.environ.get('BUILDKITE_LABEL', 'unknown job')}"


def annotate_upload(artifacts_domain, prefix, files_with_sizes, title):
    """Annotate the job with ``title`` and a link to every file under ``prefix``."""
    if not artifacts_domain:
        log("  Artifacts domain not configured; skipping adding Buildkite annotation")
        return
    file_count = len(files_with_sizes)
    total_bytes = sum(size for _, size in files_with_sizes)
    log(f"  creating Buildkite annotation for {file_count} artifact(s)")
    uri = f"https://{artifacts_domain}"
    body = (
        f"{job_heading()}\n### {title} ({file_count} files, {fmt_size(total_bytes)} total):\n\n"
    ) + "\n".join(
        f"- [{path}]({key_join(uri, prefix, path)}) ({fmt_size(size)})"
        for path, size in files_with_sizes
    )
    annotate(body, UPLOAD_CONTEXT)


def download_line(step):
    """One step a download took, as the build page shows it: its project and folder, the
    branch whose latest file named it, its commit and key, and the build its manifest names."""
    branch = "" if step.ref is None else f" of branch `{name_of(step.ref)}`"
    by, build = made_by(step)
    return (
        f"- `{step.project}` `{step.folder}`{branch}: commit `{commit_of(step.key)}`, "
        f"key `{step.key}`, {by} by [build {build.number}]({build.url})"
    )


def annotate_downloads(steps):
    """Annotate the job with every step its downloads took, one line each."""
    count = f"{len(steps)} step" + ("" if len(steps) == 1 else "s")
    lines = "\n".join(download_line(s) for s in steps)
    annotate(f"{job_heading()}\n### Downloaded artifacts ({count}):\n\n{lines}", DOWNLOAD_CONTEXT)
