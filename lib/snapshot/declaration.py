"""The declaration of a pipeline and the rule that keeps it true (specification section 2).

A pipeline depends on what its committed step templates download: every
download entry of the artifacts plugin with a literal project id of another
pipeline gives one entry ``(project, type, pin)``. An entry that names its
build (``names_build``) is a **latest successful** dependency, any other a
**snapshot** dependency. ``read_declaration`` reads those entries from the
files of one commit without running anything, for the build's own checkout and
for another repository's commit alike; reading the files is the caller's
business. ``rule_violations`` compares the download entries of a generated
pipeline with that declaration. Both read the plugin's configuration by its
structure (the ``configuration`` module), so what the reader counts and what
the rule checks are one notion.

A snapshot tree never contains a latest successful dependency: a declaration
that holds one has no snapshot (``has_snapshot``).

Nothing here raises on foreign input: a file that does not parse and an entry
that cannot be read are values (``UnparsableFile``, ``InvalidEntry``) that the
caller decides about. Beyond what the specification's reading rule says, so
that a template of another repository costs no more than its size: a template
larger than ``FILE_LIMIT`` or holding a YAML anchor or alias does not parse,
and a project id that is not a pipeline slug (``is_slug``) is an invalid entry.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Union

import yaml

from ..forms import is_slug
from .configuration import (
    BUILD_ID,
    GIT_REF,
    PLUGIN,
    PROJECT_ID,
    PROMOTE,
    SNAPSHOT_OPTION,
    UPLOAD,
    EntryError,
    download_entries,
    holds_plugin_name,
    misnamed_plugins,
    option_items,
    option_value,
    plugin_block,
    plugin_configurations,
)
from .shown import shown_text, shown_value

BUILDKITE_DIR = ".buildkite/"
TOOLS_DIR = ".buildkite/tools/"
TEMPLATE_SUFFIXES = (".yml", ".yaml")

# The largest file of another repository the reader parses, in bytes. Parsing
# takes many times the size of the text (YAML about 160, Python about 440);
# the largest committed template is under 4 KB, the largest pipeline.py 22 KB.
FILE_LIMIT = 1024 * 1024

# The two types of dependency.
SNAPSHOT = "snapshot"
LATEST_SUCCESSFUL = "latest successful"


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One dependency: the project a template downloads from, its type and its pin.

    ``type`` is ``SNAPSHOT``, or ``LATEST_SUCCESSFUL`` for an entry that names
    its build (``names_build``). ``pin`` is a snapshot entry's git ref when that
    is a literal string; None when it has no git ref or one that is not
    literal, and always for a latest successful entry. ``path`` is the first
    template the entry was found in, for messages; it is not part of what
    makes two entries one.
    """

    project: str
    type: str = SNAPSHOT
    pin: str | None = None
    path: str = field(default="", compare=False)


@dataclass(frozen=True)
class Declaration:
    """The entries of one commit's templates, each once, sorted."""

    entries: tuple[Entry, ...] = ()


@dataclass(frozen=True)
class UnparsableFile:
    """A template that is not UTF-8 or not one YAML document."""

    path: str
    detail: str


@dataclass(frozen=True)
class InvalidEntry:
    """A part of a template that names the plugin and that the reading rule cannot read.

    A download entry whose project is not a literal pipeline slug, an option
    given twice with different values, or a key that names the plugin another
    way (``misnamed_plugins``).
    """

    path: str
    detail: str


ReadError = Union[UnparsableFile, InvalidEntry]


@dataclass(frozen=True)
class Reading:
    """What ``read_declaration`` found: the declaration and the errors on the way."""

    declaration: Declaration
    errors: tuple[ReadError, ...] = ()


@dataclass(frozen=True)
class StepDownloads:
    """The download entries of one step of a pipeline, and how messages name the step.

    ``problems`` say where the step's plugin configurations cannot be read
    (``step_downloads``).
    """

    step: str
    entries: tuple[dict[str, Any], ...]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True)
class Violation:
    """A part of a generated pipeline that the declaration, or the reading of it, does not allow.

    ``project`` is the project the entry downloads from; None for a problem of
    the plugin's configuration, which concerns no one project.
    """

    step: str
    project: str | None
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


def has_snapshot(d: Declaration) -> bool:
    """False when ``d`` holds a latest successful dependency: a snapshot never contains one."""
    return not declared_entries(d, LATEST_SUCCESSFUL)


def violation_message(v: Violation) -> str:
    """How a violation is shown in the job log and the annotation."""
    return f"step {shown_text(v.step)}: {v.reason}"


