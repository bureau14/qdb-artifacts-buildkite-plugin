"""Promote: make a step's captured files at the build's key official, then point the branch's
latest file at that key.

A promote reads the manifest of the folder it promotes. Captured, it writes it
back once, naming this build, its branch and the time, then writes the latest
file of the branch: its text is the key, and the last writer wins. Promoted
already, by this build or another, it leaves it (the first promote wins) and
writes no latest file, so the retry of a job that promoted never moves a
branch's latest file back. Not there, it fails: the step that uploads it
uploaded nothing.

A build that does not speak for its branch writes no latest file
(``branch_build``): one a plan step started for a dependency, whose
``SNAPSHOT_BRANCH_BUILD`` says so. The plan step writes the latest files of
the steps it finds made with the same function (``write_latest_file``), on the
same condition.
"""

import time

from . import layout
from .clients import aws_clients, resolve_object_auth
from .config import load_store_config
from .errors import ArtifactsError
from .forms import branch_ref, iso_utc
from .log import log
from .manifest import Promoted, promoted_by, read_manifest_at, write_manifest
from .objects import destination_store


def write_latest_file(store, prefix, project, branch, folder, key):
    """Point the latest file of ``folder`` of ``project`` on ``branch`` at ``key``: its text
    is the key, and the last writer wins."""
    where = layout.latest_file(prefix, project, branch_ref(branch), folder)
    store.put(where, key.encode(), "text/plain")


def promote_folder(store, prefix, project, key, folder, promotion, branch_build):
    """Promote ``folder`` of ``project`` at ``key`` as ``promotion`` says, unless a build
    promoted it already; the manifest that holds afterwards. The latest file of the branch
    moves only for a build that speaks for its branch (``branch_build``). A folder without a
    manifest raises."""
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
    if branch_build:
        write_latest_file(store, prefix, project, promotion.branch, folder, key)
    return made


def promote(project, key, folder, branch, build, branch_build):
    """Promote the step ``folder`` (platform and step id) of ``project`` at ``key`` for
    ``build``, which runs under ``branch`` and speaks for it when ``branch_build``."""
    _, ssm = aws_clients()
    cfg = load_store_config(ssm)
    auth = resolve_object_auth(ssm, cfg, permission="object-read-write")
    store, prefix = destination_store(cfg, auth)

    promotion = Promoted(iso_utc(time.time()), branch, build)
    held = promote_folder(store, prefix, project, key, folder, promotion, branch_build)
    if held.promoted != promotion:
        b = held.promoted.build
        log(f"{folder} at {key} was promoted by build {b.number} ({b.url}); left as it is")
    elif branch_build:
        log(f"Promoted {folder} at {key}; the latest file of {branch} names it")
    else:
        log(
            f"Promoted {folder} at {key}; this build was started for a dependency and does not "
            f"speak for its branch, so no latest file of {branch} names it"
        )
