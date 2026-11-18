"""How a foreign value or text appears in a message.

The messages of the planning half go into the job log and, through the plan
step, into an annotation. What they quote comes from other repositories, from
Buildkite's API or from a plan in build meta-data, so it may be of any size and
hold any character. ``shown_value`` and ``shown_text`` give it as one line of
printable ASCII of bounded length: a quoted value can neither forge a line of
a message, nor carry a terminal escape sequence, nor make a message that cannot
be encoded, nor make a message of any size.
"""

from __future__ import annotations

import reprlib
from typing import Any

VALUE_MAX = 100
TEXT_MAX = 200
SHORT_COMMIT = 12
# Python 3.9 turns an integer into text in quadratic time (a million digits:
# 12 s), so a larger one is shown by its size.
INT_BITS_MAX = 256


class _Short(reprlib.Repr):
    """``repr`` that shows a few members of a few levels, and a large integer by its size."""

    def repr_int(self, x: int, level: int) -> str:
        if x.bit_length() > INT_BITS_MAX:
            return f"<int of {x.bit_length()} bits>"
        return super().repr_int(x, level)


_REPR = _Short()
_REPR.maxlevel = 2
_REPR.maxdict = _REPR.maxlist = _REPR.maxtuple = _REPR.maxset = _REPR.maxfrozenset = 3
_REPR.maxstring = _REPR.maxother = 60


def _printable_ascii(text: str) -> str:
    """``text`` with every character outside printable ASCII written as its escape."""
    return "".join(
        c if " " <= c <= "~" else c.encode("unicode_escape").decode("ascii") for c in text
    )


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def shown_value(value: Any) -> str:
    """``value`` as Python writes it, shortened: at most ``VALUE_MAX`` characters of ASCII."""
    return _cut(_printable_ascii(_REPR.repr(value)), VALUE_MAX)


def shown_text(text: str) -> str:
    """``text`` as it is, shortened: at most ``TEXT_MAX`` characters of ASCII, without quotes."""
    return _cut(_printable_ascii(text[: TEXT_MAX + 1]), TEXT_MAX)


def short_commit(commit: str) -> str:
    """A commit id as a label or a message shows it: its first ``SHORT_COMMIT`` hex digits."""
    return commit[:SHORT_COMMIT]
