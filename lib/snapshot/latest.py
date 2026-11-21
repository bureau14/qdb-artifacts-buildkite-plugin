"""The downloads of a first build that take the latest successful build of another project,
read once by its plan step (specification section 4).

Such a download is not planned: the key of each step it takes is the one that
project's latest files name, found by the walk the jobs and the library take
(``resolve.first_named``, ``resolve.first_newest``). The plan step reads them
once, before it plans, and the plan holds them, so its second run and every
job of its build take the same keys; the build's key holds them, so the build
is reused while the latest files name the same keys. A download that finds no
latest file fails the plan before anything runs, rather than its job when it
runs.
"""

from __future__ import annotations

from collections.abc import Sequence

from .. import layout
from ..layout import Folder
from ..resolve import first_named, first_newest, walked_refs
from .declaration import LATEST_SUCCESSFUL, Declaration, declared_path
from .plan import Latest, LatestDownload, PlanError


def missing_named(
    prefix: str, d: LatestDownload, step: str, refs: Sequence[str], template: str
) -> str:
    """Why ``d`` cannot be planned: no branch of its walk has a latest file of ``step`` that
    names a key at which the step is promoted."""
    folder = Folder(d.variant, step)
    tried = ", ".join(layout.latest_file(prefix, d.project, r, folder) for r in refs)
    return (
        f"{d.project} has no latest file at {tried}. The download of {d.project} in {template} "
        "takes its latest successful build, and no build of it promoted that step on those "
        "branches."
    )


def missing_newest(prefix: str, d: LatestDownload, refs: Sequence[str], template: str) -> str:
    """Why ``d``, which names no step, cannot be planned: no branch of its walk has a latest
    file of its platform that names a key at which the step is promoted."""
    tried = ", ".join(layout.latest_files(prefix, d.project, r, d.variant) for r in refs)
    return (
        f"{d.project} has no latest file in {tried}. The download of {d.project} in {template} "
        f"takes its latest successful build of every step, and no build of it promoted a step "
        f"of {d.variant} on those branches."
    )


def read_download(store, prefix: str, d: LatestDownload, template: str) -> Latest | str:
    """``d`` with the key of each step it takes, read through the latest files: of each step
    it names, the key its own latest file names; without a step, of every step whose latest
    file names the newest key of the platform. Else why it cannot be planned, a line per
    step not found; ``template`` is where its entry is written."""
    refs = walked_refs(d.ref, d.strict)
    if d.step is None:
        newest = first_newest(store, prefix, d.project, refs, d.variant)
        if not newest:
            return missing_newest(prefix, d, refs, template)
        return Latest(d, {s.folder.step: s.key for s in newest})
    named = {s: first_named(store, prefix, d.project, refs, Folder(d.variant, s)) for s in d.step}
    missing = [missing_named(prefix, d, s, refs, template) for s, f in named.items() if f is None]
    if missing:
        return "\n".join(missing)
    return Latest(d, {s: f.key for s, f in named.items()})


def read_latest(
    store, prefix: str, downloads: Sequence[LatestDownload], declaration: Declaration
) -> tuple[Latest, ...]:
    """Each of ``downloads``, once, with the keys of the steps it takes; a download that
    finds none fails the plan, every such download named.

    ``declaration`` is that of the build's own commit, which names the
    template of each download.
    """
    read = [
        read_download(store, prefix, d, declared_path(declaration, d.project, LATEST_SUCCESSFUL))
        for d in dict.fromkeys(downloads)
    ]
    missing = [r for r in read if isinstance(r, str)]
    if missing:
        raise PlanError("\n".join(missing))
    return tuple(read)
