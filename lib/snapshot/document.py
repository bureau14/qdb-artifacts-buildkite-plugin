"""The generated pipeline as data: what the plan step reads from it and writes into it.

Specification section 8, and the trigger and resume steps of section 7.

The pipeline a repository generates says "download quasardb-build,
linux-aarch64-release" and leaves the build open. Every job would then find a
build for itself, at its own moment, and two jobs of one chain could find two
different builds. So the plan step fixes the build once and writes it into
the document before any job exists (``planned``). A job on any platform then
downloads an explicit build with the code path it already has.

Values in, values out: nothing here talks to the bucket, to git, to Buildkite
or to the agent. The caller hands in the facts as plain values (``Pointer``,
``Rewrite``, the plan). A function that writes takes a document and returns a
new one; the document handed in is never changed. Within one rewrite the copy
keeps the sharing of the original, so an object the document shares through
aliases is rewritten once and stays shared.

Download entries, plugin configurations and option keys are those of the
``configuration`` module, so the reader of templates, the rule and the rewrite
have one notion of each. The upload interpolates ``$``, so every value this
module writes into a document has ``$`` as ``$$`` (``escaped``); text it
copies from the document stays as it is.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from ..forms import is_object_id, is_slug, is_variant
from ..pointer import Pointer
from .configuration import (
    BUILD_ID,
    GIT_REF,
    PLUGIN,
    PROMOTE,
    SNAPSHOT_OPTION,
    STRICT_REF_MATCH,
    UPLOAD,
    VARIANT,
    EntryError,
    download_entries,
    is_plugin_key,
    mappings,
    option_value,
    plugin_block,
    plugin_configurations,
    reads_as,
)
from .declaration import (
    SNAPSHOT,
    STEP_KEY_FIELDS,
    entry_type,
    leaf_steps,
    other_project,
    pipeline_steps,
    step_key,
    step_label,
    step_name,
    yaml_problem,
)
from .plan import Plan, dependencies_first, trigger_label
from .shown import shown_text, shown_value

RESUME_OPTION = "resume"

PROMOTE_KEY_PREFIX = "snapshot-promote-"
TRIGGER_KEY_PREFIX = "snapshot-dep-"
RESUME_KEY = "snapshot-resume"
RESUME_LABEL = ":camera_with_flash: snapshot resume"
# As the gate's own steps: before every other step, and never for long.
GATE_PRIORITY = 100
GATE_TIMEOUT_MINUTES = 15


class DocumentError(ValueError):
    """The generated pipeline cannot be read or written as the specification says."""


# ---------------------------------------------------------------------------
# Loading and dumping
# ---------------------------------------------------------------------------


def load_document(text: str) -> Any:
    """The generated pipeline in ``text``, parsed with PyYAML's safe loader.

    Objects the text shares through anchors and aliases are shared in the
    value. A text the safe loader refuses is a ``DocumentError`` with the
    parser's message.
    """
    try:
        return yaml.safe_load(text)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise DocumentError(f"the generated pipeline does not load: {yaml_problem(exc)}") from exc


def dump_document(doc: Any) -> str:
    """``doc`` as the runner's ``generate`` prints a pipeline: PyYAML's dump, keys sorted.

    For the values the safe loader gives, ``yaml.safe_dump`` writes what
    ``yaml.dump`` writes; a shared object is written once with an anchor.
    """
    try:
        return yaml.safe_dump(doc)
    except (yaml.YAMLError, TypeError) as exc:
        raise DocumentError(f"the pipeline does not dump: {shown_value(str(exc))}") from exc


class QuotedDumper(yaml.SafeDumper):
    """PyYAML's safe dumper, writing every string between double quotes."""


QuotedDumper.add_representer(
    str, lambda dumper, text: dumper.represent_scalar("tag:yaml.org,2002:str", text, style='"')
)


def dump_written(doc: Any) -> str:
    """A document this module writes from nothing, the trigger and resume steps, keys sorted.

    Every string is quoted. The agent reads YAML 1.2, which reads as numbers
    some strings PyYAML writes plain: a commit of digits with a leading 0, or
    of digits, an ``e`` and digits, and a branch named ``1e3``. The generated
    pipeline is dumped as the runner printed it instead (``dump_document``).
    """
    return yaml.dump(doc, Dumper=QuotedDumper)


