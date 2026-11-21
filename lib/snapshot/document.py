"""The generated pipeline as data: what the plan step reads from it and writes into it.

Specification section 8, and the trigger and resume steps of section 7.

The pipeline a repository generates says "download quasardb-build,
linux-aarch64-release" and leaves the build open. Every job would then find a
build for itself, at its own moment, and two jobs of one chain could find two
different builds. So the plan step fixes the key of every step a download
takes, and writes it into the document before any job exists (``planned``):
``keys: {<step id>: <key>}`` into every snapshot entry of another project,
and the build's own key into every configuration of the plugin, which the
build's own uploads, promotes and downloads use.

What a step makes is a folder of the key: a platform (the option ``variant``
of an upload or promote block) and the step id the configuration names at its
top (``step``).

Values in, values out: nothing here talks to the bucket, to git, to Buildkite
or to the agent. The caller hands in the facts as plain values (``Rewrite``,
the plan). A function that writes takes a document and returns a new one; the
document handed in is never changed.

Download entries, plugin configurations and option keys are those of the
``configuration`` module, so the reader of templates, the rule and the rewrite
have one notion of each.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from ..layout import Folder, folder_document
from ..manifest import Manifest
from .configuration import (
    GIT_REF,
    KEY,
    KEYS,
    LATEST,
    PLUGIN,
    PROMOTE,
    STEP,
    STRICT_REF_MATCH,
    UPLOAD,
    VARIANT,
    download_entries,
    entry_steps,
    is_plugin_key,
    mappings,
    named_step,
    option_value,
    plugin_block,
    plugin_configurations,
    reads_as,
)
from .declaration import (
    SNAPSHOT,
    entry_type,
    leaf_steps,
    other_project,
    pipeline_steps,
    step_key,
    step_label,
    step_name,
)
from .plan import Plan, dependencies_first, trigger_label

RESUME_OPTION = "resume"

TRIGGER_KEY_PREFIX = "snapshot-dep-"
RESUME_KEY = "snapshot-resume"
RESUME_LABEL = ":camera_with_flash: snapshot resume"
# As the gate's own steps: before every other step, and never for long.
GATE_PRIORITY = 100
GATE_TIMEOUT_MINUTES = 15


class DocumentError(ValueError):
    """The generated pipeline promotes a folder at two places (``promotions``), or from a
    step without a key (``promote_entries``)."""


# ---------------------------------------------------------------------------
# Loading and dumping
# ---------------------------------------------------------------------------


def load_document(text: str) -> Any:
    """The generated pipeline in ``text``, parsed with PyYAML's safe loader."""
    return yaml.safe_load(text)


def dump_document(doc: Any) -> str:
    """``doc`` as the runner's ``generate`` prints a pipeline: PyYAML's dump, keys sorted.

    For the values the safe loader gives, ``yaml.safe_dump`` writes what
    ``yaml.dump`` writes.
    """
    return yaml.safe_dump(doc)


def dump_written(doc: Any) -> str:
    """A document this module writes from nothing, the trigger and resume steps, as JSON.

    JSON quotes every string: the agent reads YAML 1.2, which reads as
    numbers some strings YAML writes plain, such as a commit of digits with a
    leading 0, or of digits, an ``e`` and digits, and a branch named ``1e3``.
    The generated pipeline is dumped as the runner printed it instead
    (``dump_document``).
    """
    return json.dumps(doc, sort_keys=True)


# ---------------------------------------------------------------------------
# Options, read as the agent reads them
# ---------------------------------------------------------------------------


def named_variant(options: Mapping[Any, Any]) -> str | None:
    """The platform an entry or a block names (``variant``), as text; None when it names none.

    The agent hands a list or a mapping over as variables of other names, so
    the plugin reads no platform from one.
    """
    v = option_value(options, VARIANT)
    if v is None or v == "" or isinstance(v, (dict, list)):
        return None
    return str(v)


def without_options(options: Mapping[Any, Any], names: Iterable[str]) -> dict[Any, Any]:
    """``options`` without every key the agent reads as one of ``names``."""
    dropped = tuple(names)
    return {k: v for k, v in options.items() if not any(reads_as(k, n) for n in dropped)}


def replace_content(target: dict[Any, Any], content: Mapping[Any, Any]) -> None:
    """Give the mapping ``target`` the items of ``content``, keeping it the same object, so
    the document that holds it holds the change."""
    items = list(content.items())
    target.clear()
    target.update(items)


# ---------------------------------------------------------------------------
# Download entries
# ---------------------------------------------------------------------------


