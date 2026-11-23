"""The declaration of a pipeline: what the step templates of one commit say it depends on,
and the step ids they promote. Read from the files of a commit, without running anything.

A template is a ``*.yml`` or ``*.yaml`` file under ``.buildkite/`` whose text names the
plugin. Every download entry in it with the project id of another pipeline is a dependency:
a latest successful one when it writes ``latest: true``, else a snapshot dependency, whose
``git_ref`` is a pin when it is literal text. A step id counts when a step promotes it and
some step writes it as ``step_id``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import dacite
import yaml

from ..layout import is_step_id
from ..options import PLUGIN, STEP_ID_RULE, read
from .pipeline import configurations, leaf_steps, other_project

BUILDKITE_DIR = ".buildkite/"
TEMPLATE_SUFFIXES = (".yml", ".yaml")


class TemplateError(ValueError):
    """A template that cannot be read, naming the project and the file."""


# ---------------------------------------------------------------------------
# The options a declaration reads
# ---------------------------------------------------------------------------
# A template is not read as ``Options``: its generator has yet to write what it fills, each
# block's ``variant`` among it.


@dataclass(frozen=True)
class TemplateEntry:
    project_id: str | None = None
    git_ref: str | None = None
    latest: bool = False


@dataclass(frozen=True)
class TemplateDownload:
    projects: tuple[TemplateEntry, ...] = ()


@dataclass(frozen=True)
class TemplatePromote:
    step: str | None = None


@dataclass(frozen=True)
class TemplateOptions:
    download: TemplateDownload | None = None
    promote: TemplatePromote | None = None


def entries_of(o: TemplateOptions) -> tuple[TemplateEntry, ...]:
    return o.download.projects if o.download else ()


@dataclass(frozen=True)
class Dependency:
    """``pin`` is a snapshot dependency's literal git ref, else None. ``path`` is the first
    template it was found in, for messages; it is not part of what makes two of them one."""

    project: str
    latest: bool = False
    pin: str | None = None
    path: str = field(default="", compare=False)


def kind(latest: bool) -> str:
    """How messages name the two kinds of dependency."""
    return "latest successful" if latest else "snapshot"


@dataclass(frozen=True)
class Declaration:
    """Each dependency once, sorted, and the step ids the templates promote, sorted."""

    entries: tuple[Dependency, ...] = ()
    steps: tuple[str, ...] = ()


def snapshot_dependencies(d: Declaration) -> dict[str, Dependency]:
    """The first entry of each snapshot dependency, by project, sorted."""
    found: dict[str, Dependency] = {}
    for e in d.entries:
        if not e.latest:
            found.setdefault(e.project, e)
    return found


def latest_dependencies(d: Declaration) -> tuple[Dependency, ...]:
    return tuple(e for e in d.entries if e.latest)


def declared_pins(d: Declaration, project: str) -> frozenset[str]:
    return frozenset(e.pin for e in d.entries if e.project == project and e.pin is not None)


def declared_path(d: Declaration, project: str, latest: bool) -> str:
    """The template that declares ``project`` as that kind of dependency, the first found."""
    return next(e.path for e in d.entries if e.project == project and e.latest == latest)


def is_template_path(path: str) -> bool:
    return path.startswith(BUILDKITE_DIR) and path.endswith(TEMPLATE_SUFFIXES)


def literal_pin(value: Any) -> str | None:
    """A git ref as a pin: non-empty text that no substitution changes; None for the rest."""
    literal = isinstance(value, str) and value and "{" not in value and "$" not in value
    return value if literal else None


def template_steps(doc: Any) -> list[dict[str, Any]]:
    """The steps of a template: the mapping it holds, and each of its ``steps``."""
    if not isinstance(doc, dict):
        return []
    inner = doc.get("steps")
    listed = leaf_steps(inner) if isinstance(inner, list) else []
    return [doc, *(s for s in listed if isinstance(s, dict))]


def step_id_of(own: str, path: str, step: Mapping[str, Any]) -> str | None:
    value = step.get("step_id")
    if value is None or is_step_id(value):
        return value
    raise TemplateError(f"{own}: {path}: step_id {value} is not a step id. {STEP_ID_RULE}")


def promoted_ids(options: list[TemplateOptions], step_id: str | None) -> list[str]:
    """The step ids a template step promotes: of each promote block its own ``step``, else the
    step's ``step_id``."""
    found = (o.promote.step or step_id for o in options if o.promote)
    return [i for i in found if i]


def options_in(value: Any) -> list[TemplateOptions]:
    return [read(TemplateOptions, c) for c in configurations(value)]


def dependency(e: TemplateEntry, own: str, path: str) -> Dependency | None:
    project = other_project(e.project_id, own)
    if project is None:
        return None
    return Dependency(project, e.latest, None if e.latest else literal_pin(e.git_ref), path)


@dataclass(frozen=True)
class Template:
    """What one template says: its dependencies, the step ids its steps promote, and those
    they write as ``step_id``."""

    dependencies: tuple[Dependency, ...]
    promoted: frozenset[str]
    written: frozenset[str]


def template_of(doc: Any, own: str, path: str) -> Template:
    steps = [(step_id_of(own, path, s), options_in(s)) for s in template_steps(doc)]
    found = (dependency(e, own, path) for o in options_in(doc) for e in entries_of(o))
    return Template(
        tuple(d for d in found if d is not None),
        frozenset(p for i, options in steps for p in promoted_ids(options, i)),
        frozenset(i for i, _ in steps if i),
    )


def read_template(path: str, content: str | bytes, own: str) -> Template:
    try:
        return template_of(yaml.safe_load(content), own, path)
    except (yaml.YAMLError, dacite.DaciteError) as exc:
        raise TemplateError(f"{own}: {path} cannot be read: {exc}") from exc


def names_plugin(content: str | bytes) -> bool:
    """Whether a file's text names the plugin, in any case."""
    text = content.encode("utf-8") if isinstance(content, str) else content
    return PLUGIN.encode("ascii") in text.lower()


def read_declaration(files: Mapping[str, str | bytes], own: str) -> Declaration:
    """The declaration of pipeline ``own`` in ``files``, the files of one commit by path.
    Raises ``TemplateError`` for a template that cannot be read."""
    read = [
        read_template(path, content, own)
        for path, content in sorted(files.items())
        if is_template_path(path) and names_plugin(content)
    ]
    entries = dict.fromkeys(e for t in read for e in t.dependencies)
    promoted = frozenset().union(*(t.promoted for t in read))
    written = frozenset().union(*(t.written for t in read))
    ordered = sorted(entries, key=lambda e: (e.project, not e.latest, e.pin or ""))
    return Declaration(tuple(ordered), tuple(sorted(promoted & written)))
