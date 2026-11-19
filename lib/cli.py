"""What each hook phase does, decided from the step's options:

    pre-command   download the step's download projects
    post-command  a gate step runs its one gate command and nothing else; any
                  other step uploads, when the step names files; then
                  promotes, when the step names a variant to promote, its
                  command passed and the upload was not empty

The hooks find Python and start this module with the name of their phase. The
decisions are made here, in one process, so that whether to promote can depend
on what the upload did. An ArtifactsError of the library ends the phase with
"[artifacts] ERROR: <message>" and exit status 1.
"""

import sys

from .download import download
from .errors import ArtifactsError
from .options import (
    command_exit_status,
    download_arguments,
    gate_step,
    promote_arguments,
    promote_requested,
    upload_arguments,
    upload_requested,
)
from .promote import promote
from .upload import upload


def section(title):
    """Open a collapsible section of the job log. Flushed at once, because the
    agent shows it among the progress lines that go to stderr."""
    print(f"~~~ {title}", flush=True)


def pre_command():
    section(":arrow_down: Downloading artifacts")
    download(**download_arguments())
    return 0


def run_gate_step(step):
    """Run the gate command of a gate step: plan (enter), resume or leave.

    It runs also after a failed command, whose failure it records. The imports
    are here and not at the top: the planning half needs PyYAML, which only the
    steps of plan and resume install, and no other job loads it. Leave imports
    its own module only, which reads no document.
    """
    section(f":camera_with_flash: Snapshot {step.kind}")
    if step.kind == "leave":
        from .snapshot import leave

        return leave.run()
    from .snapshot import enter

    return enter.run(step.kind, step.pipeline)


def post_command():
    """A gate step's gate command; for any other step upload, then promote, each when due.

    An upload runs also after a failed command: the files of a failed build are
    evidence. Promoting says "this build is good to use", so it needs a passed
    command and an upload that was not empty.
    """
    step = gate_step()
    if step is not None:
        return run_gate_step(step)
    if upload_requested():
        section(":arrow_up: Uploading artifacts")
        if not upload(**upload_arguments()):
            section("Skipping artifact promotion (empty upload)")
            return 0
    if not promote_requested():
        return 0
    status = command_exit_status()
    if status != 0:
        section(f":warning: Skipping artifact promotion (command exited {status})")
        return 0
    section(":pushpin: Promoting artifacts")
    promote(**promote_arguments())
    return 0


PHASES = {"pre-command": pre_command, "post-command": post_command}


def main():
    argv = sys.argv[1:]
    try:
        if len(argv) != 1 or argv[0] not in PHASES:
            raise ArtifactsError(f"expected one of {sorted(PHASES)}, got {argv}")
        return PHASES[argv[0]]()
    except ArtifactsError as error:
        print(f"[artifacts] ERROR: {error}", file=sys.stderr)
        return 1