def escaped(text: str) -> str:
    """``text`` as a value of an uploaded document: the upload turns ``$$`` into ``$``.

    A backslash right before a ``$`` is a ``DocumentError``: the upload also
    reads ``\\$`` as an escaped ``$`` (buildkite-agent 3.118.1), so the ``$$``
    written after it would not arrive as the ``$`` of the text.
    """
    if "\\$" in text:
        raise DocumentError(
            f"{shown_value(text)} holds a backslash before a $, which the upload reads as an "
            "escaped $: it cannot be written as it is"
        )
    return text.replace("$", "$$")


# ---------------------------------------------------------------------------
# Options, read as the agent reads them
# ---------------------------------------------------------------------------


def option(options: Mapping[Any, Any], name: str, where: str) -> Any:
    """``configuration.option_value``, an option given twice with different values an error."""
    try:
        return option_value(options, name)
    except EntryError as exc:
        raise DocumentError(f"{where}: {exc}") from exc


def project_of(options: Mapping[Any, Any], own: str, where: str) -> str | None:
    """The other project a download entry or a block names; None for the own project."""
    try:
        return other_project(options, own)
    except EntryError as exc:
        raise DocumentError(f"{where}: {exc}") from exc


def named_variant(options: Mapping[Any, Any], where: str) -> str | None:
    """The variant an entry or a block names, as the plugin receives it; None when it names none.

    The agent hands a scalar over as its text (``true`` for true), and a list
    or a mapping as variables of other names, so the plugin reads no variant
    from one. The text is as written: it may hold what the upload interpolates.
    """
    v = option(options, VARIANT, where)
    if v is None or v == "" or isinstance(v, (dict, list)):
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def variant_of(options: Mapping[Any, Any], where: str) -> str | None:
    """``named_variant``, which must have the form of a variant (``forms.is_variant``).

    Wherever the plan step names a pointer or a build by a variant: the
    pointer is named after the variant the upload interpolated, which is the
    text as written only when nothing in it is interpolated.
    """
    v = named_variant(options, where)
    if v is not None and not is_variant(v):
        raise DocumentError(f"{where}: the variant {shown_value(v)} is not a variant")
    return v


def without_options(options: Mapping[Any, Any], names: Iterable[str]) -> dict[Any, Any]:
    """``options`` without every key the agent reads as one of ``names``."""
    dropped = tuple(names)
    return {k: v for k, v in options.items() if not any(reads_as(k, v, n) for n in dropped)}


def replace_content(target: dict[Any, Any], content: Mapping[Any, Any]) -> None:
    """Give the mapping ``target`` the items of ``content``, keeping it the same object.

    Every place of the document that shares ``target`` then sees the change:
    a shared object is rewritten once and stays shared.
    """
    items = list(content.items())
    target.clear()
    target.update(items)


# ---------------------------------------------------------------------------
# Download entries
# ---------------------------------------------------------------------------


def entries_of(doc: Any) -> list[dict[str, Any]]:
    """``configuration.download_entries``, a block given twice with different values an error."""
    try:
        return download_entries(doc)
    except EntryError as exc:
        raise DocumentError(f"a download block: {exc}") from exc


def snapshot_dependencies(doc: Any, own: str) -> list[tuple[str, dict[str, Any]]]:
    """``(project, entry)`` of every snapshot dependency entry of ``doc``, each once, in order.

    ``own`` is the pipeline's own project. An entry of another project that
    names no build is one; a latest successful entry names its build.
    """
    found = []
    for e in entries_of(doc):
        project = project_of(e, own, "a download entry")
        if project is not None and entry_type(e) == SNAPSHOT:
            found.append((project, e))
    return found


def own_downloads(doc: Any, own: str) -> list[dict[str, Any]]:
    """The download entries of ``doc`` from the pipeline's own project, each once."""
    return [e for e in entries_of(doc) if project_of(e, own, "a download entry") is None]