def snapshot_dependencies(doc: Any, own: str) -> list[tuple[str, dict[str, Any]]]:
    """``(project, entry)`` of every snapshot dependency entry of ``doc``, in order.

    ``own`` is the pipeline's own project. An entry of another project that
    does not take the latest successful build is one.
    """
    found = []
    for e in download_entries(doc):
        project = other_project(e, own)
        if project is not None and entry_type(e) == SNAPSHOT:
            found.append((project, e))
    return found


def taken_folders(entry: Mapping[Any, Any], steps: Sequence[str]) -> frozenset[Folder]:
    """The folders a snapshot entry takes: its platform and each step it names, or each of
    ``steps``, the dependency's step ids, when it names none."""
    platform = named_variant(entry)
    return frozenset(Folder(platform, s) for s in entry_steps(entry) or steps)


# ---------------------------------------------------------------------------
# The steps that promote and make
# ---------------------------------------------------------------------------


def own_folders(step: Any, block: str) -> list[Folder]:
    """The folders the ``upload`` or ``promote`` blocks of ``step`` name: the block's platform
    and the step id of its configuration, both named; in order. A block makes a folder of the
    pipeline's own project, as the job reads it."""
    found = []
    for config in plugin_configurations(step):
        b = plugin_block(config, block)
        if b is None:
            continue
        platform, step_id = named_variant(b), named_step(config)
        if platform is not None and step_id is not None:
            found.append(Folder(platform, step_id))
    return found


def made_folders(step: Any) -> frozenset[Folder]:
    """What ``step`` makes: every folder its upload or promote blocks name."""
    return frozenset(own_folders(step, UPLOAD) + own_folders(step, PROMOTE))


def step_places(doc: Any) -> list[Any]:
    """The steps of ``doc`` that are not groups, in order: Buildkite runs each place."""
    return list(leaf_steps(pipeline_steps(doc)))


def promoted_twice(f: Folder, first: tuple[int, Any], second: tuple[int, Any]) -> DocumentError:
    """The error of ``f`` promoted at two places, each ``(position, step)``."""
    (p1, s1), (p2, s2) = first, second
    if s1 is s2:
        who = f"the step {step_name(s1, p1)} at two places, steps {p1} and {p2}"
    else:
        who = f"two steps, {step_name(s1, p1)} and {step_name(s2, p2)}"
    return DocumentError(
        f"{f} is promoted by {who}: the first to pass would promote it while the other still "
        "uploads"
    )


def promotions(doc: Any) -> dict[Folder, Any]:
    """``{folder: the step that promotes it}``; its keys are ``D``, what the pipeline makes.

    A folder promoted at two places is an error: the first to pass would
    promote it while the other still uploads.
    """
    found: dict[Folder, tuple[int, Any]] = {}
    for position, step in enumerate(step_places(doc), start=1):
        for f in own_folders(step, PROMOTE):
            first = found.setdefault(f, (position, step))
            if first[0] != position:
                raise promoted_twice(f, first, (position, step))
    return {f: step for f, (_, step) in found.items()}


def promote_entries(doc: Any) -> list[dict[str, str]]:
    """``[{"step", "step_key", "variant"}]`` of the steps that promote: meta-data
    ``snapshot-promotes``, sorted.

    The leave step asks for a step's outcome by its key, so a step that
    promotes and has none is an error.
    """
    keys = {f: step_key(s) for f, s in promotions(doc).items()}
    keyless = sorted(f for f, k in keys.items() if k is None)
    if keyless:
        raise DocumentError(f"the step that promotes {keyless[0]} has no key")
    return [{**folder_document(f), "step_key": k} for f, k in sorted(keys.items())]


# ---------------------------------------------------------------------------
# The facts of a rewrite
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Taken:
    """What the plan fixes of a snapshot dependency: its key, and its step ids."""

    key: str
    steps: tuple[str, ...]


@dataclass(frozen=True)
class Rewrite:
    """What the plan step writes into the generated pipeline of ``own`` before it uploads it.

    ``key`` is the pipeline's own key, None when it has no snapshot;
    ``plugin_commit`` the commit the plan step runs (``git rev-parse HEAD`` of
    the plugin's checkout); ``dependencies`` the key and step ids of each
    snapshot dependency, by project; ``made`` the manifest of each own folder
    that is made at ``key``.
    """

    own: str
    key: str | None
    plugin_commit: str
    dependencies: Mapping[str, Taken]
    made: Mapping[Folder, Manifest]


# ---------------------------------------------------------------------------
# Completion: the steps whose folders are made are replaced
# ---------------------------------------------------------------------------


