"""The downloads of a first build that take the latest successful build of another project.

Such a download is not planned: the key of each step it takes is the one that project's
latest files name, by the walk the jobs take (``resolve``). The plan step reads them once and
the plan holds them, so its second run and every job take the same keys, and the build's key
holds them. Under a first build at a tag only the entry's own branch is read. A download that
finds no latest file fails the plan before anything runs.
"""

from __future__ import annotations

from collections.abc import Sequence

from ..errors import ArtifactsError
from ..forms import name_of
from ..layout import Folder
from ..options import Entry
from ..resolve import walk_named, walk_newest, walked_refs
from .plan import Latest, LatestDownload, PlanError
from .templates import Declaration, declared_path


def latest_download(e: Entry, project: str) -> LatestDownload:
    """What a latest successful entry of ``project`` asks the walk over latest files."""
    return LatestDownload(project, e.variant, e.git_ref, e.require_strict_ref_match, e.step or None)


def read_keys(store, d: LatestDownload, refs: Sequence[str]) -> dict[str, str]:
    """The key of each step ``d`` takes: of each step it names, the key its own latest file
    names; without a step, of every step of the platform's newest key."""
    if d.step is None:
        return {s.folder.step: s.key for s in walk_newest(store, d.project, refs, d.variant)}
    return {s: walk_named(store, d.project, refs, Folder(d.variant, s)).key for s in d.step}


def asked_by(d: LatestDownload, declaration: Declaration, at_tag: bool) -> str:
    """Where the download that found no latest file is written, and the way out at a tag."""
    template = declared_path(declaration, d.project, latest=True)
    asked = f"The download of {d.project} in {template} takes its latest successful build."
    if not at_tag:
        return asked
    return (
        f"{asked} A build at a tag reads no other branch: build {d.project} under the name "
        f"{name_of(d.ref)}, or write the branch to take as the git_ref of that entry."
    )


def read_latest(
    store, downloads: Sequence[LatestDownload], declaration: Declaration, at_tag: bool
) -> tuple[Latest, ...]:
    """Each of ``downloads``, once, with the key of each step it takes. ``declaration`` names
    the template of each; ``at_tag`` is whether the first build is at a tag. A download that
    finds no key fails the plan, every such download named."""
    found, problems = [], []
    for d in dict.fromkeys(downloads):
        refs = (d.ref,) if at_tag else walked_refs(d.ref, d.strict)
        try:
            found.append(Latest(d, read_keys(store, d, refs)))
        except ArtifactsError as exc:
            problems.append(f"{exc} {asked_by(d, declaration, at_tag)}")
    if problems:
        raise PlanError("\n".join(problems))
    return tuple(found)
