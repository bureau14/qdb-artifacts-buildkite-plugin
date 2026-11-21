"""The generated pipeline as data: what the plan step reads from it and writes into it, and
the trigger and resume steps it uploads.

The pipeline a repository generates says "download quasardb-build,
linux-aarch64-release" and leaves the build open. Every job would then find a
build for itself, at its own moment, and two jobs of one chain could find two
different builds. So the plan step fixes the key of every step a download
takes, and writes it into the document before any job exists (``planned``):
``keys: {<step id>: <key>}`` into every entry of another project, the planned
key of a snapshot dependency or the key read for a download that takes the
latest successful build, and the build's own key into every configuration of
the plugin, which the build's own uploads, promotes and downloads use.

What a step makes is a folder of the key: a platform (the option ``variant``
of an upload or promote block) and the step id the configuration names at its
top (``step``), or that a promote block names, to promote what another step of
the pipeline uploaded.

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
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from ..forms import name_of
from ..layout import Folder, folder_document
from ..manifest import Manifest
from ..options import BRANCH_BUILD, read_bool
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
    block_step,
    download_entries,
    entry_steps,
    is_plugin_key,
    mappings,
    named_variant,
    option_value,
    plugin_block,
    plugin_configurations,
    reads_as,
)
from .declaration import (
    LATEST_SUCCESSFUL,
    SNAPSHOT,
    entry_type,
    leaf_steps,
    other_project,
    pipeline_downloads,
    pipeline_steps,
    step_key,
    step_label,
    step_name,
)
from .plan import (
    LatestDownload,
    LeftOut,
    Plan,
    Planned,
    dependencies_first,
    short_commit,
    trigger_label,
)

RESUME_OPTION = "resume"

TRIGGER_KEY_PREFIX = "snapshot-dep-"
RESUME_KEY = "snapshot-resume"
RESUME_LABEL = ":camera_with_flash: snapshot resume"
# As the gate's own steps: before every other step, and never for long.
GATE_PRIORITY = 100
GATE_TIMEOUT_MINUTES = 15


class DocumentError(ValueError):
    """The generated pipeline promotes a folder at two places (``promoting_places``), or from
    a step without a key (``promote_entries``)."""


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


def dependencies_of(doc: Any, own: str, t: str) -> list[tuple[str, dict[str, Any]]]:
    """``(project, entry)`` of every entry of ``doc`` of another project whose type is ``t``,
    ``SNAPSHOT`` or ``LATEST_SUCCESSFUL``, in order; ``own`` is the pipeline's own project."""
    found = []
    for e in download_entries(doc):
        project = other_project(e, own)
        if project is not None and entry_type(e) == t:
            found.append((project, e))
    return found


def latest_download(entry: Mapping[Any, Any], project: str) -> LatestDownload:
    """What an entry of ``project`` that takes the latest successful build asks the walk over
    latest files: its platform, its ref, whether it tries that ref alone, and the steps it
    names, None for every step."""
    return LatestDownload(
        project,
        named_variant(entry),
        option_value(entry, GIT_REF),
        read_bool(option_value(entry, STRICT_REF_MATCH)),
        entry_steps(entry) or None,
    )


def latest_dependencies(doc: Any, own: str) -> list[tuple[LatestDownload, dict[str, Any]]]:
    """``(download, entry)`` of every entry of ``doc`` of another project that takes the latest
    successful build, in order; ``own`` is the pipeline's own project."""
    return [(latest_download(e, p), e) for p, e in dependencies_of(doc, own, LATEST_SUCCESSFUL)]


def taken_folders(entry: Mapping[Any, Any], steps: Sequence[str]) -> frozenset[Folder]:
    """The folders a snapshot entry takes: its platform and each step it names, or each of
    ``steps``, the dependency's step ids, when it names none."""
    platform = named_variant(entry)
    return frozenset(Folder(platform, s) for s in entry_steps(entry) or steps)


def entry_taking(
    doc: Any, own: str, project: str, steps: Sequence[str], folders: Collection[Folder]
) -> dict[str, Any]:
    """The first snapshot entry of ``project`` in ``doc`` that takes one of ``folders``;
    ``steps`` are the project's step ids, which an entry that names no step takes."""
    return next(
        e
        for p, e in dependencies_of(doc, own, SNAPSHOT)
        if p == project and taken_folders(e, steps) & frozenset(folders)
    )


