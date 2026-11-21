"""Which steps a download takes, at which keys, and which of their files.

A step is found at a key in one of two ways:

- the key is given: the ``keys`` the plan step writes into an entry, or the
  build's own key; the step's manifest must be there (``keyed_step``);
- the walk over the latest files of branches, for a download that takes the
  latest successful build: the entry's ref, then ``refs/heads/master``, then
  ``refs/heads/main``, or the entry's ref alone when it is one of those or
  the entry asks for a strict ref match. A named step takes the key its own
  latest file names (``walk_named``); an entry without a step takes every
  step whose latest file names the newest key of the platform
  (``walk_newest``). A key counts only when the step's manifest says
  promoted.

The files of a step are those its manifest names that match the entry's
patterns and no exclude (``selected``): a download lists nothing. The plugin's
download and a caller of the library resolve through these same functions,
and fetch the objects each its own way; the plan step of a first build reads
its latest successful downloads through the walk's own finders
(``named_on_first_ref``, ``newest_on_first_ref``), and says itself what it did
not find. Before a step's files are fetched, the log says which build they
come from (``log_step``): the branch and latest file the walk read, the key,
the key's commit, and the build that promoted the step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from . import layout
from .errors import ArtifactsError
from .forms import name_of
from .key import commit_of
from .layout import Folder
from .log import log
from .manifest import Build, Manifest, is_made, read_manifest_at
from .objects import destination_store
from .rules import is_excluded, match_rules, parse_rules

MASTER_REFS = ("refs/heads/master", "refs/heads/main")


@dataclass(frozen=True)
class ResolvedStep:
    """One step a download takes: its project, folder and key, where it lies, its manifest.

    ``ref`` is the ref whose latest file the walk found it through, ``latest``
    that file; both None when its key was given.
    """

    project: str
    folder: Folder
    key: str
    location: str
    manifest: Manifest
    ref: str | None = None
    latest: str | None = None


def made_by(step: ResolvedStep) -> tuple[str, Build]:
    """Whose files a download takes of ``step``: ``promoted`` and the build that promoted
    them; a step of the own build that a later step promotes is captured when it is read, and
    then ``uploaded`` and the build that uploaded them."""
    m = step.manifest
    if m.promoted is not None:
        return "promoted", m.promoted.build
    return "uploaded", m.uploaded.build


@dataclass(frozen=True)
class ResolvedArtifact:
    """Named selection fields for callers; entry-rule order resolves overlapping filters."""

    key: str
    rel: str
    size: int
    entry_rules: list[tuple[str | None, str | None]]


def entry_name(n: int, project: str, platform: str) -> str:
    """How messages name the ``n``-th download entry of a step."""
    return f"Download entry {n} ({project}, {platform})"


# ---------------------------------------------------------------------------
# A step at a given key
# ---------------------------------------------------------------------------


def keyed_step(
    store, prefix: str, project: str, key: str, folder: Folder, name: str
) -> ResolvedStep:
    """The step ``folder`` of ``project`` at ``key``, whose manifest must be there.

    ``name`` is how messages name the download.
    """
    location = layout.step_folder(prefix, project, key, folder)
    m = read_manifest_at(store, location)
    if m is None:
        raise ArtifactsError(
            f"{name} takes step {folder.step} at {key}, and {location}/{layout.MANIFEST} does "
            "not exist: no build has uploaded that step at that key."
        )
    return ResolvedStep(project, folder, key, location, m)


# ---------------------------------------------------------------------------
# The walk over latest files
# ---------------------------------------------------------------------------


def walked_refs(ref: str, strict: bool) -> tuple[str, ...]:
    """The branches a walk tries, in order."""
    if strict or ref in MASTER_REFS:
        return (ref,)
    return (ref, *MASTER_REFS)


def named_key(store, where: str) -> str | None:
    """The key a latest file names; None when there is no such file."""
    body = store.get(where)
    return None if body is None else body.decode("utf-8").strip()


def made_step(
    store, prefix: str, project: str, key: str, folder: Folder, ref: str, latest: str
) -> ResolvedStep | None:
    """The step at ``key`` that the latest file ``latest`` of ``ref`` named, when its manifest
    says promoted."""
    location = layout.step_folder(prefix, project, key, folder)
    m = read_manifest_at(store, location)
    return ResolvedStep(project, folder, key, location, m, ref, latest) if is_made(m) else None


def named_step_at(
    store, prefix: str, project: str, ref: str, folder: Folder
) -> ResolvedStep | None:
    """The step of ``folder`` that the latest file of ``ref`` names; None when it names none
    that is made."""
    where = layout.latest_file(prefix, project, ref, folder)
    key = named_key(store, where)
    return None if key is None else made_step(store, prefix, project, key, folder, ref, where)


def newest_steps_at(
    store, prefix: str, project: str, ref: str, platform: str
) -> list[ResolvedStep]:
    """The steps of ``platform`` whose latest file of ``ref`` names the newest key, and that
    are made there; none when ``ref`` has no latest file of the platform.

    The newest key is the one the latest file with the latest time names; of
    two at one time, the key that sorts last.
    """
    listed = store.list(layout.latest_files(prefix, project, ref, platform))
    named = [(item.last_modified, named_key(store, item.key), item.key) for item in listed]
    named = [(time, key, where) for time, key, where in named if key is not None]
    if not named:
        return []
    newest = max((time, key) for time, key, _ in named)[1]
    found = (
        made_step(
            store, prefix, project, key, Folder(platform, where.rsplit("/", 1)[1]), ref, where
        )
        for _, key, where in named
        if key == newest
    )
    return sorted((s for s in found if s is not None), key=lambda s: s.folder.step)


def named_on_first_ref(
    store, prefix: str, project: str, refs: Sequence[str], folder: Folder
) -> ResolvedStep | None:
    """The step ``folder`` of the latest successful build of ``project`` that made it, on the
    first of ``refs`` that has one; None when none has."""
    found = (named_step_at(store, prefix, project, r, folder) for r in refs)
    return next((s for s in found if s is not None), None)


def newest_on_first_ref(
    store, prefix: str, project: str, refs: Sequence[str], platform: str
) -> list[ResolvedStep]:
    """Every made step of the newest key of ``platform``, on the first of ``refs`` that has
    one; none when none has."""
    found = (newest_steps_at(store, prefix, project, r, platform) for r in refs)
    return next((s for s in found if s), [])


def walk_named(
    store, prefix: str, project: str, ref: str, strict: bool, folder: Folder
) -> ResolvedStep:
    """The step ``folder`` of the latest successful build of ``project`` that made it,
    on the first branch of the walk that has one."""
    refs = walked_refs(ref, strict)
    found = named_on_first_ref(store, prefix, project, refs, folder)
    if found is not None:
        return found
    tried = ", ".join(layout.latest_file(prefix, project, r, folder) for r in refs)
    raise ArtifactsError(
        f"{project} has no latest successful build of {folder} on {', '.join(refs)}: no "
        f"latest file of {tried} names a key at which {folder} is promoted."
    )


def walk_newest(
    store, prefix: str, project: str, ref: str, strict: bool, platform: str
) -> list[ResolvedStep]:
    """Every made step of the newest key of ``platform``, on the first branch of the walk
    that has one."""
    refs = walked_refs(ref, strict)
    found = newest_on_first_ref(store, prefix, project, refs, platform)
    if found:
        return found
    tried = ", ".join(layout.latest_files(prefix, project, r, platform) for r in refs)
    raise ArtifactsError(
        f"{project} has no latest successful build of {platform} on {', '.join(refs)}: no "
        f"latest file in {tried} names a key at which its step is promoted."
    )


# ---------------------------------------------------------------------------
# The files of a step
# ---------------------------------------------------------------------------


def selected(step: ResolvedStep, rules, exclude_patterns) -> list[ResolvedArtifact]:
    """The files the manifest of ``step`` names that match ``rules`` and no exclude pattern."""
    found = (
        (f, match_rules(f.path, rules))
        for f in step.manifest.files
        if not is_excluded(f.path, exclude_patterns)
    )
    return [
        ResolvedArtifact(layout.key_join(step.location, f.path), f.path, f.size, entry_rules)
        for f, entry_rules in found
        if entry_rules
    ]


def within_sentence(name: str) -> str:
    """A download's name, which starts a sentence, inside one."""
    return name[0].lower() + name[1:]