# ---------------------------------------------------------------------------
# One download entry
# ---------------------------------------------------------------------------


def other_project(entry: Mapping[str, Any], own: str) -> str | None:
    """The project of another pipeline that ``entry`` downloads from; None for ``own``.

    An entry without a project id, or with a null or empty one, downloads from
    the pipeline's own project, as the artifacts plugin reads it. Raises
    ``EntryError`` for a project id that is not a string or not a slug (so
    also for one with a ``{placeholder}`` or a ``$``).
    """
    v = option_value(entry, PROJECT_ID)
    if v is None or v == "" or v == own:
        return None
    if not isinstance(v, str):
        raise EntryError(f"project id {shown_value(v)} is not a string")
    if not is_slug(v):
        raise EntryError(
            f"project id {shown_value(v)} is not a pipeline slug (lowercase letters, digits and -)"
        )
    return v


def names_build(entry: Mapping[str, Any]) -> bool:
    """True for an entry with a key the plugin reads as ``build_id``, whatever its value.

    Such an entry names the build it downloads: it is a latest successful
    dependency, not a snapshot dependency. It is not planned, not triggered
    and not part of a key, and the plan step leaves it as generated.
    ``build_id: LATEST_SUCCESSFUL`` is how a pipeline asks, for one download
    and for good, for the newest good build.
    """
    return bool(option_items(entry, BUILD_ID))


def entry_type(entry: Mapping[str, Any]) -> str:
    return LATEST_SUCCESSFUL if names_build(entry) else SNAPSHOT


def is_literal(text: str) -> bool:
    """True for text that no substitution changes: it has no ``{`` and no ``$``."""
    return "{" not in text and "$" not in text


def literal_pin(value: Any) -> str | None:
    """A git ref as a pin: a non-empty literal string; None for anything else."""
    return value if isinstance(value, str) and value and is_literal(value) else None


# ---------------------------------------------------------------------------
# The steps of a generated pipeline
# ---------------------------------------------------------------------------

# The fields that give a step its key (``id`` and ``identifier`` are Buildkite's
# older names of it), and those that give it its label.
STEP_KEY_FIELDS = ("key", "id", "identifier")
STEP_LABEL_FIELDS = ("label", "name")


def pipeline_steps(doc: Any) -> list[Any]:
    """The top-level steps of a pipeline document: its ``steps``, or the document as a list."""
    steps = doc.get("steps") if isinstance(doc, dict) else doc
    return steps if isinstance(steps, list) else []


def is_group(step: Any) -> bool:
    return isinstance(step, dict) and "group" in step and isinstance(step.get("steps"), list)


def leaf_steps(steps: Sequence[Any]) -> Iterator[Any]:
    """Every step that is not a group, in order, the steps of a group in its place."""
    for s in steps:
        if is_group(s):
            yield from leaf_steps(s["steps"])
        else:
            yield s


def _first_text(step: Any, fields: Sequence[str]) -> str | None:
    if isinstance(step, dict):
        for name in fields:
            if isinstance(step.get(name), str) and step[name]:
                return step[name]
    return None


def step_key(step: Any) -> str | None:
    """The key of a step, under any of ``STEP_KEY_FIELDS``; None when it has none."""
    return _first_text(step, STEP_KEY_FIELDS)


def step_label(step: Any) -> str | None:
    """The label of a step, under any of ``STEP_LABEL_FIELDS``; None when it has none."""
    return _first_text(step, STEP_LABEL_FIELDS)


def step_name(step: Any, position: int) -> str:
    """How messages name a step: its key, else its label, else ``step <position>``."""
    return step_key(step) or step_label(step) or f"step {position}"


def plan_step_options(step: Any) -> list[str]:
    """The ``snapshot`` options of the upload and promote blocks of ``step``, of any project.

    Only the plan step writes one, with the build's key: a promote writes a
    pointer at the key it is given, so a key the generator wrote would put a
    pointer where no plan placed it. Raises ``EntryError`` when a block is
    given twice with different values.
    """
    return [
        f"{block}.{shown_text(str(k))} is given in the generated pipeline "
        f"({shown_value(v)}): only the plan step writes it, with the build's key"
        for config in plugin_configurations(step)
        for block in (UPLOAD, PROMOTE)
        for k, v in option_items(plugin_block(config, block) or {}, SNAPSHOT_OPTION)
    ]