def is_replaced(step: Any, made: Mapping[Folder, Manifest]) -> bool:
    """True when ``step`` makes at least one folder and every one is made."""
    folders = made_folders(step)
    return bool(folders) and folders <= frozenset(made)


def replaced_steps(doc: Any, made: Mapping[Folder, Manifest]) -> list[Any]:
    """The steps of ``doc`` that completion replaces, in order.

    ``made`` is the manifest of each own folder made at the build's key. A
    step is replaced when it makes at least one folder and every one is in
    ``D`` (``promotions``) and made (``is_replaced``); a made folder outside
    ``D`` replaces nothing.
    """
    in_d = promotions(doc) if made else {}
    done = {f: m for f, m in made.items() if f in in_d}
    return [s for s in step_places(doc) if is_replaced(s, done)]


# What decides whether and when a step runs; a replacement keeps them, so the
# build flows as generated.
FLOW_FIELDS = ("key", "depends_on", "allow_dependency_failure", "if", "branches")


def replacement(step: Any, own: str, made: Mapping[Folder, Manifest]) -> dict[str, Any]:
    """The step that stands in for ``step``: its ``FLOW_FIELDS`` and an echo.

    It has no plugins and no agents, so it runs for a second on the default
    queue, passes, and every rule about its dependents and the gate holds as
    for any step. The label is the step's with the builds that made its
    folders; the command says which build made each.
    """
    folders = sorted(made_folders(step))
    builds = {f: made[f].promoted.build for f in folders}
    numbers = sorted({b.number for b in builds.values()})
    by = f"build {numbers[0]}" if len(numbers) == 1 else f"builds {', '.join(map(str, numbers))}"
    label = step_label(step)
    made_by = f"(made by {by})"
    kept = {f: step[f] for f in FLOW_FIELDS if f in step}
    echoes = (
        f'echo "{f} of {own} was made by build {b.number} {b.url}"' for f, b in builds.items()
    )
    return {
        "label": f"{label} {made_by}" if label else made_by,
        **kept,
        "command": "\n".join(echoes),
    }


def needed_folders(
    doc: Any, own: str, made: Mapping[Folder, Manifest], steps_of: Mapping[str, Sequence[str]]
) -> dict[str, frozenset[Folder]]:
    """``{project: folders}``: what the snapshot entries of the kept steps take, per dependency.

    ``made`` is the manifest of each own folder made at the build's key, as
    ``Rewrite.made``; ``steps_of`` the step ids of each dependency, which an
    entry that names no step takes. A step that completion replaces
    (``replaced_steps``) downloads nothing, so it needs nothing; an entry it
    shares with a kept step is needed. This is what the plan step reads the
    manifests of (step 8), and what a trigger step hands down as
    ``SNAPSHOT_NEEDS``.
    """
    kept = copy.deepcopy(doc)
    for s in replaced_steps(kept, made):
        replace_content(s, {})
    needs: dict[str, frozenset[Folder]] = {}
    for project, e in snapshot_dependencies(kept, own):
        needs[project] = needs.get(project, frozenset()) | taken_folders(e, steps_of[project])
    return needs


# ---------------------------------------------------------------------------
# The rewrite
# ---------------------------------------------------------------------------


def keyed_entry(entry: Mapping[Any, Any], taken: Taken) -> dict[Any, Any]:
    """``entry`` taking each of its steps at the dependency's key: ``keys``, in place of the
    options that would find a build."""
    folders = taken_folders(entry, taken.steps)
    kept = without_options(entry, (STEP, GIT_REF, STRICT_REF_MATCH, LATEST, KEYS))
    return {**kept, KEYS: {f.step: taken.key for f in sorted(folders)}}


def with_key(config: Mapping[Any, Any], key: str) -> dict[Any, Any]:
    """An artifacts configuration that carries the build's key."""
    return {**without_options(config, (KEY,)), KEY: key}


def plugin_key(commit: str) -> str:
    """The key that names the plugin at ``commit``."""
    return f"{PLUGIN}#{commit}"


def with_plugin_commit(m: Mapping[Any, Any], commit: str) -> dict[Any, Any]:
    """The mapping ``m`` with every key that names the plugin naming it at ``commit``."""
    return {plugin_key(commit) if is_plugin_key(k) else k: v for k, v in m.items()}