def nothing_matched(name: str, patterns, exclude_patterns, steps: Sequence[ResolvedStep]):
    """The error of a download none of whose steps gives a file."""
    folders = ", ".join(s.location for s in steps)
    return ArtifactsError(
        f"No file of {within_sentence(name)} matched files {list(patterns)} and exclude "
        f"{list(exclude_patterns or [])}, in any of its steps: {folders}."
    )


def log_step(name: str, step: ResolvedStep) -> None:
    """Say which build a download takes a step of: the branch and latest file the walk read,
    when it read one; the key; the key's commit; the build its manifest names."""
    log(f"{name}, step {step.folder.step}:")
    if step.ref is not None:
        log(f"  branch   = {name_of(step.ref)}, latest file {step.latest}")
    log(f"  key      = {step.key}")
    log(f"  commit   = {commit_of(step.key)}")
    by, build = made_by(step)
    log(f"  {by} = build {build.number} {build.url}")


# ---------------------------------------------------------------------------
# The library
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Branch:
    """The latest successful build of a step on a branch: through the latest file of
    ``git_ref``, a full ref, then of master and main unless ``strict``."""

    git_ref: str
    strict: bool = False


@dataclass(frozen=True)
class Key:
    """The build of a step at ``key``."""

    key: str


def library_step(store, prefix: str, project: str, folder: Folder, build, name: str):
    """The step ``folder`` of ``project`` in ``build``, a ``Branch`` or a ``Key``."""
    if isinstance(build, Key):
        return keyed_step(store, prefix, project, build.key, folder, name)
    return walk_named(store, prefix, project, build.git_ref, build.strict, folder)


def resolve_artifacts(cfg, auth, project_id, variant, step, build, patterns, exclude_patterns=None):
    """Find one step of a project's platform and select its artifacts, without downloading.

    Takes explicit StoreConfig and ObjectAuth; no Buildkite environment or
    output directory is required. ``build`` says which build of the step: a
    ``Branch``, its latest successful build there, or a ``Key``.
    ``patterns`` use the plugin's archive_glob[!entry_filter] syntax. Returns
    (bucket, ResolvedStep, objects), objects a list of ResolvedArtifact. A
    step that cannot be found, or that gives no file, raises ArtifactsError.
    """
    store, prefix = destination_store(cfg, auth)
    folder = Folder(variant, step)
    name = f"The download of {project_id} ({variant})"
    resolved = library_step(store, prefix, project_id, folder, build, name)
    log_step(name, resolved)
    objects = selected(resolved, parse_rules(patterns), exclude_patterns)
    if not objects:
        raise nothing_matched(name, patterns, exclude_patterns, [resolved])
    return store.bucket, resolved, objects