def outside_the_key(doc: Any, own: str, own_ref: str) -> list[LeftOut]:
    """Every download entry of ``doc`` of the pipeline's own project ``own`` at a ref that is
    not the build's ``own_ref``, each once, in order: its job reads that ref's latest build
    when it runs, and the key leaves it out."""
    found = (
        LeftOut(sd.step, named_variant(e), option_value(e, GIT_REF))
        for sd in pipeline_downloads(doc)
        for e in sd.entries
        if other_project(e, own) is None and option_value(e, GIT_REF) not in (None, "", own_ref)
    )
    return list(dict.fromkeys(found))


def stepless_download(step: str, project: str, n: Planned) -> str:
    """Why step ``step`` cannot download ``project``, planned as ``n``, without naming a step."""
    return (
        f"Step {step} downloads {project} without naming a step, and no template of {project} "
        f"at {short_commit(n.commit)} writes step_id beside a promote, so {project} has no step "
        f"ids at that commit. Bring to {n.ref} of {project} the commit that writes them, or "
        f"remove the download if {project} promotes nothing."
    )


def stepless_downloads(doc: Any, own: str, plan: Plan) -> list[str]:
    """Why each snapshot entry of ``doc`` that names no step breaks the plan: the templates of
    its dependency promote no step id at the commit ``plan`` takes it at, so it would take
    nothing. Each once, in order."""
    found = []
    for sd in pipeline_downloads(doc):
        for e in sd.entries:
            project = other_project(e, own)
            if project is None or entry_type(e) != SNAPSHOT or entry_steps(e):
                continue
            n = plan.projects[project]
            if not n.steps:
                found.append(stepless_download(sd.step, project, n))
    return list(dict.fromkeys(found))


# ---------------------------------------------------------------------------
# The steps that promote and make
# ---------------------------------------------------------------------------


def own_folders(step: Any, block: str) -> list[Folder]:
    """The folders the ``upload`` or ``promote`` blocks of ``step`` name: the block's platform
    and the step id it makes (``block_step``), both named; in order. A block makes a folder of
    the pipeline's own project, as the job reads it."""
    found = []
    for config in plugin_configurations(step):
        b = plugin_block(config, block)
        if b is None:
            continue
        platform, step_id = named_variant(b), block_step(config, block)
        if platform is not None and step_id is not None:
            found.append(Folder(platform, step_id))
    return found


def made_folders(step: Any) -> frozenset[Folder]:
    """What ``step`` makes: every folder its upload or promote blocks name. A step that promotes
    what another step uploaded makes that folder with it, so completion replaces both."""
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
        f"{f} is promoted by {who}. Each step id of a platform is promoted by one step only: "
        "give each its own step_id, or promote it from one of them."
    )


def promoting_places(doc: Any) -> dict[Folder, tuple[int, Any]]:
    """``{folder: (position, step)}`` of the step that promotes each folder.

    A folder promoted at two places is an error: the first to pass would
    promote it while the other still uploads.
    """
    found: dict[Folder, tuple[int, Any]] = {}
    for position, step in enumerate(step_places(doc), start=1):
        for f in own_folders(step, PROMOTE):
            first = found.setdefault(f, (position, step))
            if first[0] != position:
                raise promoted_twice(f, first, (position, step))
    return found


def promotions(doc: Any) -> dict[Folder, Any]:
    """``{folder: the step that promotes it}``; its keys are ``D``, what the pipeline makes."""
    return {f: step for f, (_, step) in promoting_places(doc).items()}


