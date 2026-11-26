"""The release line of a root build.

A release line is a branch named ``master``, ``main`` or ``<digits>.<digits>.x``. A build's
line is its branch when that is one, else the line of its repository whose merge-base with
its commit is the most recent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

NUMBERED_LINE = re.compile(r"(?P<major>[0-9]+)\.(?P<minor>[0-9]+)\.x")


def is_release_line(branch: str) -> bool:
    return branch in ("master", "main") or NUMBERED_LINE.fullmatch(branch) is not None


def line_rank(line: str) -> tuple[int, int, int]:
    """The order of lines whose merge-bases are equally recent: ``master``, then ``main``,
    then the numbered lines, the newest first."""
    if line in ("master", "main"):
        return (2 if line == "master" else 1), 0, 0
    m = NUMBERED_LINE.fullmatch(line)
    return (0, int(m["major"]), int(m["minor"])) if m else (-1, 0, 0)


def choose_release_line(merge_base_times: Mapping[str, int | None]) -> str:
    """The line whose merge-base time is the most recent; ``master`` when none has one."""
    known = [(t, line_rank(line), line) for line, t in merge_base_times.items() if t is not None]
    return max(known)[2] if known else "master"


def release_line(git, url: str, branch: str, of: str | None = None) -> str:
    """The release line of ``branch`` of the repository at ``url``: ``branch`` itself when it
    is one, with no question to git. ``git`` has the branch's commit checked out, or it is
    given as ``of``."""
    if is_release_line(branch):
        return branch
    lines = {b: c for b, c in sorted(git.branches(url).items()) if is_release_line(b)}
    times = git.merge_base_times(url, sorted(set(lines.values())), of)
    return choose_release_line({line: times.get(c) for line, c in lines.items()})
