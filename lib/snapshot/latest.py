"""The downloads of a first build that take the latest successful build of another project,
read once by its plan step.

Such a download is not planned: the key of each step it takes is the one that
project's latest files name, found by the walk the jobs and the library take
(``resolve.named_on_first_ref``, ``resolve.newest_on_first_ref``). The plan
step reads them once, before it plans, and the plan holds them, so its second
run and every job of its build take the same keys; the build's key holds them,
so the build is reused while the latest files name the same keys. A download
that finds no latest file fails the plan before anything runs, rather than its
job when it runs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from .. import layout
from ..layout import Folder
from ..resolve import named_on_first_ref, newest_on_first_ref, walked_refs
from .declaration import LATEST_SUCCESSFUL, Declaration, declared_path
from .plan import Latest, LatestDownload, PlanError


def read_keys(store, prefix: str, d: LatestDownload, refs: Sequence[str]) -> dict[str, str | None]:
    """The key of each step ``d`` takes, read through the latest files of ``refs``: of each
    step it names, the key its own latest file names, None when none does; without a step, of
    every step whose latest file names the newest key of the platform, none when none does."""
    if d.step is None:
        newest = newest_on_first_ref(store, prefix, d.project, refs, d.variant)
        return {s.folder.step: s.key for s in newest}
    named = {
        s: named_on_first_ref(store, prefix, d.project, refs, Folder(d.variant, s)) for s in d.step
    }
    return {s: None if f is None else f.key for s, f in named.items()}


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


def missing_lines(
    prefix: str,
    d: LatestDownload,
    refs: Sequence[str],
    keys: Mapping[str, str | None],
    template: str,
) -> list[str]:
    """Why ``d`` cannot be planned with the ``keys`` read on ``refs``: a line per step it names
    that has no key, or one when it names none and none was found; no line when it can be.
    ``template`` is where its entry is written."""
    if d.step is None:
        return [] if keys else [missing_newest(prefix, d, refs, template)]
    return [missing_named(prefix, d, s, refs, template) for s, k in keys.items() if k is None]


def read_latest(
    store, prefix: str, downloads: Sequence[LatestDownload], declaration: Declaration
) -> tuple[Latest, ...]:
    """Each of ``downloads``, once, with the key of each step it takes; a download that finds
    none fails the plan, every such download named.

    ``declaration`` is that of the build's own commit, which names the
    template of each download.
    """
    refs = {d: walked_refs(d.ref, d.strict) for d in downloads}
    keys = {d: read_keys(store, prefix, d, r) for d, r in refs.items()}
    missing = [
        line
        for d, r in refs.items()
        for line in missing_lines(
            prefix, d, r, keys[d], declared_path(declaration, d.project, LATEST_SUCCESSFUL)
        )
    ]
    if missing:
        raise PlanError("\n".join(missing))
    return tuple(Latest(d, k) for d, k in keys.items())