def unwritten_promotions(doc: Any, own: str, steps: Collection[str]) -> list[str]:
    """Why each folder ``doc`` promotes breaks the own check: no template of ``own`` writes its
    step id as ``step_id`` (``steps``, the step ids the declaration of the build's commit
    holds), so the builds that depend on ``own`` would not know it."""
    return [
        f"Step {step_name(s, p)} promotes {f}, and no template of {own} writes step_id: "
        f"{f.step}. The builds that depend on {own} read its steps from those templates, so "
        f"make the step from a template that writes step_id: {f.step}."
        for f, (p, s) in sorted(promoting_places(doc).items())
        if f.step not in steps
    ]


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

    ``key`` is the pipeline's own key; ``plugin_commit`` the commit the plan
    step runs (``git rev-parse HEAD`` of the plugin's checkout);
    ``dependencies`` the key and step ids of each snapshot dependency, by
    project; ``latest`` the key of each step each latest successful download
    takes, as the plan holds them; ``made`` the manifest of each own folder
    that is made at ``key``.
    """

    own: str
    key: str
    plugin_commit: str
    dependencies: Mapping[str, Taken]
    latest: Mapping[LatestDownload, Mapping[str, str]]
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
    manifests of (step 7), and what a trigger step hands down as
    ``SNAPSHOT_NEEDS``.
    """
    kept = copy.deepcopy(doc)
    for s in replaced_steps(kept, made):
        replace_content(s, {})
    needs: dict[str, frozenset[Folder]] = {}
    for project, e in dependencies_of(kept, own, SNAPSHOT):
        needs[project] = needs.get(project, frozenset()) | taken_folders(e, steps_of[project])
    return needs


# ---------------------------------------------------------------------------
# The rewrite
# ---------------------------------------------------------------------------


def keyed_entry(entry: Mapping[Any, Any], keys: Mapping[str, str]) -> dict[Any, Any]:
    """``entry`` taking each step of ``keys`` at its key: ``keys``, in place of the options
    that would find a build."""
    kept = without_options(entry, (STEP, GIT_REF, STRICT_REF_MATCH, LATEST, KEYS))
    return {**kept, KEYS: dict(sorted(keys.items()))}


def planned_keys(entry: Mapping[Any, Any], taken: Taken) -> dict[str, str]:
    """The key of each step a snapshot entry takes: the dependency's planned key."""
    return {f.step: taken.key for f in taken_folders(entry, taken.steps)}


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
    """The document to upload: one resolution for every job.

    ``doc`` is a generated pipeline that keeps the rule
    (``declaration.rule_violations``) and promotes no folder at two places
    (``promotions``).

    - A step that completion replaces (``replaced_steps``) is replaced in its
      place (``replacement``). A made step's files lie at the build's key
      whoever made them, so an entry of the build's own steps stays as
      generated.
    - Every entry of another project that remains gets ``keys``: of a
      snapshot entry the dependency's key for each step it takes, of a latest
      successful entry the key the plan step read for each.
    - Every configuration of the plugin carries the build's key as ``key``.
    - Every key that names the plugin names it at the plugin commit.
    """
    out = copy.deepcopy(doc)
    for s in replaced_steps(out, r.made):
        replace_content(s, replacement(s, r.own, r.made))
    for project, e in dependencies_of(out, r.own, SNAPSHOT):
        replace_content(e, keyed_entry(e, planned_keys(e, r.dependencies[project])))
    for download, e in latest_dependencies(out, r.own):
        replace_content(e, keyed_entry(e, r.latest[download]))
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


def branch_build_value(writes: bool) -> str:
    """``SNAPSHOT_BRANCH_BUILD``: whether a started build writes its branch's latest files, as
    the text ``true`` or ``false`` that ``options.is_branch_build`` reads."""
    return "true" if writes else "false"


def trigger_step(plan: Plan, project: str, needs: Iterable[Folder], after: Sequence[str]) -> dict:
    """The step that builds ``project`` as the plan says and hands down ``needs``, and whether
    the build writes its branch's latest files.

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
        "branch": name_of(planned_project.ref),
        "commit": planned_project.commit,
        "message": message,
        "env": {
            "SNAPSHOT_PLAN": plan.id,
            "SNAPSHOT_NEEDS": needs_value(needs),
            BRANCH_BUILD: branch_build_value(planned_project.writes_latest_file),
        },
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
    """The document of step 9: a trigger step per dependency of ``needs``, and the resume step."""
    triggers = trigger_steps(plan, needs)
    keys = [t["key"] for t in triggers]
    return {"steps": [*triggers, resume_step(command, plugin_commit, pipeline_path, keys)]}
