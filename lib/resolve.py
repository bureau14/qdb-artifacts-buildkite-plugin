"""Which steps a download takes, at which keys, and which of their files.

A step is found at a key in one of two ways:

- the key is given: the ``keys`` the plan step wrote into the entry, or the build's own key;
  the step's manifest must be there (``keyed_step``);
- the walk over the latest files of branches, for the latest successful build: the entry's
  ref, then master, then main (``walked_refs``). A named step takes the key its own latest
  file names; an entry without a step takes every step whose latest file names the newest key
  of the platform. A key counts only when the step's manifest says promoted.

The files of a step are those its manifest names that match the entry's patterns and no
exclude: a download lists nothing. The plugin's download, a caller of the library and the plan
step all resolve through these functions.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import layout
from .codec import decoded
from .config import StoreConfig
from .errors import ArtifactsError
from .forms import name_of
from .job import Build, Job, job_ref
from .key import KeyJson, commit_of
from .layout import Folder
from .log import log
from .manifest import Manifest, is_made, read_manifest_at
from .objects import destination_store
from .options import Entry
from .rules import is_excluded, match_rules, parse_rules

MASTER_REFS = ("refs/heads/master", "refs/heads/main")


@dataclass(frozen=True)
class ResolvedStep:
    """One step a download takes. ``ref`` is the ref whose latest file the walk found it
    through and ``latest`` that file; both None when its key was given."""

    project: str
    folder: Folder
    key: str
    location: str
    manifest: Manifest
    ref: str | None = None
    latest: str | None = None


@dataclass(frozen=True)
class ResolvedArtifact:
    """Named selection fields for callers; entry-rule order resolves overlapping filters."""

    key: str
    rel: str
    size: int
    entry_rules: list[tuple[str | None, str | None]]


def made_by(step: ResolvedStep) -> tuple[str, Build]:
    """Whose files a download takes of ``step``: ``promoted`` and the build that promoted
    them; of a step of the own build that a later step promotes, ``uploaded`` and its build."""
    m = step.manifest
    if m.promoted is not None:
        return "promoted", m.promoted.build
    return "uploaded", m.uploaded.build


def entry_name(n: int, project: str, platform: str) -> str:
    """How messages name the ``n``-th download entry of a step."""
    return f"Download entry {n} ({project}, {platform})"


# ---------------------------------------------------------------------------
# A step at a given key
# ---------------------------------------------------------------------------


def keyed_step(store, project: str, key: str, folder: Folder, name: str) -> ResolvedStep:
    """The step ``folder`` of ``project`` at ``key``, whose manifest must be there."""
    location = layout.step_folder(store.prefix, project, key, folder)
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
    return (ref,) if strict or ref in MASTER_REFS else (ref, *MASTER_REFS)


def named_key(store, where: str) -> str | None:
    """The key a latest file names; None when there is no such file."""
    body = store.get(where)
    return None if body is None else body.decode("utf-8").strip()


def made_step(
    store, project: str, key: str, folder: Folder, ref: str, latest: str
) -> ResolvedStep | None:
    """The step at ``key`` that the latest file ``latest`` of ``ref`` names, when it is made."""
    location = layout.step_folder(store.prefix, project, key, folder)
    m = read_manifest_at(store, location)
    return ResolvedStep(project, folder, key, location, m, ref, latest) if is_made(m) else None


def named_step_at(store, project: str, ref: str, folder: Folder) -> ResolvedStep | None:
    """The step of ``folder`` that the latest file of ``ref`` names, when it is made."""
    where = layout.latest_file(store.prefix, project, ref, folder)
    key = named_key(store, where)
    return None if key is None else made_step(store, project, key, folder, ref, where)


def newest_steps_at(store, project: str, ref: str, platform: str) -> list[ResolvedStep]:
    """The made steps of ``platform`` whose latest file of ``ref`` names the newest key: the
    one the latest file written last names; of two written at one time, the key that sorts
    last."""
    listed = store.list(layout.latest_files(store.prefix, project, ref, platform))
    named = [(item.last_modified, named_key(store, item.key), item.key) for item in listed]
    named = [(time, key, where) for time, key, where in named if key is not None]
    if not named:
        return []
    newest = max((time, key) for time, key, _ in named)[1]
    found = (
        made_step(store, project, key, Folder(platform, where.rsplit("/", 1)[1]), ref, where)
        for _, key, where in named
        if key == newest
    )
    return sorted((s for s in found if s is not None), key=lambda s: s.folder.step)


def first_found(refs: Sequence[str], at: Callable[[str], Any]) -> Any:
    """``at(ref)`` of the first of ``refs`` that gives anything; None when none does."""
    return next((found for found in map(at, refs) if found), None)


def walk_named(store, project: str, refs: Sequence[str], folder: Folder) -> ResolvedStep:
    """The step ``folder`` of the latest successful build of ``project`` that made it, on the
    first of ``refs`` that has one."""
    found = first_found(refs, lambda r: named_step_at(store, project, r, folder))
    if found:
        return found
    tried = ", ".join(layout.latest_file(store.prefix, project, r, folder) for r in refs)
    raise ArtifactsError(
        f"{project} has no latest successful build of {folder} on {', '.join(refs)}: no "
        f"latest file of {tried} names a key at which {folder} is promoted."
    )


def walk_newest(store, project: str, refs: Sequence[str], platform: str) -> list[ResolvedStep]:
    """Every made step of the newest key of ``platform`` of ``project``, on the first of
    ``refs`` that has one."""
    found = first_found(refs, lambda r: newest_steps_at(store, project, r, platform))
    if found:
        return found
    tried = ", ".join(layout.latest_files(store.prefix, project, r, platform) for r in refs)
    raise ArtifactsError(
        f"{project} has no latest successful build of {platform} on {', '.join(refs)}: no "
        f"latest file in {tried} names a key at which its step is promoted."
    )


# ---------------------------------------------------------------------------
# The steps of a download entry
# ---------------------------------------------------------------------------


def takes_own_build(e: Entry, project: str, ref: str | None) -> bool:
    """Whether ``e`` takes from the job's own build: of its own ``project``, and at no git_ref,
    at the build's own ``ref``, or at any when the build's ref is unknown."""
    own_project = (e.project_id or project) == project
    return own_project and (not e.git_ref or ref is None or e.git_ref == ref)


def no_git_ref(project: str, entry: str) -> str:
    """Why an entry of another ``project`` without git_ref is refused, by a job and by the
    plan step alike. ``entry`` says where to write one."""
    return (
        f"downloads from {project} and names no git_ref, so no build of {project} can be found. "
        f'Way out: write git_ref in {entry}, "{{git_ref}}" for the build\'s own ref or the ref '
        "to take."
    )


def entry_steps(
    store, e: Entry, job: Job, own_key: Callable[[], str], name: str
) -> list[ResolvedStep]:
    """The steps entry ``e`` takes, in the order of their ids: at the plan step's keys; else,
    from the job's own build, the steps it names at ``own_key()``; else by the walk."""
    project = e.project_id or job.project

    def at(key: str, step: str) -> ResolvedStep:
        return keyed_step(store, project, key, Folder(e.variant, step), name)

    if e.keys:
        return [at(key, step) for step, key in sorted(e.keys.items())]
    if takes_own_build(e, job.project, job_ref(job)):
        if not e.step:
            raise ArtifactsError(
                f"{name} names no step and downloads from this build. Write in it the steps it "
                "takes, as step: build or step: [api, rest]; on a branch that has taken the "
                "step-id change (merge master), the runner writes them when another step "
                f"uploads {e.variant}."
            )
        return [at(own_key(), step) for step in sorted(e.step)]
    if not e.git_ref:
        raise ArtifactsError(f"{name} {no_git_ref(project, 'that entry')}")
    refs = walked_refs(e.git_ref, e.require_strict_ref_match)
    if not e.step:
        return walk_newest(store, project, refs, e.variant)
    return [walk_named(store, project, refs, Folder(e.variant, s)) for s in sorted(e.step)]


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


