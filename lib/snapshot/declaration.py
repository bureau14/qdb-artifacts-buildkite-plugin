"""The declaration of a pipeline and the rule that keeps it true (specification section 2).

A pipeline depends on what its committed step templates download: every
download entry of the artifacts plugin with a literal project id of another
pipeline gives one entry ``(project, type, pin)``. An entry that takes the
latest successful build (``latest: true``) is a **latest successful**
dependency, any other a **snapshot** dependency. The same templates give the
pipeline's step ids: what every template step that promotes promotes, its
promote block's ``step`` or else its own ``step_id``, when some template
writes that id as ``step_id``.
``read_declaration`` reads both from the files of one commit without running
anything, for the build's own checkout and for another repository's commit
alike; reading the files is the caller's business. ``rule_violations``
compares the download entries of a generated pipeline with that declaration,
and holds every upload and promote of it to a step id.
Both read the plugin's configuration by its structure (the ``configuration``
module), so what the reader counts and what the rule checks are one notion.

A snapshot tree never contains a latest successful dependency: a declaration
that holds one has no snapshot (``has_snapshot``).

A template that does not parse, or that writes a ``step_id`` that is not a
step id, raises ``TemplateError``, which names the project and the file.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import yaml

from ..layout import is_step_id
from .configuration import (
    GIT_REF,
    PLUGIN,
    PROJECT_ID,
    PROMOTE,
    UPLOAD,
    block_step,
    download_entries,
    misnamed_plugins,
    named_step,
    named_variant,
    option_value,
    plugin_block,
    plugin_configurations,
    takes_latest,
)

BUILDKITE_DIR = ".buildkite/"
TEMPLATE_SUFFIXES = (".yml", ".yaml")
# What a template writes in a step in place of its key; the runner's
# load_template turns it into the key and the plugin's step.
STEP_ID = "step_id"

# The two types of dependency.
SNAPSHOT = "snapshot"
LATEST_SUCCESSFUL = "latest successful"


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One dependency: the project a template downloads from, its type and its pin.

    ``type`` is ``SNAPSHOT``, or ``LATEST_SUCCESSFUL`` for an entry that takes
    the latest successful build (``takes_latest``). ``pin`` is a snapshot
    entry's git ref when that is a literal string; None when it has no git ref
    or one that is not literal, and always for a latest successful entry.
    ``path`` is the first template the entry was found in, for messages; it is
    not part of what makes two entries one.
    """

    project: str
    type: str = SNAPSHOT
    pin: str | None = None
    path: str = field(default="", compare=False)


@dataclass(frozen=True)
class Declaration:
    """The entries of one commit's templates, each once, sorted, and the step ids its
    templates promote, sorted."""

    entries: tuple[Entry, ...] = ()
    steps: tuple[str, ...] = ()


class TemplateError(ValueError):
    """A template of the reading rule that does not parse as YAML."""


@dataclass(frozen=True)
class StepDownloads:
    """The download entries of one step of a pipeline, and how messages name the step.

    ``problems`` are what of the step's plugin configuration breaks the rule
    beside its entries: a key that names the plugin another way, an upload or
    promote without a step id (``step_downloads``).
    """

    step: str
    entries: tuple[dict[str, Any], ...]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Violation:
    """A part of a generated pipeline that the declaration does not allow: its step and why."""

    step: str
    reason: str


def declared_projects(d: Declaration, t: str = SNAPSHOT) -> frozenset[str]:
    """The projects ``d`` depends on with dependencies of type ``t``."""
    return frozenset(e.project for e in d.entries if e.type == t)


def declared_entries(d: Declaration, t: str) -> tuple[Entry, ...]:
    """The entries of type ``t``, in the declaration's order."""
    return tuple(e for e in d.entries if e.type == t)


def declared_pins(d: Declaration, project: str) -> frozenset[str]:
    """The pins ``d`` declares for ``project``; more than one is the planner's error to report."""
    return frozenset(e.pin for e in d.entries if e.project == project and e.pin is not None)


def declared_path(d: Declaration, project: str, t: str) -> str:
    """The template in which ``d`` declares ``project`` as a dependency of type ``t``, the
    first found, for messages that name the entry to change."""
    return next(e.path for e in d.entries if e.project == project and e.type == t)


