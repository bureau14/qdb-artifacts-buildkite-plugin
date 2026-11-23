"""The generated pipeline as the plan step uploads it: one resolution for every job.

The pipeline a repository generates says "download quasardb-build, linux-aarch64-release" and
leaves the build open; two jobs could then find two different builds. So the plan step writes
the key of every step a download takes into the document before any job exists:

- every entry of another project gets ``keys: {<step id>: <key>}`` in place of the options
  that would find a build: the planned key of a snapshot dependency, or the key read for a
  latest successful download;
- every configuration of the plugin gets the build's own ``key``;
- every key that names the plugin names it at the commit the plan step runs;
- a step whose folders are all made at the build's key is replaced by a step that says so.

Values in, values out: the document handed in is never changed.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import dacite

from ..layout import Folder
from ..manifest import Manifest
from ..options import PLUGIN, READ, Entry, as_agent_names
from .latest import latest_download
from .pipeline import (
    Step,
    dependencies,
    is_group,
    is_plugin_key,
    made_folders,
    other_project,
    promotions,
    taken_folders,
)
from .plan import LatestDownload, Planned

# What decides whether and when a step runs; a replacement keeps them.
FLOW_FIELDS = ("key", "depends_on", "allow_dependency_failure", "if", "branches")
# The options of an entry that would find a build, which ``keys`` take the place of.
FINDING = ("step", "git_ref", "require_strict_ref_match", "latest", "keys")


@dataclass(frozen=True)
class Rewrite:
    """``key`` is the pipeline's own key, ``plugin_commit`` the commit of the plugin the plan
    step runs, ``dependencies`` each snapshot dependency as the plan holds it, ``latest``
    the key of each step each latest successful download takes, ``made`` the manifest of each
    own folder that is made at ``key``."""

    own: str
    key: str
    plugin_commit: str
    dependencies: Mapping[str, Planned]
    latest: Mapping[LatestDownload, Mapping[str, str]]
    made: Mapping[Folder, Manifest]


def plugin_key(commit: str) -> str:
    return f"{PLUGIN}#{commit}"


# ---------------------------------------------------------------------------
# Completion: a step whose folders are made is replaced
# ---------------------------------------------------------------------------


def replaced(steps: Sequence[Step], made: Mapping[Folder, Manifest]) -> frozenset[int]:
    """The positions of the steps completion replaces: those that make at least one folder,
    every one of them made and promoted by this pipeline. A made folder the pipeline does not
    promote replaces nothing."""
    done = frozenset(made) & frozenset(promotions(steps)) if made else frozenset()
    return frozenset(s.position for s in steps if made_folders(s) and made_folders(s) <= done)


def replacement(step: Step, own: str, made: Mapping[Folder, Manifest]) -> dict[str, Any]:
    """The step that stands in for ``step``: its ``FLOW_FIELDS``, and an echo of the builds
    that made its folders. Without plugins and agents it runs for a second on the default
    queue and passes, so the build flows as generated."""
    builds = {f: made[f].promoted.build for f in sorted(made_folders(step))}
    numbers = sorted({b.number for b in builds.values()})
    by = f"build {numbers[0]}" if len(numbers) == 1 else f"builds {', '.join(map(str, numbers))}"
    echoes = [
        f'echo "{f} of {own} was made by build {b.number} {b.url}"' for f, b in builds.items()
    ]
    return {
        "label": f"{step.label} (made by {by})" if step.label else f"(made by {by})",
        **{f: step.raw[f] for f in FLOW_FIELDS if f in step.raw},
        "command": "\n".join(echoes),
    }


def needed_folders(
    steps: Sequence[Step],
    own: str,
    made: Mapping[Folder, Manifest],
    step_ids: Mapping[str, Sequence[str]],
) -> dict[str, frozenset[Folder]]:
    """``{project: folders}``: what the snapshot entries of the steps that completion keeps
    take of each dependency. ``step_ids`` gives the step ids of each dependency, which an
    entry that names no step takes."""
    gone = replaced(steps, made)
    kept = [s for s in steps if s.position not in gone]
    needs: dict[str, frozenset[Folder]] = {}
    for _, project, e in dependencies(kept, own, latest=False):
        needs[project] = needs.get(project, frozenset()) | taken_folders(e, step_ids[project])
    return needs


# ---------------------------------------------------------------------------
# The rewrite
# ---------------------------------------------------------------------------


def planned_entry(entry: Mapping[str, Any], r: Rewrite) -> Mapping[str, Any]:
    """``entry`` of another project with the key of each step it takes; any other as it is."""
    e = dacite.from_dict(Entry, entry, READ)
    project = other_project(e, r.own)
    if project is None:
        return entry
    if e.latest:
        keys = r.latest[latest_download(e, project)]
    else:
        taken = r.dependencies[project]
        keys = {f.step: taken.key for f in taken_folders(e, taken.steps)}
    kept = {k: v for k, v in entry.items() if k not in FINDING}
    return {**kept, "keys": dict(sorted(keys.items()))}


def planned_configuration(config: Any, r: Rewrite) -> Any:
    """One configuration of the plugin with the build's key, and its entries planned."""
    if not isinstance(config, dict):
        return config
    c = as_agent_names(config)
    download = c.get("download")
    if isinstance(download, dict) and isinstance(download.get("projects"), list):
        entries = [planned_entry(e, r) for e in download["projects"]]
        c = {**c, "download": {**download, "projects": entries}}
    return {**c, "key": r.key}


def with_configurations(value: Any, f: Callable[[Any], Any], plugin: str) -> Any:
    """``value`` with every configuration of the plugin replaced by ``f`` of it, under the
    name ``plugin``."""
    if isinstance(value, dict):
        return {
            (plugin if is_plugin_key(k) else k): (
                f(v) if is_plugin_key(k) else with_configurations(v, f, plugin)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [with_configurations(v, f, plugin) for v in value]
    return value


def with_steps(listed: Sequence[Any], steps: Sequence[Step], f: Callable[[Step], Any]) -> list:
    """The step list ``listed`` of a document, each step that is not a group replaced by ``f``
    of its ``Step``; ``steps`` are the document's, in order."""
    remaining = iter(steps)

    def mapped(items: Sequence[Any]) -> list:
        return [
            {**s, "steps": mapped(s["steps"])} if is_group(s) else f(next(remaining)) for s in items
        ]

    return mapped(listed)


def planned(doc: Any, steps: Sequence[Step], r: Rewrite) -> Any:
    """The document to upload for the generated pipeline ``doc``, whose steps are ``steps``.
    ``doc`` keeps the rule and promotes no folder at two places."""
    gone = replaced(steps, r.made)
    plugin = plugin_key(r.plugin_commit)

    def rewritten(s: Step) -> Any:
        if s.position in gone:
            return replacement(s, r.own, r.made)
        return with_configurations(s.raw, lambda c: planned_configuration(c, r), plugin)

    return {**doc, "steps": with_steps(doc["steps"], steps, rewritten)}
