"""The plugin's options, as a step's configuration writes them.

The agent hands a job its configuration as JSON (BUILDKITE_PLUGIN_CONFIGURATION). The plan
step finds the same mappings in a generated pipeline. Both read them with ``options_of``, so
a job and the plan step have one notion of every option. A step template holds them before
its generator wrote the options it fills, so the plan step reads a template with ``read``,
into a shape of the options it needs there (``snapshot/templates.py``).

A key is read as the agent names its variable: in lower case, with ``-`` and white space as
``_``. An option the shape does not name is ignored. Values are not checked for their type: a
template holds placeholders where a pipeline holds numbers.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeVar

import dacite

from .layout import Folder, is_step_id

T = TypeVar("T")
PLUGIN = "bureau14/qdb-artifacts"
STEP_ID_RULE = 'A step id is lower-case letters, digits and "_", and starts with a letter.'
# Files that travel at once, and multipart threads of each: up to 128 streams.
PARALLEL, CONCURRENCY = 4, 32


@dataclass(frozen=True)
class Entry:
    """One download entry. ``step`` holds the step ids it names, none for every step; ``keys``
    the key of each step it takes, written by the plan step."""

    variant: str
    project_id: str | None = None
    git_ref: str | None = None
    require_strict_ref_match: bool = False
    latest: bool = False
    step: tuple[str, ...] = ()
    keys: Mapping[str, str] = field(default_factory=dict)
    output_dir: str = "."
    extract: bool = False
    files: tuple[str, ...] = ("*.tar.zst",)
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Download:
    projects: tuple[Entry, ...] = ()
    clean: bool = False
    parallel: int = PARALLEL
    concurrency: int = CONCURRENCY


@dataclass(frozen=True)
class Upload:
    variant: str
    files: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    base_dir: str | None = None
    parallel: int = PARALLEL
    concurrency: int = CONCURRENCY
    annotate: bool = True
    allow_empty: bool = False


@dataclass(frozen=True)
class Promote:
    variant: str
    step: str | None = None


@dataclass(frozen=True)
class Options:
    """``step`` is the step id of a step that uploads or promotes; ``key`` the build's key,
    written by the plan step. ``plan``, ``resume`` and ``leave`` mark the plan step, its second
    run and the last step; the first two name the generated pipeline."""

    step: str | None = None
    key: str | None = None
    download: Download | None = None
    upload: Upload | None = None
    promote: Promote | None = None
    plan: str | None = None
    resume: str | None = None
    leave: bool = False


def read_bool(v: Any) -> bool:
    return str(v).strip().lower() in ("true", "1", "on", "yes")


def listed(v: Any) -> tuple[str, ...]:
    """One value or a list of them, as a tuple of text."""
    return tuple(str(x) for x in v) if isinstance(v, list) else (str(v),)


def as_agent_names(v: Any) -> Any:
    """``v`` with every mapping key as the agent names its variable."""
    if isinstance(v, dict):
        return {re.sub(r"[-\s]", "_", str(k).lower()): as_agent_names(x) for k, x in v.items()}
    return [as_agent_names(x) for x in v] if isinstance(v, list) else v


READ = dacite.Config(
    type_hooks={bool: read_bool, tuple[str, ...]: listed}, cast=[tuple], check_types=False
)


def read(shape: type[T], config: Mapping[str, Any] | None) -> T:
    """What one configuration of the plugin writes of the options ``shape`` names. Raises
    ``dacite.DaciteError`` when it lacks one that ``shape`` gives no default."""
    return dacite.from_dict(shape, as_agent_names(config or {}), READ)


def options_of(config: Mapping[str, Any] | None) -> Options:
    """The options of a configuration that a job runs. Raises ``dacite.DaciteError`` when a
    block lacks its ``variant``."""
    return read(Options, config)


def job_options(env: Mapping[str, str]) -> Options:
    return options_of(json.loads(env.get("BUILDKITE_PLUGIN_CONFIGURATION") or "{}"))


# ---------------------------------------------------------------------------
# The folder a step uploads or promotes
# ---------------------------------------------------------------------------


def promoted_step(o: Options) -> str | None:
    """The step id a step promotes: its promote block's ``step``, which names what another
    step uploaded, else the top-level ``step``."""
    return o.promote.step or o.step


def step_id_problem(value: str | None, platform: str, act: str) -> str | None:
    """Why ``value`` cannot be the step id of a step that ``act`` (uploads or promotes)
    ``platform``, as the rest of a sentence that begins with the step; None when it can."""
    if not value:
        return (
            f"{act} {platform} and names no step id. Its template writes step_id in place of key, "
            "with the .buildkite/tools of that change (merge master); a pipeline without "
            "pipeline.py writes step: <id> beside upload."
        )
    if not is_step_id(value):
        return f"{act} {platform} as {value}, which is not a step id. {STEP_ID_RULE}"
    return None


def step_id_problems(o: Options) -> list[str]:
    """Why the upload and promote blocks of ``o`` name no folder; empty when each does."""
    found = [
        step_id_problem(o.step, o.upload.variant, "uploads") if o.upload else None,
        step_id_problem(promoted_step(o), o.promote.variant, "promotes") if o.promote else None,
    ]
    return [p for p in found if p is not None]


def uploaded_folder(o: Options) -> Folder:
    """A step uploads its platform under the top-level ``step``."""
    return Folder(o.upload.variant, o.step)


def promoted_folder(o: Options) -> Folder:
    return Folder(o.promote.variant, promoted_step(o))