def entry_variant(entry: Mapping[Any, Any], project: str) -> str:
    """The variant a download entry of ``project`` names; an entry that names none is an error."""
    v = variant_of(entry, f"a download entry of {project}")
    if v is None:
        raise DocumentError(f"a download entry of {project} names no variant")
    return v


# ---------------------------------------------------------------------------
# The steps that promote and produce
# ---------------------------------------------------------------------------


def unique_steps(doc: Any) -> list[Any]:
    """The steps of ``doc`` that are not groups, in order, a step shared by two places once."""
    found: dict[int, Any] = {}
    for s in leaf_steps(pipeline_steps(doc)):
        found.setdefault(id(s), s)
    return list(found.values())


def own_blocks(step: Any, own: str, block: str) -> list[dict[str, Any]]:
    """The ``upload`` or ``promote`` blocks of ``step`` that are of the own project."""
    where = f"the {block} block of a step"
    found = []
    for config in plugin_configurations(step):
        try:
            b = plugin_block(config, block)
        except EntryError as exc:
            raise DocumentError(f"{where}: {exc}") from exc
        if b is not None and project_of(b, own, where) is None:
            found.append(b)
    return found


def block_variants(step: Any, own: str, block: str) -> list[str]:
    """The variants the own ``block`` blocks of ``step`` name (``named_variant``), in order."""
    where = f"the {block} block of a step"
    named = (named_variant(b, where) for b in own_blocks(step, own, block))
    return [v for v in named if v is not None]


def promoted_variants(step: Any, own: str) -> list[str]:
    """The variants the own promote blocks of ``step`` name, of the form of a variant, in order."""
    named = (variant_of(b, "a promote block") for b in own_blocks(step, own, PROMOTE))
    return [v for v in named if v is not None]


def produced_variants(step: Any, own: str) -> frozenset[str]:
    """What ``step`` produces: every variant its own upload or promote blocks name."""
    return frozenset(block_variants(step, own, UPLOAD) + block_variants(step, own, PROMOTE))


def step_places(doc: Any) -> list[Any]:
    """The steps of ``doc`` that are not groups, at every place: Buildkite runs each place.

    A step that aliases put at two places is there twice.
    """
    return list(leaf_steps(pipeline_steps(doc)))


def promoted_twice(v: str, first: tuple[int, Any], second: tuple[int, Any]) -> DocumentError:
    """The error of ``v`` promoted at two places, each ``(position, step)``."""
    (p1, s1), (p2, s2) = first, second
    if s1 is s2:
        who = f"the step {shown_text(step_name(s1, p1))} at two places, steps {p1} and {p2}"
    else:
        who = f"two steps, {shown_text(step_name(s1, p1))} and {shown_text(step_name(s2, p2))}"
    return DocumentError(
        f"{v} is promoted by {who}: the first to pass would write its pointer while the other "
        "still uploads"
    )


def promotions(doc: Any, own: str) -> dict[str, Any]:
    """``{variant: the step that promotes it}``; its keys are ``D``, what the pipeline can give.

    Each is of the form of a variant, as it names a pointer. A variant
    promoted at two places is an error, also by one step that aliases put at
    two places: the first to pass would write its pointer while the other
    still uploads.
    """
    found: dict[str, tuple[int, Any]] = {}
    for position, step in enumerate(step_places(doc), start=1):
        for v in promoted_variants(step, own):
            first = found.setdefault(v, (position, step))
            if first[0] != position:
                raise promoted_twice(v, first, (position, step))
    return {v: step for v, (_, step) in found.items()}


def with_promote_keys(doc: Any, own: str) -> Any:
    """``doc`` with a key for every step that promotes and has none.

    The leave step asks for a step's outcome by its key. The keys are
    ``snapshot-promote-<n>``, ``n`` counting such steps from 1 in the order of
    the document, whichever variants exist. A variant promoted at two places
    is an error (``promotions``): the key of a step at two places would be
    one key of two steps.
    """
    promotions(doc, own)
    out = copy.deepcopy(doc)
    promoting = (s for s in unique_steps(out) if block_variants(s, own, PROMOTE))
    keyless = [s for s in promoting if step_key(s) is None]
    for n, step in enumerate(keyless, start=1):
        step["key"] = f"{PROMOTE_KEY_PREFIX}{n}"
    return out