def nothing_matched(name: str, patterns, exclude_patterns, steps: Sequence[ResolvedStep]):
    """The error of a download none of whose steps gives a file."""
    folders = ", ".join(s.location for s in steps)
    return ArtifactsError(
        f"No file of {name[0].lower() + name[1:]} matched files {list(patterns)} and exclude "
        f"{list(exclude_patterns or [])}, in any of its steps: {folders}."
    )


def log_step(name: str, step: ResolvedStep) -> None:
    """Say which build a download takes a step of."""
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


def resolve_dependency_key(cfg: StoreConfig, resolved: ResolvedStep, project_id: str) -> Key:
    """Read the direct dependency recorded by the producer of a resolved step.

    A key document can record multiple keys for one project. Without a unique key
    the caller cannot reproduce the producer's selection by project alone.
    """
    store = destination_store(cfg)
    where = layout.key_json(store.prefix, resolved.project, resolved.key)
    body = store.get(where)
    if body is None:
        raise ArtifactsError(f"Missing snapshot metadata at {where}")
    document = decoded(KeyJson, body)
    keys = {key for project, key in document.deps if project == project_id}
    if len(keys) != 1:
        raise ArtifactsError(
            f"Expected one dependency key for {project_id} in {where}, found {len(keys)}"
        )
    return Key(keys.pop())


def resolve_artifacts(cfg, project_id, variant, step, build, patterns, exclude_patterns=None):
    """Find one step of a project's platform and select its artifacts, without downloading.

    ``build`` says which build of the step: a ``Branch``, its latest successful build there,
    or a ``Key``. ``patterns`` use the plugin's archive_glob[!entry_filter] syntax. Returns
    (bucket, ResolvedStep, objects), objects a list of ResolvedArtifact. A step that cannot be
    found, or that gives no file, raises ArtifactsError. Needs no Buildkite environment.
    """
    store = destination_store(cfg)
    folder = Folder(variant, step)
    name = f"The download of {project_id} ({variant})"
    if isinstance(build, Key):
        resolved = keyed_step(store, project_id, build.key, folder, name)
    else:
        refs = walked_refs(build.git_ref, build.strict)
        resolved = walk_named(store, project_id, refs, folder)
    log_step(name, resolved)
    objects = selected(resolved, parse_rules(patterns), exclude_patterns)
    if not objects:
        raise nothing_matched(name, patterns, exclude_patterns, [resolved])
    return store.bucket, resolved, objects