def has_snapshot(d: Declaration) -> bool:
    """False when ``d`` holds a latest successful dependency: a snapshot never contains one."""
    return not declared_entries(d, LATEST_SUCCESSFUL)


def violation_message(v: Violation) -> str:
    """How a violation is shown in the job log and the annotation."""
    return f"step {v.step}: {v.reason}"


# ---------------------------------------------------------------------------
# One download entry
# ---------------------------------------------------------------------------


def other_project(entry: Mapping[str, Any], own: str) -> str | None:
    """The project of another pipeline that ``entry`` downloads from; None for ``own``.

    An entry without a project id, or with a null or empty one, downloads from
    the pipeline's own project, as the artifacts plugin reads it.
    """
    v = option_value(entry, PROJECT_ID)
    return None if v is None or v == "" or v == own else v


def entry_type(entry: Mapping[str, Any]) -> str:
    """``LATEST_SUCCESSFUL`` for an entry that takes the latest successful build, which is
    not planned, not triggered and not part of a key, and which the plan step leaves as
    generated; else ``SNAPSHOT``."""
    return LATEST_SUCCESSFUL if takes_latest(entry) else SNAPSHOT


def is_literal(text: str) -> bool:
    """True for text that no substitution changes: it has no ``{`` and no ``$``."""
    return "{" not in text and "$" not in text


def literal_pin(value: Any) -> str | None:
    """A git ref as a pin: a non-empty literal string; None for anything else."""
    return value if isinstance(value, str) and value and is_literal(value) else None


# ---------------------------------------------------------------------------
# The steps of a generated pipeline
# ---------------------------------------------------------------------------


def pipeline_steps(doc: Any) -> list[Any]:
    """The top-level steps of a pipeline document: its ``steps``."""
    return doc["steps"]


def is_group(step: Any) -> bool:
    return isinstance(step, dict) and "group" in step and isinstance(step.get("steps"), list)


def leaf_steps(steps: Sequence[Any]) -> Iterator[Any]:
    """Every step that is not a group, in order, the steps of a group in its place."""
    for s in steps:
        if is_group(s):
            yield from leaf_steps(s["steps"])
        else:
            yield s


def step_key(step: Any) -> str | None:
    """The key of a step; None when it has none (a ``wait`` step is text)."""
    return step.get("key") if isinstance(step, dict) else None


def step_label(step: Any) -> str | None:
    """The label of a step; None when it has none."""
    return step.get("label") if isinstance(step, dict) else None


def step_name(step: Any, position: int) -> str:
    """How messages name a step: its key, else its label, else ``step <position>``."""
    return step_key(step) or step_label(step) or f"step {position}"


def step_id_problem(config: Any, block: str) -> str | None:
    """Why the ``upload`` or ``promote`` block of an artifacts configuration breaks the rule:
    it names a platform and no step id, or one that is not a step id, so no job and no plan
    step knows the folder it makes."""
    b = plugin_block(config, block)
    platform = None if b is None else named_variant(b)
    if platform is None:
        return None
    value = block_step(config, block)
    if value is None:
        act = "uploads" if block == UPLOAD else "promotes"
        return (
            f"{act} {platform} and names no step id. Its template writes step_id in place of "
            "key, with the .buildkite/tools of that change (merge master)."
        )
    if not is_step_id(value):
        return (
            f'{value} is not a step id. A step id is lower-case letters, digits and "_", and '
            "starts with a letter."
        )
    return None


def step_id_problems(step: Any) -> list[str]:
    """Why each upload or promote block of ``step`` breaks the rule (``step_id_problem``)."""
    configs = plugin_configurations(step)
    found = (step_id_problem(c, b) for c in configs for b in (UPLOAD, PROMOTE))
    return [p for p in found if p is not None]


def step_downloads(step: Any, name: str) -> StepDownloads:
    """The download entries of ``step``, which messages name ``name``, and its plugin problems:
    the keys that name the plugin another way (``misnamed_plugins``), and the upload and
    promote blocks without a step id (``step_id_problems``)."""
    problems = (*misnamed_plugins(step), *step_id_problems(step))
    return StepDownloads(name, tuple(download_entries(step)), problems)