def promote_keys(doc: Any, own: str) -> dict[str, str]:
    """``{variant: step key}`` of the steps that promote: meta-data ``snapshot-promotes``."""
    keys = {v: step_key(s) for v, s in promotions(doc, own).items()}
    keyless = sorted(v for v, k in keys.items() if k is None)
    if keyless:
        raise DocumentError(f"the step that promotes {keyless[0]} has no key")
    return {v: k for v, k in keys.items() if k is not None}


# ---------------------------------------------------------------------------
# The facts of a rewrite
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rewrite:
    """What the plan step writes into the generated pipeline of ``own`` before it uploads it.

    ``key`` is the pipeline's own key, None when it has no snapshot;
    ``plugin_commit`` the commit the plan step runs (``git rev-parse HEAD`` of
    the plugin's checkout); ``dependencies`` the pointer of each snapshot
    dependency and variant the entries name, by ``(project, variant)``;
    ``existing`` the pointer of each own variant at ``key``, by variant. Each
    pointer is one that ``pointer.find`` read, which checked the form of every
    field that goes into an entry or a step command.
    """

    own: str
    key: str | None
    plugin_commit: str
    dependencies: Mapping[tuple[str, str], Pointer]
    existing: Mapping[str, Pointer]

    def __post_init__(self) -> None:
        if not is_slug(self.own):
            raise ValueError(f"project {shown_value(self.own)} is not a pipeline slug")
        if self.key is None and self.existing:
            raise ValueError("own variants exist only at a key, and this rewrite has none")
        if not is_object_id(self.plugin_commit):
            raise ValueError(f"plugin commit {shown_value(self.plugin_commit)} is not a commit")


# ---------------------------------------------------------------------------
# Completion: the steps of variants that exist are replaced
# ---------------------------------------------------------------------------


def is_replaced(step: Any, own: str, existing: Mapping[str, Pointer]) -> bool:
    """True when ``step`` produces at least one variant and every one exists."""
    produced = produced_variants(step, own)
    return bool(produced) and produced <= frozenset(existing)


def replaced_steps(doc: Any, own: str, existing: Mapping[str, Pointer]) -> list[Any]:
    """The steps of ``doc`` that completion replaces, each once, in order.

    ``existing`` is the pointer of each own variant at the build's key. A step
    is replaced when it produces at least one variant and every one is in
    ``D`` (``promotions``) and exists (``is_replaced``); an existing variant
    outside ``D`` replaces nothing.
    """
    in_d = promotions(doc, own) if existing else {}
    done = {v: p for v, p in existing.items() if v in in_d}
    return [s for s in unique_steps(doc) if is_replaced(s, own, done)]


# What decides whether and when a step runs; a replacement keeps them, so the
# build flows as generated.
FLOW_FIELDS = (*STEP_KEY_FIELDS, "depends_on", "allow_dependency_failure", "if", "branches")


def replacement(step: Any, own: str, existing: Mapping[str, Pointer]) -> dict[str, Any]:
    """The step that stands in for ``step``: its ``FLOW_FIELDS`` and an echo.

    It has no plugins and no agents, so it runs for a second on the default
    queue, passes, and every rule about its dependents and the gate holds as
    for any step. The label is the step's with the builds that hold its
    variants; the command says where each variant is.
    """
    produced = sorted(produced_variants(step, own))
    builds = {v: existing[v].build for v in produced}
    numbers = sorted({b.number for b in builds.values()})
    where = f"build {numbers[0]}" if len(numbers) == 1 else f"builds {', '.join(map(str, numbers))}"
    label = step_label(step)
    exists = escaped(f"(exists in {where})")
    kept = {f: step[f] for f in FLOW_FIELDS if f in step}
    echoes = (f'echo "{v} of {own} exists in build {b.number} {b.url}"' for v, b in builds.items())
    return {
        "label": f"{label} {exists}" if label else exists,
        **kept,
        "command": escaped("\n".join(echoes)),
    }


