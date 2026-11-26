"""Promote: make a step's captured files at the build's key official, then point the branch's
latest file at that key.

A promote reads the manifest of the folder it promotes. Captured, it writes it back once,
naming this build, its branch and the time, then writes the branch's latest file: its text is
the key, and the last writer wins. Promoted already, by any build, it leaves it and writes no
latest file, so the retry of a job that promoted never moves a latest file back. Not there, it
fails: the step that uploads it uploaded nothing.

A job writes latest files only when the plan of its build says so
(``Job.writes_latest_files``).
"""

from __future__ import annotations

import dataclasses
import time

from . import layout
from .config import WRITE
from .errors import ArtifactsError
from .forms import branch_ref, iso_utc
from .job import Job
from .key import own_key
from .layout import Folder
from .log import log
from .manifest import Manifest, Promoted, read_manifest_at, write_manifest
from .objects import open_destination
from .options import Options, promoted_folder


def write_latest_file(store, project: str, branch: str, folder: Folder, key: str) -> None:
    """Point the latest file of ``folder`` of ``project`` on ``branch`` at ``key``."""
    where = layout.latest_file(store.prefix, project, branch_ref(branch), folder)
    store.put(where, key.encode(), "text/plain")


def promote_folder(
    store, project: str, key: str, folder: Folder, promotion: Promoted, writes: bool
) -> Manifest:
    """Promote ``folder`` of ``project`` at ``key`` as ``promotion`` says, unless a build
    promoted it already; the manifest that holds afterwards. The branch's latest file moves
    only when ``writes``. A folder without a manifest raises."""
    location = layout.step_folder(store.prefix, project, key, folder)
    held = read_manifest_at(store, location)
    if held is None:
        raise ArtifactsError(
            f"This step promotes {folder} at {key}, and no upload of that step is there: "
            f"{location}/{layout.MANIFEST} does not exist. The step that uploads "
            f"{folder.step} for {folder.platform} uploaded nothing in this build."
        )
    if held.promoted is not None:
        return held
    made = dataclasses.replace(held, promoted=promotion)
    write_manifest(store, location, made)
    if writes:
        write_latest_file(store, project, promotion.branch, folder, key)
    return made


def promote(job: Job, o: Options) -> None:
    """Promote the folder the step's promote block names, at the build's key."""
    key, folder = own_key(job, o.key).key, promoted_folder(o)
    store = open_destination(WRITE).store
    promotion = Promoted(iso_utc(time.time()), job.branch, job.build)
    writes = job.writes_latest_files
    held = promote_folder(store, job.project, key, folder, promotion, writes)
    if held.promoted != promotion:
        b = held.promoted.build
        log(f"{folder} at {key} was promoted by build {b.number} ({b.url}); left as it is")
    elif writes:
        log(f"Promoted {folder} at {key}; the latest file of {job.branch} names it")
    else:
        log(
            f"Promoted {folder} at {key}; no latest file of {job.branch} names this key: its "
            "plan does not show that a build of its branch would be at this commit and take "
            "every dependency at the same commits"
        )