def pipeline_downloads(doc: Any) -> list[StepDownloads]:
    """Every step of ``doc`` with download entries or plugin problems, groups descended.

    ``doc`` is a parsed generated pipeline. Positions in names count every
    step that is not a group, from 1.
    """
    steps = enumerate(leaf_steps(pipeline_steps(doc)), start=1)
    found = (step_downloads(s, step_name(s, i)) for i, s in steps)
    return [sd for sd in found if sd.entries or sd.problems]


# ---------------------------------------------------------------------------
# The reading rule
# ---------------------------------------------------------------------------


def is_template_path(path: str) -> bool:
    """True for a path the reading rule looks at.

    ``path`` is relative to the repository root and ``/``-separated: a
    ``*.yml`` or ``*.yaml`` file under ``.buildkite/``. The files are those of
    a commit, never of a working tree, so the pipeline a job generates is not
    among them unless it is committed, and the tools submodule is not
    descended into.
    """
    return path.startswith(BUILDKITE_DIR) and path.endswith(TEMPLATE_SUFFIXES)


def mentions_plugin(content: str | bytes) -> bool:
    """True when the file's text contains the plugin's name. A file that does not is never
    parsed."""
    return (PLUGIN.encode("ascii") if isinstance(content, bytes) else PLUGIN) in content


def parse_template(own: str, path: str, content: str | bytes) -> Any:
    """The parsed YAML of one template of ``own``, with a safe loader.

    ``{placeholder}`` text is left alone. A template that does not parse
    raises ``TemplateError``.
    """
    try:
        return yaml.safe_load(content)
    except yaml.YAMLError as exc:
        raise TemplateError(f"{own}: {path} does not parse as YAML: {exc}") from exc


def read_entry(path: str, entry: Mapping[str, Any], own: str) -> Entry | None:
    """The dependency one download entry of a template declares; None for the own project.

    An entry that takes the latest successful build is a latest successful
    dependency, without a pin.
    """
    project = other_project(entry, own)
    if project is None:
        return None
    if takes_latest(entry):
        return Entry(project, LATEST_SUCCESSFUL, None, path)
    return Entry(project, SNAPSHOT, literal_pin(option_value(entry, GIT_REF)), path)


def template_steps(doc: Any) -> list[dict[str, Any]]:
    """The steps of a template: the mapping it holds, and each of its ``steps``, groups
    descended."""
    if not isinstance(doc, dict):
        return []
    inner = doc.get("steps")
    listed = list(leaf_steps(inner)) if isinstance(inner, list) else []
    return [doc, *(s for s in listed if isinstance(s, dict))]


def step_id_of(own: str, path: str, step: Mapping[str, Any]) -> str | None:
    """The ``step_id`` a template step writes, None when it writes none; one that is not a
    step id raises ``TemplateError``, as a template that does not parse does."""
    value = step.get(STEP_ID)
    if value is None or is_step_id(value):
        return value
    raise TemplateError(
        f'{own}: {path}: step_id {value} is not a step id. Write lower-case letters, digits and "_", '
        "starting with a letter."
    )


def promoted_ids(step: Mapping[str, Any], step_id: str | None) -> list[str]:
    """The step ids a template step promotes, whose ``step_id`` is ``step_id``: of each promote
    block of its artifacts configurations, the block's own ``step``, which names what another
    step uploaded, else ``step_id``."""
    blocks = (plugin_block(c, PROMOTE) for c in plugin_configurations(step))
    named = (named_step(b) or step_id for b in blocks if b is not None)
    return [i for i in named if i]


@dataclass(frozen=True)
class TemplateRead:
    """What one template that mentions the plugin declares: its entries, the step ids its steps
    promote, and the step ids its steps write as ``step_id``."""

    entries: tuple[Entry, ...]
    promoted: tuple[str, ...]
    written: tuple[str, ...]