def rewired_variants(steps: Sequence[Any], own: str, replaced: Iterable[Any]) -> frozenset[str]:
    """The variants uploaded by at least one step of ``steps``, and only by replaced steps."""
    gone = {id(s) for s in replaced}
    uploaders: dict[str, list[Any]] = {}
    for s in steps:
        for v in block_variants(s, own, UPLOAD):
            uploaders.setdefault(v, []).append(s)
    return frozenset(v for v, by in uploaders.items() if all(id(s) in gone for s in by))


def needed_variants(
    doc: Any, own: str, existing: Mapping[str, Pointer]
) -> dict[str, frozenset[str]]:
    """``{project: variants}``: what the snapshot entries of the kept steps name, per dependency.

    ``existing`` is the pointer of each own variant at the build's key, as
    ``Rewrite.existing``. A step that completion replaces
    (``replaced_steps``) downloads nothing, so it needs nothing; an entry it
    shares with a kept step is needed. This is what the plan step looks up
    pointers of (step 8), and what a trigger step hands down as
    ``SNAPSHOT_NEEDS``.
    """
    kept = copy.deepcopy(doc)
    for s in replaced_steps(kept, own, existing):
        replace_content(s, {})
    needs: dict[str, set[str]] = {}
    for project, e in snapshot_dependencies(kept, own):
        needs.setdefault(project, set()).add(entry_variant(e, project))
    return {p: frozenset(v) for p, v in needs.items()}


# ---------------------------------------------------------------------------
# The rewrite
# ---------------------------------------------------------------------------


def resolved_entry(entry: Mapping[Any, Any], p: Pointer) -> dict[Any, Any]:
    """``entry`` downloading exactly the build of ``p``: its build id, its ref, strict matching."""
    kept = without_options(entry, (GIT_REF, STRICT_REF_MATCH, BUILD_ID))
    return {
        **kept,
        BUILD_ID: escaped(p.build.id),
        GIT_REF: escaped(p.git_ref),
        STRICT_REF_MATCH: True,
    }


def with_snapshot(block: Mapping[Any, Any], key: str) -> dict[Any, Any]:
    """An upload or promote block that carries the key ``key``."""
    return {**without_options(block, (SNAPSHOT_OPTION,)), SNAPSHOT_OPTION: escaped(key)}


def plugin_key(commit: str) -> str:
    """The key that names the plugin at ``commit``."""
    return f"{PLUGIN}#{escaped(commit)}"


def with_plugin_commit(m: Mapping[Any, Any], commit: str) -> dict[Any, Any]:
    """The mapping ``m`` with every key that names the plugin naming it at ``commit``."""
    named = [k for k in m if is_plugin_key(k)]
    if len(named) > 1:
        shown = " and ".join(shown_value(k) for k in named)
        raise DocumentError(f"two keys of one mapping name the plugin, {shown}")
    return {plugin_key(commit) if is_plugin_key(k) else k: v for k, v in m.items()}


def dependency_pointer(project: str, entry: Mapping[Any, Any], r: Rewrite) -> Pointer:
    """The pointer a snapshot entry of ``project`` downloads by: the one of its variant."""
    variant = entry_variant(entry, project)
    p = r.dependencies.get((project, variant))
    if p is None:
        raise DocumentError(f"no build of {project} {variant} was handed in for its entry")
    return p


