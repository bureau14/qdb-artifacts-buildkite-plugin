"""Promote: make a step's captured files at the build's key official, then point the branch's
latest file at that key.

A promote reads the manifest of the folder it promotes. Captured, it writes it
back once, naming this build, its branch and the time, then writes the latest
file of the branch: its text is the key, and the last writer wins. Promoted
already, by this build or another, it leaves it (the first promote wins) and
writes no latest file, so the retry of a job that promoted never moves a
branch's latest file back. Not there, it fails: the step that uploads it
uploaded nothing.

A build under a ``snapshot/`` name, which a plan step started for a
dependency, writes no latest file: that name is no branch.
"""

import time

from . import layout
from .clients import aws_clients, resolve_object_auth
from .config import load_store_config
from .errors import ArtifactsError
from .forms import BRANCH_PREFIX, iso_utc
from .log import log
from .manifest import Promoted, promoted_by, read_manifest_at, write_manifest
from .objects import destination_store

SNAPSHOT_BRANCH_PREFIX = "snapshot/"


def writes_latest(branch):
    """Whether a promote under ``branch`` writes its latest file."""
    return not branch.startswith(SNAPSHOT_BRANCH_PREFIX)


def promote_folder(store, prefix, project, key, folder, promotion):
    """Promote ``folder`` of ``project`` at ``key`` as ``promotion`` says, unless a build
    promoted it already; the manifest that holds afterwards. A folder without a manifest
    raises."""
    location = layout.step_folder(prefix, project, key, folder)
    held = read_manifest_at(store, location)
    if held is None:
        raise ArtifactsError(
            f"This step promotes {folder} at {key}, and no upload of that step is there: "
            f"{location}/{layout.MANIFEST} does not exist. The step that uploads "
            f"{folder.step} for {folder.platform} uploaded nothing in this build."
        )
    if held.promoted is not None:
        return held
    made = promoted_by(held, promotion)
    write_manifest(store, location, made)
    if writes_latest(promotion.branch):
        ref = BRANCH_PREFIX + promotion.branch
        store.put(layout.latest_file(prefix, project, ref, folder), key.encode(), "text/plain")
    return made


def promote(project, key, folder, branch, build):
    """Promote the step ``folder`` (platform and step id) of ``project`` at ``key`` for
    ``build``, which runs under ``branch``."""
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    store, prefix = destination_store(cfg, auth)

    promotion = Promoted(iso_utc(time.time()), branch, build)
    held = promote_folder(store, prefix, project, key, folder, promotion)
    if held.promoted != promotion:
        b = held.promoted.build
        log(f"{folder} at {key} was promoted by build {b.number} ({b.url}); left as it is")
    elif writes_latest(branch):
        log(f"Promoted {folder} at {key}; the latest file of {branch} names it")
    else:
        log(f"Promoted {folder} at {key}; {branch} is no branch, so no latest file names it")
