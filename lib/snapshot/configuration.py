"""The plugin's configuration as written in a document (specification section 2).

A step template or a generated pipeline writes the artifacts plugin's
configuration under a key that names the plugin. The reader of templates, the
rule over a generated pipeline and the rewrite of it all read that
configuration here, so they have one notion of an option, a block and a
download entry.

A configuration is read by its structure: the blocks ``download``, ``upload``
and ``promote``, the entries of ``download.projects`` (a list of mappings), and
their options. The agent names a variable by the whole path to a value, so a
configuration could reach the plugin's variables by another path; those are
not read, as no template writes one. A key is read as the agent reads it, in
any case and with ``-`` as ``_`` (``option_name``), so ``project_id``,
``project-id`` and ``Project-Id`` are one option; of one option written two
ways, the first is read. A key of a generated pipeline that names the plugin
another way than ``PLUGIN`` breaks the rule: the agent runs the plugin for it,
and the reader would not see its downloads (``misnamed_plugins``).

A download entry is a mapping in the ``download.projects`` list of the value
under a key that names the plugin (``download_entries``), wherever that key
sits in a document.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..options import read_bool

PLUGIN = "bureau14/qdb-artifacts"

# The plugin's blocks and the options the planning half reads and writes.
DOWNLOAD = "download"
UPLOAD = "upload"
PROMOTE = "promote"
PROJECTS = "projects"
PROJECT_ID = "project_id"
GIT_REF = "git_ref"
VARIANT = "variant"
STRICT_REF_MATCH = "require_strict_ref_match"
LATEST = "latest"
STEP = "step"
KEY = "key"
KEYS = "keys"


# ---------------------------------------------------------------------------
# Option keys, as the agent reads them
# ---------------------------------------------------------------------------


def option_name(key: Any) -> str | None:
    """The option a key names as the agent reads it: in lower case, ``-`` as ``_``; None for a
    key that is not text."""
    return key.lower().replace("-", "_") if isinstance(key, str) else None


def reads_as(key: Any, option: str) -> bool:
    """True when the plugin reads ``key`` as ``option``."""
    return option_name(key) == option


def option_items(options: Mapping[Any, Any], option: str) -> list[tuple[Any, Any]]:
    """The keys and values of ``options`` that the plugin reads as ``option``, in order."""
    return [(k, v) for k, v in options.items() if reads_as(k, option)]


def option_value(options: Mapping[Any, Any], option: str) -> Any:
    """The value of ``option`` in ``options`` under the first key the plugin reads as it; None
    if absent."""
    return next((v for k, v in options.items() if reads_as(k, option)), None)


# ---------------------------------------------------------------------------
# The plugin's name
# ---------------------------------------------------------------------------


def is_plugin_key(k: Any) -> bool:
    """True for a mapping key naming the artifacts plugin: its part before ``#`` is ``PLUGIN``."""
    return isinstance(k, str) and k.split("#", 1)[0] == PLUGIN


def names_plugin_another_way(k: Any) -> bool:
    """True for a key whose part before ``#`` holds ``PLUGIN`` in any case and is not it.

    The agent runs a plugin for such a key (GitHub's names have no case),
    while the reader reads only ``is_plugin_key``.
    """
    if not isinstance(k, str):
        return False
    name = k.split("#", 1)[0]
    return name != PLUGIN and PLUGIN in name.lower()


# ---------------------------------------------------------------------------
# Walks over a document
# ---------------------------------------------------------------------------


def mappings(value: Any) -> list[dict[Any, Any]]:
    """Every mapping in ``value``, at any depth, in document order."""
    found: list[dict[Any, Any]] = []
    pending = [value]
    while pending:
        v = pending.pop()
        if isinstance(v, dict):
            found.append(v)
            pending.extend(reversed(list(v.values())))
        elif isinstance(v, list):
            pending.extend(reversed(v))
    return found


def mapping_items(value: Any) -> list[tuple[Any, Any]]:
    """Every key and value of every mapping in ``value`` (``mappings``), in document order."""
    return [item for m in mappings(value) for item in m.items()]


def plugin_configurations(value: Any) -> list[Any]:
    """The value under every key naming the artifacts plugin in ``value``, in order."""
    return [v for k, v in mapping_items(value) if is_plugin_key(k)]


# ---------------------------------------------------------------------------
# Blocks and download entries
# ---------------------------------------------------------------------------


def plugin_block(config: Any, block: str) -> dict[str, Any] | None:
    """The mapping a plugin configuration holds as option ``block``; None when it holds none.

    ``block`` is ``download``, ``upload`` or ``promote``.
    """
    found = option_value(config, block) if isinstance(config, dict) else None
    return found if isinstance(found, dict) else None


def named_step(config: Any) -> str | None:
    """The step id an artifacts configuration names at its top (``step``), as text; None
    when it names none."""
    v = option_value(config, STEP) if isinstance(config, dict) else None
    return v if isinstance(v, str) and v else None


def entry_steps(entry: Mapping[Any, Any]) -> tuple[str, ...]:
    """The step ids a download entry names: one, a list, or none (absent, or ``[]``)."""
    v = option_value(entry, STEP)
    if isinstance(v, list):
        return tuple(str(s) for s in v)
    return () if v is None or v == "" else (str(v),)


def takes_latest(entry: Mapping[Any, Any]) -> bool:
    """True for an entry that takes the latest successful build (``latest: true``), read as
    the job reads a boolean."""
    return read_bool(option_value(entry, LATEST))


def plugin_download_entries(config: Any) -> list[dict[str, Any]]:
    """The mappings in ``download.projects`` of one artifacts plugin configuration."""
    download = plugin_block(config, DOWNLOAD)
    projects = option_value(download, PROJECTS) if download is not None else None
    if not isinstance(projects, list):
        return []
    return [e for e in projects if isinstance(e, dict)]


def download_entries(value: Any) -> list[dict[str, Any]]:
    """Every download entry in ``value``, at any depth, in document order.

    A download entry is a mapping in the ``download.projects`` list of the
    value under a key that names the artifacts plugin (``is_plugin_key``),
    wherever that key sits.
    """
    return [e for config in plugin_configurations(value) for e in plugin_download_entries(config)]


# ---------------------------------------------------------------------------
# The plugin named another way
# ---------------------------------------------------------------------------


def misnamed_plugins(value: Any) -> list[str]:
    """Why each key in ``value`` that names the plugin another way is an error."""
    return [
        f"the plugin is named {k!r}: the agent runs it for that key, and the "
        f"reading rule reads only {PLUGIN} or {PLUGIN}#<ref>"
        for k, _ in mapping_items(value)
        if names_plugin_another_way(k)
    ]