def planned(doc: Any, r: Rewrite) -> Any:
    """The document to upload: one resolution for every job (section 8).

    ``doc`` is a generated pipeline that keeps the rule
    (``declaration.rule_violations``), so it writes no ``snapshot`` option
    itself and every plugin configuration in it can be read.

    - With a key, a variant promoted at two places is an error
      (``promotions``); a step that promotes and has no key gets one
      (``with_promote_keys``); a step that completion replaces
      (``replaced_steps``) is replaced in its place (``replacement``), and an
      own download entry of a variant whose uploading steps were all replaced
      gets that variant's build, ref and strict matching.
    - Every snapshot entry that remains gets the build, ref and strict
      matching handed in for its project and variant (``needed_variants``
      names them). A latest successful entry is left as generated.
    - With a key, every own upload and promote block gets ``snapshot: <key>``.
    - Every key that names the plugin names it at the plugin commit.

    Without a key no step gets a key: only the leave step of a build with a
    key asks for outcomes.
    """
    out = copy.deepcopy(doc) if r.key is None else with_promote_keys(doc, r.own)
    replaced = replaced_steps(out, r.own, r.existing)
    rewired = rewired_variants(unique_steps(out), r.own, replaced)
    for s in replaced:
        replace_content(s, replacement(s, r.own, r.existing))
    for e in own_downloads(out, r.own):
        v = named_variant(e, "an own download entry")
        if v in rewired:
            replace_content(e, resolved_entry(e, r.existing[v]))
    for project, e in snapshot_dependencies(out, r.own):
        replace_content(e, resolved_entry(e, dependency_pointer(project, e, r)))
    if r.key is not None:
        for s in unique_steps(out):
            for block in (*own_blocks(s, r.own, UPLOAD), *own_blocks(s, r.own, PROMOTE)):
                replace_content(block, with_snapshot(block, r.key))
    for m in mappings(out):
        if any(is_plugin_key(k) for k in m):
            replace_content(m, with_plugin_commit(m, r.plugin_commit))
    return out


# ---------------------------------------------------------------------------
# Trigger and resume
# ---------------------------------------------------------------------------


def trigger_key(project: str) -> str:
    return f"{TRIGGER_KEY_PREFIX}{project}"


def trigger_step(plan: Plan, project: str, needs: Iterable[str], after: Sequence[str]) -> dict:
    """The step that builds ``project`` as the plan says and hands down ``needs``.

    ``after`` are the keys of the trigger steps it waits for. ``soft_fail``
    is set: the pointer says whether the dependency is usable, not the colour
    of its build.
    """
    node = plan.nodes[project]
    wanted = sorted(frozenset(needs))
    if not all(is_variant(v) for v in wanted):
        raise DocumentError(f"{project}: the variants {shown_value(wanted)} are not all variants")
    message = f"snapshot dependency of {plan.root.project} build {plan.root.build_number}"
    step: dict[str, Any] = {
        "label": escaped(trigger_label(plan, project)),
        "key": trigger_key(project),
        "trigger": project,
        "soft_fail": True,
    }
    if after:
        step["depends_on"] = list(after)
    step["build"] = {
        "branch": escaped(node.label),
        "commit": node.commit,
        "message": escaped(message),
        "env": {"SNAPSHOT_PLAN": plan.id, "SNAPSHOT_NEEDS": escaped(json.dumps(wanted))},
    }
    return step


def below_in(plan: Plan, project: str) -> frozenset[str]:
    """The nodes below ``project`` in ``plan``: all that it reaches through its dependencies."""
    if project not in plan.nodes:
        raise DocumentError(f"{project} is not a node of plan {plan.id}")
    return frozenset(dependencies_first(plan, project)) - {project}


def trigger_steps(plan: Plan, needs: Mapping[str, Iterable[str]]) -> list[dict]:
    """One trigger step per dependency of ``needs`` (``{project: variants}``), dependencies first.

    Each waits for the triggers of the dependencies in its own tree.
    """
    below = {p: below_in(plan, p) for p in needs}
    order = [p for p in dependencies_first(plan, plan.root.project) if p in needs]
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
        "command": escaped(command),
        "plugins": [{plugin_key(plugin_commit): {RESUME_OPTION: escaped(pipeline_path)}}],
    }


def resume_pipeline(
    plan: Plan,
    needs: Mapping[str, Iterable[str]],
    command: str,
    plugin_commit: str,
    pipeline_path: str,
) -> dict:
    """The document of step 10: a trigger step per dependency of ``needs``, and the resume step."""
    triggers = trigger_steps(plan, needs)
    keys = [t["key"] for t in triggers]
    return {"steps": [*triggers, resume_step(command, plugin_commit, pipeline_path, keys)]}
