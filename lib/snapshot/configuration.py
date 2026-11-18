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
not read, as no template writes one. Each key is read as the agent reads one
key (``key_names``), so ``project_id``, ``project-id`` and ``Project-Id`` are
one option, and one option written two ways with different values is an error
(``option_value``). A key that names the plugin another way than ``PLUGIN`` is
an error too: the agent runs the plugin for it, and the reader would not see
its downloads (``misnamed_plugins``).

A download entry is a mapping in the ``download.projects`` list of the value
under a key that names the plugin (``download_entries``), wherever that key
sits in a document. A document parsed with aliases shares its values; every
walk here visits each shared value once.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .shown import shown_value

PLUGIN = "bureau14/qdb-artifacts"

# The plugin's blocks and the options the planning half reads.
DOWNLOAD = "download"
UPLOAD = "upload"
PROMOTE = "promote"
PROJECTS = "projects"
PROJECT_ID = "project_id"
GIT_REF = "git_ref"
BUILD_ID = "build_id"
VARIANT = "variant"
STRICT_REF_MATCH = "require_strict_ref_match"
SNAPSHOT_OPTION = "snapshot"


# ---------------------------------------------------------------------------
# Option keys, as the agent reads them
# ---------------------------------------------------------------------------

# formatEnvKey in agent/plugin/plugin.go of buildkite/agent, measured on agent
# 3.118.1: every "-" and every white space character of Go's regexp class \s
# (ASCII only, without \v) becomes "_".
_KEY_SEPARATORS = re.compile(r"[-\t\n\f\r ]")
_UNDERSCORES = re.compile(r"_+")


def agent_key(key: str) -> str:
    """The name the agent gives a plugin option key: upper case, ``-`` and white space as ``_``.

    Keys are compared as ASCII names: every option name is ASCII, and
    ``str.upper`` agrees with the agent's upper case on ASCII.
    """
    return _KEY_SEPARATORS.sub("_", key.upper())


def key_names(key: Any, value: Any) -> frozenset[str]:
    """Every name under which the plugin can read ``key`` holding ``value``; none for a non-string.

    The agent names a variable by the names of the keys on the way, joined
    with ``_``. When that name holds a run of ``_``, it also sets the name
    with every run made one. So a key is read under its ``agent_key`` and
    under that with every run of ``_`` made one, without ``_`` at its start
    (it joins the one before it) and, when ``value`` holds further options (a
    mapping or a list), without ``_`` at its end (it joins the one after it).
    """
    if not isinstance(key, str):
        return frozenset()
    plain = agent_key(key)
    squeezed = _UNDERSCORES.sub("_", plain).lstrip("_")
    if isinstance(value, (dict, list)):
        squeezed = squeezed.rstrip("_")
    return frozenset((plain, squeezed))


def reads_as(key: Any, value: Any, option: str) -> bool:
    """True when the plugin reads ``key`` holding ``value`` as ``option``."""
    return agent_key(option) in key_names(key, value)


class EntryError(ValueError):
    """An option that cannot be read; turned into a value by the callers."""


def option_items(options: Mapping[Any, Any], option: str) -> list[tuple[Any, Any]]:
    """The keys and values of ``options`` that the plugin reads as ``option``, in order."""
    return [(k, v) for k, v in options.items() if reads_as(k, v, option)]


def option_value(options: Mapping[Any, Any], option: str) -> Any:
    """The value of ``option`` in ``options`` under any key the plugin reads as it; None if absent.

    Raises ``EntryError`` when two such keys hold different values: which one
    the plugin would see is not decided by the document.
    """
    items = option_items(options, option)
    differing = [(k, v) for k, v in items if v != items[0][1]]
    if differing:
        (k1, v1), (k2, v2) = items[0], differing[0]
        raise EntryError(
            f"{option} is given twice with different values: {shown_value(v1)} as "
            f"{shown_value(k1)} and {shown_value(v2)} as {shown_value(k2)}"
        )
    return items[0][1] if items else None


# ---------------------------------------------------------------------------
# The plugin's name
# ---------------------------------------------------------------------------

