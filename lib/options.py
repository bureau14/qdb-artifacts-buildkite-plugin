"""The plugin's options, as a step's configuration writes them.

The agent hands a job its configuration as JSON (BUILDKITE_PLUGIN_CONFIGURATION). The plan
step finds the same mappings in a generated pipeline. Both read them with ``options_of``, so
a job and the plan step have one notion of every option. A step template holds them before
its generator wrote the options it fills, so the plan step reads a template with ``read``,
into a shape of the options it needs there (``planning/templates.py``).

A key is read as the agent names its variable: in lower case, with ``-`` and white space as
``_``. ``read`` does not check the type of a value, because a template holds placeholders where a
pipeline holds numbers; ``options_of`` checks the numbers that a job uses.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, TypeVar

import dacite

from .errors import ArtifactsError
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
    written by the plan step. ``plan``, ``plan_again`` and ``last_step`` mark the plan step,
    its second run and the last step; the first two name the generated pipeline."""

    step: str | None = None
    key: str | None = None
    download: Download | None = None
    upload: Upload | None = None
    promote: Promote | None = None
    plan: str | None = None
    plan_again: str | None = None
    last_step: bool = False


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


def count_of(block: str, name: str, v: Any) -> int:
    """The option ``block.name`` as a whole number of at least 1. Raises ``ArtifactsError``
    naming the option and its value otherwise."""
    try:
        n = v if isinstance(v, int) and not isinstance(v, bool) else int(str(v).strip())
    except ValueError:
        n = 0
    if n < 1:
        raise ArtifactsError(
            f"the option {block}.{name} must be a whole number of at least 1, got {v!r}"
        )
    return n


def with_counts(block: str, b: T | None) -> T | None:
    """``b``, a download or an upload block, with ``parallel`` and ``concurrency`` as numbers."""
    if b is None:
        return None
    return replace(
        b,
        parallel=count_of(block, "parallel", b.parallel),
        concurrency=count_of(block, "concurrency", b.concurrency),
    )


def options_of(config: Mapping[str, Any] | None) -> Options:
    """The options of a configuration that a job runs. Raises ``dacite.DaciteError`` when a
    block lacks its ``variant``, and ``ArtifactsError`` when ``parallel`` or ``concurrency``
    is not a whole number of at least 1."""
    o = read(Options, config)
    return replace(
        o, download=with_counts("download", o.download), upload=with_counts("upload", o.upload)
    )


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


def upload_problems(o: Options) -> list[str]:
    """Why the upload block of ``o`` uploads nothing; empty when it names files or is absent."""
    if o.upload and not o.upload.files:
        return ["names no files to upload: set upload.files"]
    return []


def uploaded_folder(o: Options) -> Folder:
    """A step uploads its platform under the top-level ``step``."""
    return Folder(o.upload.variant, o.step)


def promoted_folder(o: Options) -> Folder:
    return Folder(o.promote.variant, promoted_step(o))
