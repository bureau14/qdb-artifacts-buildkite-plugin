"""Local git repositories for tests, served over file:// as GitHub serves them.

Tests that use these ask for the fixture ``isolated_git`` (``conftest.py``),
so that neither the commands here nor ``RealGit`` see the user's git
configuration.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# A commit time well inside what git reads as epoch seconds.
T = 1_700_000_000


def git(cwd: Path, *args: str, date: int | None = None) -> str:
    env = dict(os.environ)
    if date is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = f"@{date} +0000"
    p = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, check=True)
    return p.stdout.decode().strip()


def write(repo: Path, files: dict) -> None:
    for path, content in files.items():
        f = repo / path
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(content if isinstance(content, bytes) else content.encode())


def commit(repo: Path, files: dict, message: str, date: int = T) -> str:
    write(repo, files)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message, date=date)
    return git(repo, "rev-parse", "HEAD")


def new_repository(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "master")
    return path


def served(repo: Path) -> str:
    """Let clients fetch any commit and filter blobs, as GitHub does; the file:// URL."""
    git(repo, "config", "uploadpack.allowFilter", "true")
    git(repo, "config", "uploadpack.allowAnySHA1InWant", "true")
    return repo.as_uri()


def clone(url: str, into: Path, branch: str, *options: str) -> Path:
    """A checkout of ``branch``, as an agent's checkout of a build."""
    git(into.parent, "clone", "-q", *options, "--branch", branch, url, str(into))
    return into