def read_template(path: str, content: str | bytes, own: str) -> TemplateRead:
    """What one template that mentions the plugin declares."""
    doc = parse_template(own, path, content)
    found = (read_entry(path, e, own) for e in download_entries(doc))
    steps = [(step_id_of(own, path, s), s) for s in template_steps(doc)]
    return TemplateRead(
        tuple(x for x in found if x is not None),
        tuple(p for i, s in steps for p in promoted_ids(s, i)),
        tuple(i for i, _ in steps if i),
    )


def _entry_order(e: Entry) -> tuple[str, str, str]:
    return e.project, e.type, e.pin or ""


def read_declaration(files: Mapping[str, str | bytes], own: str) -> Declaration:
    """The declaration of pipeline ``own`` in ``files``.

    ``files`` maps paths relative to the repository root to their content,
    for the files of one commit's tree under ``.buildkite/``; other paths are
    ignored. A file counts when ``is_template_path`` holds and its text
    contains ``PLUGIN`` (``mentions_plugin``). In every such file each download entry
    (``download_entries``) with a project id gives one entry ``(project,
    type, pin)``: a latest successful one when it takes the latest successful
    build (``takes_latest``), else a snapshot one. Entries of ``own`` and
    entries without a project id are dropped. Files are read in path order;
    entries are kept once each, sorted, with the first file each was found
    in. Every step that promotes gives the step id it promotes
    (``promoted_ids``), which counts when some template writes it as
    ``step_id``; kept once each, sorted: the step ids a build of this commit
    makes, never a Buildkite key or the plugin's top-level ``step`` of a
    pipeline written in YAML by hand.
    """
    read = [
        read_template(path, files[path], own)
        for path in sorted(files)
        if is_template_path(path) and mentions_plugin(files[path])
    ]
    entries = [e for r in read for e in r.entries]
    written = {i for r in read for i in r.written}
    steps = {i for r in read for i in r.promoted if i in written}
    return Declaration(
        tuple(sorted(dict.fromkeys(entries), key=_entry_order)), tuple(sorted(steps))
    )


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def _shown_ref(value: Any) -> str:
    return "no git ref" if value is None else repr(value)


def _ref_problem(project: str, ref: Any, pins: frozenset[str], own_ref: str) -> str | None:
    if isinstance(ref, str) and (ref == own_ref or ref in pins):
        return None
    declared = f" (declared: {', '.join(sorted(pins))})" if pins else ""
    return (
        f"downloads {project} at {_shown_ref(ref)}, which is neither the build's own ref "
        f"{own_ref} nor a pin declared for {project}{declared}"
    )


def entry_problem(
    entry: Mapping[str, Any], declaration: Declaration, own: str, own_ref: str
) -> str | None:
    """Why one download entry breaks the rule; None when it does not.

    The entry's type must be the type a template declares the project with.
    A latest successful entry is left as generated, so its ref is not looked at.
    """
    project = other_project(entry, own)
    if project is None:
        return None
    t = entry_type(entry)
    if project not in declared_projects(declaration, t):
        why = f"which no committed template declares as a {t} dependency"
        return f"downloads from {project} as a {t} dependency, {why}"
    if t == LATEST_SUCCESSFUL:
        return None
    ref = option_value(entry, GIT_REF)
    return _ref_problem(project, ref, declared_pins(declaration, project), own_ref)


def rule_violations(
    downloads: Sequence[StepDownloads], declaration: Declaration, own: str, own_ref: str
) -> list[Violation]:
    """The parts of a generated pipeline that the declaration does not allow.

    ``downloads`` comes from ``pipeline_downloads``; ``own`` is the pipeline's
    own project and ``own_ref`` the build's own git ref, in the form the
    generator writes it (``refs/heads/<branch>``). A step's plugin problems
    (``step_downloads``) are violations. Every entry of another project must
    have its project declared in ``declaration`` with the same type; a
    snapshot entry also a git ref that is ``own_ref`` or a pin declared for
    that project. The declaration may hold more than the pipeline uses. Each
    violation is reported once, in order.
    """

    def of_step(sd: StepDownloads) -> list[Violation]:
        found = (entry_problem(e, declaration, own, own_ref) for e in sd.entries)
        return [Violation(sd.step, p) for p in (*sd.problems, *found) if p is not None]

    return list(dict.fromkeys(v for sd in downloads for v in of_step(sd)))
