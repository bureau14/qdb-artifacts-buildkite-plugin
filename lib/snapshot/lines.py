"""The release line of a root build.

A release line is a branch named ``master``, ``main`` or ``<digits>.<digits>.x``;
a tag never is one. The root's line is its branch when that is one, else the
line of the root's repository whose merge-base with the root's commit is the
most recent. The planner takes a dependency without a pin from the root's
branch, else from that line, else from ``master``, else from ``main``.

The rule is here; git gives only the facts it is decided on (``Git.branches``
and ``Git.merge_base_times``).
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from .git import Git

DEFAULT_LINE = "master"

_NUMBERED_LINE = re.compile(r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.x")


def is_release_line(branch: str) -> bool:
    """True for a branch named ``master``, ``main`` or ``<digits>.<digits>.x``."""
    return branch in ("master", "main") or _NUMBERED_LINE.fullmatch(branch) is not None


def line_rank(line: str) -> tuple[int, int, int]:
    """The order of release lines whose merge-bases are equally recent.

    ``master``, then ``main``, then the numbered lines, the newest first.
    """
    if line == "master":
        return 2, 0, 0
    if line == "main":
        return 1, 0, 0
    m = _NUMBERED_LINE.fullmatch(line)
    return (0, int(m["major"]), int(m["minor"])) if m else (-1, 0, 0)


def choose_release_line(merge_base_times: Mapping[str, int | None]) -> str:
    """The line whose merge-base is the most recent.

    ``merge_base_times`` maps each release line to the committer time of its
    merge-base with the root's commit, None when there is none. Equal times
    are decided by ``line_rank``; ``master`` when no time is known.
    """
    known = [(t, line_rank(line), line) for line, t in merge_base_times.items() if t is not None]
    return max(known)[2] if known else DEFAULT_LINE


def release_line(git: Git, url: str, branch: str) -> str:
    """The release line of ``branch`` of the repository at ``url``, checked out by ``git``.

    ``branch`` itself when it is a release line, with no question to git.
    """
    if is_release_line(branch):
        return branch
    lines = {b: c for b, c in sorted(git.branches(url).items()) if is_release_line(b)}
    times = git.merge_base_times(url, sorted(set(lines.values())))
    return choose_release_line({line: times.get(c) for line, c in lines.items()})