def step_downloads(step: Any, name: str) -> StepDownloads:
    """The download entries of ``step``, which messages name ``name``, and its plugin problems.

    The problems are a key that names the plugin another way
    (``misnamed_plugins``), the options only the plan step writes
    (``plan_step_options``), and a block given twice with different values,
    which leaves the step without entries.
    """
    misnamed = misnamed_plugins(step)
    try:
        entries, problems = download_entries(step), plan_step_options(step)
    except EntryError as exc:
        entries, problems = [], [str(exc)]
    return StepDownloads(name, tuple(entries), tuple(misnamed + problems))


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
    ``*.yml`` or ``*.yaml`` file under ``.buildkite/``, outside the tools
    submodule. The files are those of a commit, never of a working tree, so
    the pipeline a job generates is not among them unless it is committed.
    """
    return (
        path.startswith(BUILDKITE_DIR)
        and path.endswith(TEMPLATE_SUFFIXES)
        and not path.startswith(TOOLS_DIR)
    )


def mentions_plugin(content: str | bytes) -> bool:
    """True when the file's text contains the plugin's name in any ASCII case.

    A file that does not is never parsed. Any case, so that a key naming the
    plugin another way (``configuration.names_plugin_another_way``) is found
    and refused.
    """
    if isinstance(content, bytes):
        return PLUGIN.encode("ascii") in content.lower()
    return holds_plugin_name(content)


class AnchorRefused(yaml.MarkedYAMLError):
    """A template holds an anchor or an alias, which the reading rule does not read."""


class TemplateLoader(yaml.SafeLoader):
    """The safe loader, refusing every anchor and alias.

    PyYAML keeps an alias as a shared reference, so a few hundred bytes of
    aliases stand for a tree of billions of nodes that any walk, comparison
    or message of the value goes through in full. No committed template uses
    one.
    """

    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, yaml.AliasEvent) or getattr(event, "anchor", None) is not None:
            raise AnchorRefused(problem="anchor or alias", problem_mark=event.start_mark)
        return super().compose_node(parent, index)


def _mark(exc: yaml.MarkedYAMLError) -> str:
    mark = exc.problem_mark
    return f"line {mark.line + 1}, column {mark.column + 1}"


def yaml_problem(exc: Exception) -> str:
    """The parser's explanation, shown short in ASCII: it may quote the file."""
    problem = getattr(exc, "problem", None)
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None and problem:
        return f"{shown_text(problem)} at {_mark(exc)}"
    return shown_text(" ".join(str(exc).split())) or type(exc).__name__


def size_problem(content: str | bytes) -> str | None:
    """Why ``content`` is too large for the reader to parse; None when it is not."""
    size = len(content) if isinstance(content, bytes) else len(content.encode("utf-8"))
    if size <= FILE_LIMIT:
        return None
    return f"is {size} bytes, more than the {FILE_LIMIT} bytes the reader parses"


def parse_template(path: str, content: str | bytes) -> Any | UnparsableFile:
    """The parsed YAML of one template, or why it cannot be parsed.

    A template larger than ``FILE_LIMIT`` does not parse, decided before it
    is decoded. Bytes are decoded as UTF-8. The text is parsed as it is, with
    a safe loader that refuses anchors and aliases (``TemplateLoader``):
    ``{placeholder}`` text is left alone.
    """
    too_large = size_problem(content)
    if too_large is not None:
        return UnparsableFile(path, too_large)
    try:
        text = content.decode("utf-8") if isinstance(content, bytes) else content
    except UnicodeDecodeError as exc:
        return UnparsableFile(path, f"not UTF-8: {exc.reason} at byte {exc.start}")
    try:
        return yaml.load(text, Loader=TemplateLoader)
    except AnchorRefused as exc:
        return UnparsableFile(path, f"holds an anchor or alias at {_mark(exc)}")
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        return UnparsableFile(path, f"not valid YAML: {yaml_problem(exc)}")


def read_entry(path: str, entry: Mapping[str, Any], own: str) -> Entry | InvalidEntry | None:
    """The dependency one download entry of a template declares; None for the own project.

    An entry that names its build is a latest successful dependency, without
    a pin.
    """
    try:
        project = other_project(entry, own)
        if project is None:
            return None
        if names_build(entry):
            return Entry(project, LATEST_SUCCESSFUL, None, path)
        return Entry(project, SNAPSHOT, literal_pin(option_value(entry, GIT_REF)), path)
    except EntryError as exc:
        return InvalidEntry(path, str(exc))