_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def holds_plugin_name(text: str) -> bool:
    """True when ``text`` holds ``PLUGIN`` in any ASCII case."""
    return PLUGIN in text.translate(_ASCII_LOWER)


def is_plugin_key(k: Any) -> bool:
    """True for a mapping key naming the artifacts plugin: its part before ``#`` is ``PLUGIN``."""
    return isinstance(k, str) and k.split("#", 1)[0] == PLUGIN


def names_plugin_another_way(k: Any) -> bool:
    """True for a key whose part before ``#`` holds ``PLUGIN`` in any ASCII case and is not it.

    The agent runs a plugin for such a key (GitHub's names have no case),
    while the reader reads only ``is_plugin_key``.
    """
    if not isinstance(k, str):
        return False
    name = k.split("#", 1)[0]
    return name != PLUGIN and holds_plugin_name(name)


# ---------------------------------------------------------------------------
# Walks over a document
# ---------------------------------------------------------------------------


def mappings(value: Any) -> list[dict[Any, Any]]:
    """Every mapping in ``value``, at any depth, in document order.

    Each mapping and list is visited once, however many ways lead to it: a
    document parsed with aliases shares its values, and the walk neither
    loops nor repeats.
    """
    found: list[dict[Any, Any]] = []
    seen: set[int] = set()
    pending = [value]
    while pending:
        v = pending.pop()
        if not isinstance(v, (dict, list)) or id(v) in seen:
            continue
        seen.add(id(v))
        if isinstance(v, dict):
            found.append(v)
            pending.extend(reversed(list(v.values())))
        else:
            pending.extend(reversed(v))
    return found


def mapping_items(value: Any) -> list[tuple[Any, Any]]:
    """Every key and value of every mapping in ``value`` (``mappings``), in document order."""
    return [item for m in mappings(value) for item in m.items()]


def plugin_configurations(value: Any) -> list[Any]:
    """The value under every key naming the artifacts plugin in ``value``, each once, in order."""
    found: dict[int, Any] = {}
    for k, v in mapping_items(value):
        if is_plugin_key(k):
            found.setdefault(id(v), v)
    return list(found.values())


# ---------------------------------------------------------------------------
# Blocks and download entries
# ---------------------------------------------------------------------------


def plugin_block(config: Any, block: str) -> dict[str, Any] | None:
    """The mapping a plugin configuration holds as option ``block``; None when it holds none.

    ``block`` is ``download``, ``upload`` or ``promote``. Raises ``EntryError``
    when the block is given twice with different values.
    """
    found = option_value(config, block) if isinstance(config, dict) else None
    return found if isinstance(found, dict) else None


def plugin_download_entries(config: Any) -> list[dict[str, Any]]:
    """The mappings in ``download.projects`` of one artifacts plugin configuration.

    Raises ``EntryError`` when ``download`` or ``projects`` is given twice
    with different values.
    """
    download = plugin_block(config, DOWNLOAD)
    projects = option_value(download, PROJECTS) if download is not None else None
    if not isinstance(projects, list):
        return []
    return [e for e in projects if isinstance(e, dict)]


def download_entries(value: Any) -> list[dict[str, Any]]:
    """Every download entry in ``value``, at any depth, in document order, each once.

    A download entry is a mapping in the ``download.projects`` list of the
    value under a key that names the artifacts plugin (``is_plugin_key``),
    wherever that key sits. One mapping reached in several ways is one entry.
    Raises ``EntryError`` when a configuration gives ``download`` or
    ``projects`` twice with different values.
    """
    found: dict[int, dict[str, Any]] = {}
    for config in plugin_configurations(value):
        for e in plugin_download_entries(config):
            found.setdefault(id(e), e)
    return list(found.values())


# ---------------------------------------------------------------------------
# The plugin named another way
# ---------------------------------------------------------------------------


def misnamed_plugins(value: Any) -> list[str]:
    """Why each key in ``value`` that names the plugin another way is an error."""
    return [
        f"the plugin is named {shown_value(k)}: the agent runs it for that key, and the "
        f"reading rule reads only {PLUGIN} or {PLUGIN}#<ref>"
        for k, _ in mapping_items(value)
        if names_plugin_another_way(k)
    ]