def planned(doc: Any, r: Rewrite) -> Any:
    """The document to upload: one resolution for every job (section 8).

    ``doc`` is a generated pipeline that keeps the rule
    (``declaration.rule_violations``) and promotes no folder at two places
    (``promotions``).

    - A step that completion replaces (``replaced_steps``) is replaced in its
      place (``replacement``). A made step's files lie at the build's key
      whoever made them, so an entry of the build's own steps stays as
      generated.
    - Every snapshot entry that remains gets ``keys``: the dependency's key
      for each step it takes. A latest successful entry is left as generated.
    - With a key, every configuration of the plugin carries it as ``key``.
    - Every key that names the plugin names it at the plugin commit.
    """
    out = copy.deepcopy(doc)
    for s in replaced_steps(out, r.made):
        replace_content(s, replacement(s, r.own, r.made))
    for project, e in snapshot_dependencies(out, r.own):
        replace_content(e, keyed_entry(e, r.dependencies[project]))
    if r.key is not None:
        for config in plugin_configurations(out):
            if isinstance(config, dict):
                replace_content(config, with_key(config, r.key))
    for m in mappings(out):
        if any(is_plugin_key(k) for k in m):
            replace_content(m, with_plugin_commit(m, r.plugin_commit))
    return out


# ---------------------------------------------------------------------------
# Trigger and resume
# ---------------------------------------------------------------------------


def trigger_key(project: str) -> str:
    return f"{TRIGGER_KEY_PREFIX}{project}"


def needs_value(needs: Iterable[Folder]) -> str:
    """``SNAPSHOT_NEEDS``: the folders a started build must make, as a JSON list of
    ``[<platform>, <step id>]``, sorted."""
    return json.dumps([list(f) for f in sorted(frozenset(needs))])


def read_needs(value: str) -> frozenset[Folder]:
    """The folders ``SNAPSHOT_NEEDS`` names, as ``needs_value`` wrote them."""
    return frozenset(Folder(platform, step) for platform, step in json.loads(value))


def trigger_step(plan: Plan, project: str, needs: Iterable[Folder], after: Sequence[str]) -> dict:
    """The step that builds ``project`` as the plan says and hands down ``needs``.

    ``after`` are the keys of the trigger steps it waits for. ``soft_fail``
    is set: the manifests say whether the dependency is usable, not the
    colour of its build.
    """
    planned_project = plan.projects[project]
    message = f"snapshot dependency of {plan.first.project} build {plan.first.build_number}"
    step: dict[str, Any] = {
        "label": trigger_label(plan, project),
        "key": trigger_key(project),
        "trigger": project,
        "soft_fail": True,
    }
    if after:
        step["depends_on"] = list(after)
    step["build"] = {
        "branch": planned_project.branch,
        "commit": planned_project.commit,
        "message": message,
        "env": {"SNAPSHOT_PLAN": plan.id, "SNAPSHOT_NEEDS": needs_value(needs)},
    }
    return step


def below_in(plan: Plan, project: str) -> frozenset[str]:
    """The projects below ``project`` in ``plan``: all that it reaches through its dependencies."""
    return frozenset(dependencies_first(plan, project)) - {project}


def trigger_steps(plan: Plan, needs: Mapping[str, Iterable[Folder]]) -> list[dict]:
    """One trigger step per dependency of ``needs`` (``{project: folders}``), dependencies first.

    Each waits for the triggers of the dependencies in its own tree.
    """
    below = {p: below_in(plan, p) for p in needs}
    order = [p for p in dependencies_first(plan, plan.first.project) if p in needs]
    return [
        trigger_step(plan, p, needs[p], [trigger_key(q) for q in order if q in below[p]])
        for p in order
    ]


def resume_step(
    command: str, plugin_commit: str, pipeline_path: str, triggers: Sequence[str]
) -> dict:
    """The step that runs the plan step again, as resume, when every trigger has ended.

    ``command`` is the enter step's command, ``pipeline_path`` the file its
    plan option names, ``triggers`` the keys of the trigger steps.
    """
    return {
        "label": RESUME_LABEL,
        "key": RESUME_KEY,
        "priority": GATE_PRIORITY,
        "timeout_in_minutes": GATE_TIMEOUT_MINUTES,
        "depends_on": [{"step": t, "allow_failure": True} for t in triggers],
        "command": command,
        "plugins": [{plugin_key(plugin_commit): {RESUME_OPTION: pipeline_path}}],
    }


def resume_pipeline(
    plan: Plan,
    needs: Mapping[str, Iterable[Folder]],
    command: str,
    plugin_commit: str,
    pipeline_path: str,
) -> dict:
    """The document of step 10: a trigger step per dependency of ``needs``, and the resume step."""
    triggers = trigger_steps(plan, needs)
    keys = [t["key"] for t in triggers]
    return {"steps": [*triggers, resume_step(command, plugin_commit, pipeline_path, keys)]}
