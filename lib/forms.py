"""The form of each value a snapshot names: a project, a key, a variant, a ref, a
commit, a build and a time.

A pointer, a plan and a failure record are read long after they were written,
by other builds, and what they hold goes into object keys, download options,
step commands and messages. So every value is checked for its form when it is
written and again when it is read, and the job that writes a pointer and the
plan step that reads it check one form: the one here.

This is ordinary job code, as the pointer is: nothing here imports ``snapshot``.
"""

from __future__ import annotations

import datetime
import re
from typing import Any

BRANCH_PREFIX = "refs/heads/"
TAG_PREFIX = "refs/tags/"


def _fully_matches(pattern: re.Pattern, value: Any) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")


def is_slug(value: Any) -> bool:
    """True for a Buildkite pipeline slug: lowercase letters, digits and ``-``, not first.

    Every project id is one, so it can be asked of Buildkite, quoted in a
    message and made part of a step key as it is.
    """
    return _fully_matches(_SLUG, value)


_KEY = re.compile(r"v1-[0-9a-f]{32}")


def is_key(value: Any) -> bool:
    """True for text of the form of a key: ``v1-`` and 32 lowercase hex digits."""
    return _fully_matches(_KEY, value)


_VARIANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


def is_variant(value: Any) -> bool:
    """True for a variant name: ASCII letters, digits, ``.``, ``_`` and ``-``, not first.

    Every variant in use is one. A variant names one object of the bucket and
    goes into step commands and messages, so it holds nothing a path, a shell
    or the upload's interpolation would read.
    """
    return _fully_matches(_VARIANT, value)


_OBJECT_ID = re.compile(r"[0-9a-f]{40}")


def is_object_id(value: Any) -> bool:
    """True for a full object id, as git names a commit: 40 lowercase hex digits."""
    return _fully_matches(_OBJECT_ID, value)


_BUILD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_build_id(value: Any) -> bool:
    """True for a build uuid, as Buildkite writes it: lowercase hex digits in five groups."""
    return _fully_matches(_BUILD_ID, value)


_HTTPS_URL = re.compile(r"https://[A-Za-z0-9._~:/%+-]+")


def is_https_url(value: Any) -> bool:
    """True for an ``https://`` URL without spaces, quotes or credentials."""
    return _fully_matches(_HTTPS_URL, value)


def is_count(value: Any) -> bool:
    """True for a build number: an integer that is not a bool and not negative."""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


# The bytes no ref name holds (``refname_disposition`` in git's refs.c): the
# ASCII control characters, DEL, space, and ~ ^ : ? * [ \.
_REFUSED_IN_REFS = frozenset(range(0x20)) | frozenset(b"\x7f ~^:?*[\\")


def _is_ref_component(component: bytes) -> bool:
    return bool(component) and not component.startswith(b".") and not component.endswith(b".lock")


def is_ref_name(name: str) -> bool:
    """True for a name git accepts as a branch, as ``git check-ref-format --branch <name>``.

    The rules of git's ``check_refname_format`` on ``refs/heads/<name>``, on the
    name's UTF-8 bytes: no byte of ``_REFUSED_IN_REFS``; no ``..`` and no
    ``@{``; not ending with ``.``; no empty component (so no ``/`` at either
    end and no ``//``), and none that starts with ``.`` or ends with
    ``.lock``. And those of ``strbuf_check_branch_ref``: not starting with
    ``-``, and not ``HEAD``. Any other byte is allowed, so a name may hold
    ``$``, quotes and letters that are not ASCII, and may be of any length.
    A tag name under ``refs/tags/`` follows the same rules (``check_tag_ref``).
    A name that is not UTF-8 (it holds a lone surrogate) is no name.
    """
    try:
        raw = name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return (
        not raw.startswith(b"-")
        and raw != b"HEAD"
        and not any(b in _REFUSED_IN_REFS for b in raw)
        and b".." not in raw
        and b"@{" not in raw
        and not raw.endswith(b".")
        and all(_is_ref_component(c) for c in raw.split(b"/"))
    )


def is_full_ref(value: Any) -> bool:
    """True for ``refs/heads/<name>`` or ``refs/tags/<name>`` with ``is_ref_name(<name>)``."""
    if not isinstance(value, str):
        return False
    prefix = next((p for p in (BRANCH_PREFIX, TAG_PREFIX) if value.startswith(p)), None)
    return prefix is not None and is_ref_name(value[len(prefix) :])


_ISO_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")


def is_iso_utc(value: Any) -> bool:
    """True for a time as ``iso_utc`` writes it."""
    return _fully_matches(_ISO_UTC, value)


def iso_utc(epoch: float) -> str:
    """``epoch`` seconds as UTC ISO 8601 with a "Z", to the millisecond."""
    t = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