def read_template(path: str, content: str | bytes, own: str) -> list[Entry | ReadError]:
    """The entries and errors of one template that mentions the plugin.

    A ``download`` or ``projects`` given twice with different values leaves
    the template without entries.
    """
    doc = parse_template(path, content)
    if isinstance(doc, UnparsableFile):
        return [doc]
    misnamed = [InvalidEntry(path, p) for p in misnamed_plugins(doc)]
    try:
        found = [read_entry(path, e, own) for e in download_entries(doc)]
    except EntryError as exc:
        found = [InvalidEntry(path, str(exc))]
    return [*misnamed, *(x for x in found if x is not None)]


def _entry_order(e: Entry) -> tuple[str, str, str]:
    return e.project, e.type, e.pin or ""


def read_declaration(files: Mapping[str, str | bytes], own: str) -> Reading:
    """The declaration of pipeline ``own`` in ``files``, and the errors found reading it.

    ``files`` maps paths relative to the repository root to their content,
    for the files of one commit's tree under ``.buildkite/``; other paths are
    ignored. A file counts when ``is_template_path`` holds and its text
    contains ``PLUGIN`` (``mentions_plugin``). In every such file each download entry
    (``download_entries``) with a project id gives one entry ``(project,
    type, pin)``: a latest successful one when it names its build
    (``names_build``), else a snapshot one. Entries of ``own`` and entries
    without a project id are dropped. Files are read in path order; entries
    are kept once each, sorted, with the first file each was found in, and
    errors once each, in the order found.
    """
    results = [
        x
        for path in sorted(files)
        if is_template_path(path) and mentions_plugin(files[path])
        for x in read_template(path, files[path], own)
    ]
    entries = sorted({x for x in results if isinstance(x, Entry)}, key=_entry_order)
    errors = dict.fromkeys(x for x in results if not isinstance(x, Entry))
    return Reading(Declaration(tuple(entries)), tuple(errors))


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def _shown_ref(value: Any) -> str:
    return "no git ref" if value is None else shown_value(value)


def _raw_project(entry: Mapping[str, Any]) -> str:
    return " / ".join(shown_value(v) for _, v in option_items(entry, PROJECT_ID))


def _ref_problem(project: str, ref: Any, pins: frozenset[str], own_ref: str) -> str | None:
    if isinstance(ref, str) and (ref == own_ref or ref in pins):
        return None
    declared = f" (declared: {', '.join(shown_text(p) for p in sorted(pins))})" if pins else ""
    return (
        f"downloads {project} at {_shown_ref(ref)}, which is neither the build's own ref "
        f"{shown_text(own_ref)} nor a pin declared for {project}{declared}"
    )


def entry_problem(
    entry: Mapping[str, Any], declaration: Declaration, own: str, own_ref: str
) -> tuple[str, str] | None:
    """``(project, reason)`` when one download entry breaks the rule; None when it does not.

    The entry's type must be the type a template declares the project with.
    A latest successful entry is left as generated, so its ref is not looked at.
    """
    try:
        project = other_project(entry, own)
    except EntryError as exc:
        return _raw_project(entry), str(exc)
    if project is None:
        return None
    t = entry_type(entry)
    if project not in declared_projects(declaration, t):
        why = f"which no committed template declares as a {t} dependency"
        return project, f"downloads from {project} as a {t} dependency, {why}"
    if t == LATEST_SUCCESSFUL:
        return None
    try:
        ref = option_value(entry, GIT_REF)
    except EntryError as exc:
        return project, f"downloads from {project}, but its {exc}"
    problem = _ref_problem(project, ref, declared_pins(declaration, project), own_ref)
    return None if problem is None else (project, problem)


def rule_violations(
    downloads: Sequence[StepDownloads], declaration: Declaration, own: str, own_ref: str
) -> list[Violation]:
    """The parts of a generated pipeline that the declaration does not allow.

    ``downloads`` comes from ``pipeline_downloads``; ``own`` is the pipeline's
    own project and ``own_ref`` the build's own git ref, in the form the
    generator writes it (``refs/heads/<branch>``). A step's plugin problems
    are violations. Every entry of another project must have its project
    declared in ``declaration`` with the same type; a snapshot entry also a
    git ref that is ``own_ref`` or a pin declared for that project. The
    declaration may hold more than the pipeline uses. Each violation is
    reported once, in order.
    """

    def of_step(sd: StepDownloads) -> list[Violation]:
        problems = [Violation(sd.step, None, p) for p in sd.problems]
        found = (entry_problem(e, declaration, own, own_ref) for e in sd.entries)
        return problems + [Violation(sd.step, *f) for f in found if f is not None]

    return list(dict.fromkeys(v for sd in downloads for v in of_step(sd)))
