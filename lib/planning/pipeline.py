"""A pipeline document as the plan step reads it: its steps, and the plugin's options in each.

A configuration of the plugin is the value under a key that names it, wherever that key sits
in a document; a download entry is an entry of its ``download.projects``. What a step makes is
a folder of the build's key: the platform of its upload or promote block, and a step id.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import dacite

from ..layout import Folder
from ..options import (
    PLUGIN,
    Entry,
    Options,
    options_of,
    promoted_folder,
    promoted_step,
    uploaded_folder,
)


class DocumentError(ValueError):
    """A generated pipeline the plan step cannot plan, with the reason for its author."""


def is_plugin_key(k: Any) -> bool:
    """True for a mapping key that names the artifacts plugin, in any spelling the agent runs
    it for: ``bureau14/qdb-artifacts#<ref>``, its repository's URL, any case."""
    return isinstance(k, str) and PLUGIN in k.split("#", 1)[0].lower()


def configurations(value: Any) -> Iterator[Any]:
    """The value under every key naming the plugin in ``value``, at any depth, in order."""
    if isinstance(value, dict):
        yield from (v for k, v in value.items() if is_plugin_key(k))
        for v in value.values():
            yield from configurations(v)
    elif isinstance(value, list):
        for v in value:
            yield from configurations(v)


def is_group(step: Any) -> bool:
    return isinstance(step, dict) and "group" in step and isinstance(step.get("steps"), list)


def leaf_steps(steps: Sequence[Any]) -> Iterator[Any]:
    """Every step that is not a group, in order, the steps of a group in its place."""
    for s in steps:
        if is_group(s):
            yield from leaf_steps(s["steps"])
        else:
            yield s


@dataclass(frozen=True)
class Step:
    """One step of a pipeline that is not a group: its position among them, from 1, the step
    as the document holds it, and the options of each of its configurations of the plugin."""

    position: int
    raw: Any
    options: tuple[Options, ...]

    @property
    def key(self) -> str | None:
        return self.raw.get("key") if isinstance(self.raw, dict) else None

    @property
    def label(self) -> str | None:
        return self.raw.get("label") if isinstance(self.raw, dict) else None

    @property
    def name(self) -> str:
        """How messages name the step."""
        return self.key or self.label or f"step {self.position}"


def read_step(position: int, raw: Any) -> Step:
    step = Step(position, raw, ())
    try:
        return Step(position, raw, tuple(options_of(c) for c in configurations(raw)))
    except dacite.DaciteError as exc:
        raise DocumentError(f"step {step.name}: the options of {PLUGIN}: {exc}") from exc


def steps_of(doc: Any) -> list[Step]:
    """The steps of a generated pipeline, groups descended."""
    return [read_step(i, s) for i, s in enumerate(leaf_steps(doc["steps"]), start=1)]


# ---------------------------------------------------------------------------
# Download entries
# ---------------------------------------------------------------------------


def other_project(project_id: str | None, own: str) -> str | None:
    """The project of another pipeline that an entry with ``project_id`` downloads from; None
    for ``own``, which an entry without a project id downloads from."""
    return None if not project_id or project_id == own else project_id


def entries_of(o: Options) -> tuple[Entry, ...]:
    return o.download.projects if o.download else ()


def dependencies(steps: Sequence[Step], own: str, latest: bool) -> list[tuple[Step, str, Entry]]:
    """``(step, project, entry)`` of every entry of another project that takes the latest
    successful build (``latest``) or is a snapshot dependency (not ``latest``), in order."""
    found = (
        (s, other_project(e.project_id, own), e)
        for s in steps
        for o in s.options
        for e in entries_of(o)
    )
    return [(s, project, e) for s, project, e in found if project and e.latest == latest]


def left_out(steps: Sequence[Step], own: str, own_ref: str) -> list[tuple[str, str, str]]:
    """Every download of the pipeline's own project at a ref that is not the build's own, as
    step, platform and ref, each once: the key leaves it out, and its job reads that ref's
    latest build when it runs."""
    found = (
        (s.name, e.variant, e.git_ref)
        for s in steps
        for o in s.options
        for e in entries_of(o)
        if other_project(e.project_id, own) is None and e.git_ref not in (None, "", own_ref)
    )
    return list(dict.fromkeys(found))


def taken_folders(e: Entry, steps: Sequence[str]) -> frozenset[Folder]:
    """The folders a snapshot entry takes: its platform and each step it names, or each of
    ``steps``, the dependency's step ids, when it names none."""
    return frozenset(Folder(e.variant, s) for s in e.step or steps)


# ---------------------------------------------------------------------------
# What a step makes
# ---------------------------------------------------------------------------


def uploaded(step: Step) -> list[Folder]:
    """The folders the upload blocks of ``step`` name, those with a step id."""
    return [uploaded_folder(o) for o in step.options if o.upload and o.step]


def promoted(step: Step) -> list[Folder]:
    """The folders the promote blocks of ``step`` name, those with a step id."""
    return [promoted_folder(o) for o in step.options if o.promote and promoted_step(o)]


def made_folders(step: Step) -> frozenset[Folder]:
    """What ``step`` makes: every folder it uploads or promotes."""
    return frozenset(uploaded(step) + promoted(step))


def promoted_twice(f: Folder, first: Step, second: Step) -> DocumentError:
    if first.raw is second.raw:
        who = f"the step {first.name} at two places, steps {first.position} and {second.position}"
    else:
        who = f"two steps, {first.name} and {second.name}"
    return DocumentError(
        f"{f} is promoted by {who}. Each step id of a platform is promoted by one step only: "
        "give each its own step_id, or promote it from one of them."
    )


def promotions(steps: Sequence[Step]) -> dict[Folder, Step]:
    """The step that promotes each folder the pipeline makes. A folder promoted at two places
    is an error: the first to pass would promote it while the other still uploads."""
    found: dict[Folder, Step] = {}
    for step in steps:
        for f in promoted(step):
            first = found.setdefault(f, step)
            if first is not step:
                raise promoted_twice(f, first, step)
    return found
