"""A step's files, with links and sizes, on the build page.

An annotation is a convenience for people looking at the build, so a failure
to create one is logged and never fails the upload.
"""

import os
import subprocess

from .layout import key_join
from .log import fmt_size, log


def create_buildkite_annotation(artifacts_domain, prefix, files_with_sizes, title):
    """Annotate the job with ``title`` and a link to every file under ``prefix``."""
    if not os.environ.get("BUILDKITE"):
        log(
            "  BUILDKITE env var not set; skipping adding Buildkite annotation, not running in Buildkite?"
        )
        return
    if not artifacts_domain:
        log("  Artifacts domain not configured; skipping adding Buildkite annotation")
        return
    file_count = len(files_with_sizes)
    total_bytes = sum(size for _, size in files_with_sizes)
    log(f"  creating Buildkite annotation for {file_count} artifact(s)")
    job_label = os.environ.get("BUILDKITE_LABEL", "unknown job")
    uri = f"https://{artifacts_domain}"
    body = (
        f"## {job_label}\n### {title} ({file_count} files, {fmt_size(total_bytes)} total):\n\n"
    ) + "\n".join(
        f"- [{path}]({key_join(uri, prefix, path)}) ({fmt_size(size)})"
        for path, size in files_with_sizes
    )

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
        "artifacts",
        "--style",
        "info",
        "--priority",
        "10",
        "--scope",
        "job",
    ]
    log(f"  running command: {' '.join(args)}")
    try:
        subprocess.run(args, check=True)
    except (OSError, subprocess.SubprocessError) as e:
        log(f"  :warning Failed to create Buildkite annotation: {e}")
