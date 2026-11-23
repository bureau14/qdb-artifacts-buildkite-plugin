"""What each hook phase does, decided from the step's options:

    pre-command   fail a step that uploads or promotes without a step id, before its command;
                  download the step's download entries
    post-command  the plan step, its second run and the last step run their own command and
                  nothing else; any other step uploads, then promotes when its command passed
                  and the upload was not empty

An ArtifactsError ends the phase with "[artifacts] ERROR: <message>" and exit status 1.
"""

from __future__ import annotations

import os
import sys

import dacite

from .download import download
from .errors import ArtifactsError
from .job import Job, job_of
from .options import Options, job_options, step_id_problems
from .promote import promote
from .upload import upload


def section(title: str) -> None:
    """Open a collapsible section of the job log. Flushed at once, because the agent shows it
    among the progress lines that go to stderr."""
    print(f"~~~ {title}", flush=True)


def pre_command(job: Job, o: Options) -> int:
    for problem in step_id_problems(o):
        raise ArtifactsError(f"This step {problem}")
    if o.download and o.download.projects:
        section(":arrow_down: Downloading artifacts")
        download(job, o)
    return 0


def run_plan_side(job: Job, o: Options) -> int:
    """The command of the plan step, its second run or the last step. Imported here: the
    planning side needs PyYAML, which only these steps install."""
    from .planning.edge import real_edge
    from .planning.last_step import last_step
    from .planning.plan_step import plan_step, plan_step_again

    if o.leave:
        section(":camera_with_flash: Snapshot: the last step")
        return last_step(job, real_edge())
    if o.plan:
        section(":camera_with_flash: Snapshot: the plan step")
        return plan_step(job, real_edge(), o.plan)
    section(":camera_with_flash: Snapshot: the plan step's second run")
    return plan_step_again(job, real_edge(), o.resume)


def post_command(job: Job, o: Options) -> int:
    """An upload runs also after a failed command: the files of a failed build are evidence.
    Promoting says "this build is good to use", so it needs a passed command and an upload
    that was not empty."""
    if o.plan or o.resume or o.leave:
        return run_plan_side(job, o)
    if o.upload and o.upload.files:
        section(":arrow_up: Uploading artifacts")
        if not upload(job, o):
            section("Skipping artifact promotion (empty upload)")
            return 0
    if not o.promote:
        return 0
    if job.exit_status != 0:
        section(f":warning: Skipping artifact promotion (command exited {job.exit_status})")
        return 0
    section(":pushpin: Promoting artifacts")
    promote(job, o)
    return 0


PHASES = {"pre-command": pre_command, "post-command": post_command}


def main() -> int:
    argv = sys.argv[1:]
    try:
        if len(argv) != 1 or argv[0] not in PHASES:
            raise ArtifactsError(f"expected one of {sorted(PHASES)}, got {argv}")
        return PHASES[argv[0]](job_of(os.environ), job_options(os.environ))
    except dacite.DaciteError as error:
        print(f"[artifacts] ERROR: the plugin's options: {error}", file=sys.stderr)
        return 1
    except ArtifactsError as error:
        print(f"[artifacts] ERROR: {error}", file=sys.stderr)
        return 1
