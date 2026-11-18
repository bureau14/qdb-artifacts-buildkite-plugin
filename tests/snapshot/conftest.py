"""Test setup of the planning half.

The package is imported as ``lib.snapshot``, with the plugin's root on the
path, however pytest is started.
"""

import os
import sys
from pathlib import Path

import pytest
from repos import T, commit, git, new_repository, served

ROOT = Path(__file__).resolve().parents[2]

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture
def isolated_git(monkeypatch):
    """Git without the user's configuration, with a fixed author, as on an agent.

    For tests on local repositories (``repos.py``): neither their commands
    nor ``RealGit`` then see the user's git settings.
    """
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "snapshot test")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "test@example.invalid")


@pytest.fixture
def lines(isolated_git, tmp_path):
    """A repository whose branches fork from each other at known times.

    master: c0 (T+1000), c1 (T+2000). 3.14.x: from c0, d1 (T+1500).
    from-3.14.x: from d1. from-master: from c1. 2.0.x: no common history.
    Two branches that are not release lines: 3.14.x-pin, release/1.0.x.
    """
    r = new_repository(tmp_path / "lines")
    commit(r, {"f": "0"}, "c0", T + 1000)
    git(r, "branch", "3.14.x")
    commit(r, {"f": "1"}, "c1", T + 2000)
    git(r, "checkout", "-q", "-b", "from-master")
    commit(r, {"g": "1"}, "g1", T + 3100)
    git(r, "checkout", "-q", "3.14.x")
    commit(r, {"f": "d"}, "d1", T + 1500)
    for b in ("3.14.x-pin", "release/1.0.x"):
        git(r, "branch", b)
    git(r, "checkout", "-q", "-b", "from-3.14.x")
    commit(r, {"h": "1"}, "f1", T + 3000)
    git(r, "checkout", "-q", "--orphan", "2.0.x")
    commit(r, {"o": "1"}, "orphan", T + 5000)
    git(r, "checkout", "-q", "master")
    return {"path": r, "url": served(r)}
