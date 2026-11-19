"""Everything the planning half asks of git (specification sections 2 to 4).

Two interfaces, one for each caller. ``Git`` is what the planner asks:

- ``commits_of(url, refs)``: the commit of each of the full refs ``refs``
  that the repository at ``url`` has, as ``{ref: commit}``, in one question:
  the head of ``refs/heads/<name>``, or the commit ``refs/tags/<name>``
  names, an annotated tag peeled; a ref it does not have is not in the answer;
- ``read_files(url, commit, directory, wanted)``: the files under
  ``directory`` in the tree of ``commit`` whose path ``wanted`` accepts, as
  ``{path: bytes}``; which paths count is the caller's business;
- ``branches(url)``: ``{branch: commit}`` of every branch of ``url``;
- ``merge_base_times(url, commits)``: for each of ``commits`` of ``url``, the
  committer time of its merge-base with the commit checked out in the current
  directory, None when none is known.

``Checkout`` is what a build asks of its own checkout, in the current
directory:

- ``head_commit()``: the commit checked out;
- ``checkout_files(directory, wanted)``: the same as ``read_files`` of that
  commit, read from its tree and not from the working tree.

What these facts decide (which branch is a release line, which line a branch
belongs to) is not decided here: see ``lines``.

``RealGit`` runs git and is both; ``tests/snapshot/fakes.py`` holds
``FakeGit`` and ``FakeCheckout``. A command that fails raises
``ArtifactsError``: a transport error is never read as "no such ref" or "no
files". An absent ref is an answer (None). Every value comes after ``--`` or
``--end-of-options`` on a git command line, so git never reads it as an
option, and nothing goes through a shell.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Protocol

from ..errors import ArtifactsError
from ..forms import BRANCH_PREFIX, TAG_PREFIX
from ..options import checkout_commit

# How ``git ls-remote`` names the commit an annotated tag points to.
PEELED = "^{}"

# Sent on every command that talks to a remote: git then gives the job's
# credential helper's answer at once instead of after a 401 (experiment 33).
# Every job has the helper; without one, git fails at once, even on a public
# repository ("could not read Username").
PROACTIVE_AUTH = ("-c", "http.proactiveAuth=basic")


class Git(Protocol):
    """What the planner asks of git. See the module docstring."""

    def commits_of(self, url: str, refs: Sequence[str]) -> Mapping[str, str]: ...

    def read_files(
        self, url: str, commit: str, directory: str, wanted: Callable[[str], bool]
    ) -> Mapping[str, bytes]: ...

    def branches(self, url: str) -> Mapping[str, str]: ...

    def merge_base_times(self, url: str, commits: Sequence[str]) -> Mapping[str, int | None]: ...


class Checkout(Protocol):
    """What a build asks of its own checkout. See the module docstring."""

    def head_commit(self) -> str: ...

    def checkout_files(
        self, directory: str, wanted: Callable[[str], bool]
    ) -> Mapping[str, bytes]: ...


def branch_ref(branch: str) -> str:
    """The full ref of ``branch``."""
    return BRANCH_PREFIX + branch


def branch_of(ref: str) -> str | None:
    """The branch a full ref names; None for a tag."""
    return ref[len(BRANCH_PREFIX) :] if ref.startswith(BRANCH_PREFIX) else None


# ---------------------------------------------------------------------------
# Reading git's output
# ---------------------------------------------------------------------------


def listed_refs(output: str) -> dict[str, str]:
    """``{ref: commit}`` of every line of ``git ls-remote`` output (``<commit> TAB <ref>``)."""
    rows = (row.partition("\t") for row in output.splitlines())
    return {ref: commit for commit, _, ref in rows}


def parse_ls_remote(output: str, refs: Sequence[str]) -> dict[str, str]:
    """``{ref: commit}`` of those of ``refs`` that ``git ls-remote`` output lists exactly.

    ``git ls-remote <url> <pattern>`` matches the pattern against the tail of
    ref names, so ``refs/heads/x`` also matches ``refs/heads/a/refs/heads/x``;
    only the exact name counts. An annotated tag is listed as the tag object
    under its name and, when asked for, as the commit it names under
    ``<name>^{}``; that peeled line wins.
    """
    rows = listed_refs(output)
    found = {ref: rows.get(ref + PEELED) or rows.get(ref) for ref in refs}
    return {ref: commit for ref, commit in found.items() if commit is not None}


def parse_branches(output: str) -> dict[str, str]:
    """``{branch: commit}`` from the output of ``git ls-remote --heads``."""
    return {branch_of(ref): commit for ref, commit in listed_refs(output).items()}


def tree_files(listing: bytes) -> list[tuple[str, str]]:
    """``(path, blob id)`` of every blob in ``git ls-tree -r -z`` output.

    Each record is ``<mode> <type> <id>TAB<path>`` ended by NUL; with ``-z``
    paths are not quoted, so a path may hold spaces. A submodule is listed as
    a ``commit``, not a blob, and is never descended into.
    """
    out = []
    for record in listing.split(b"\0"):
        meta, tab, path = record.partition(b"\t")
        _, kind, oid = meta.decode("ascii").split(" ") if tab else ("", "", "")
        if kind == "blob":
            out.append((path.decode("utf-8"), oid))
    return out


def parse_batch(output: bytes, oids: Sequence[str]) -> dict[str, bytes]:
    """The content of each blob of ``oids`` in ``git cat-file --batch`` output.

    For each id asked, in order: ``<id> blob <size>``, a newline, the content
    and a newline.
    """
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        end = output.index(b"\n", pos)
        size = int(output[pos:end].split(b" ")[2])
        blobs[oid] = output[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


# ---------------------------------------------------------------------------
# Running git
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Completed:
    """The outcome of one command; standard output kept as bytes, for blobs."""

    returncode: int
    stdout: bytes
    stderr: str


Runner = Callable[[Sequence[str], str, "bytes | None", Mapping[str, str]], Completed]


def run_command(
    args: Sequence[str], cwd: str, stdin: bytes | None, env: Mapping[str, str]
) -> Completed:
    """Run ``args`` in ``cwd`` with ``env``; never raises on exit status."""
    p = subprocess.run(
        list(args), cwd=cwd, env=dict(env), input=stdin, capture_output=True, check=False
    )
    return Completed(p.returncode, p.stdout, p.stderr.decode("utf-8", "replace"))


def git_environment(base: Mapping[str, str]) -> dict[str, str]:
    """``base`` with what every git command here runs under: no prompt for credentials, and
    messages in English. The job's credential helper (``GIT_CONFIG_*``) is kept."""
    return {**base, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}


def fetch_commit_arguments(url: str, commit: str) -> list[str]:
    """``git fetch`` of one commit and its trees, without blobs or history, into FETCH_HEAD."""
    return [
        *("fetch", "-q", "--no-tags", "--recurse-submodules=no"),
        *("--depth", "1", "--filter=blob:none", "--", url, commit),
    ]


def fetch_objects_arguments(url: str, oids: Sequence[str]) -> list[str]:
    """``git fetch`` of objects by id, writing neither a ref nor FETCH_HEAD."""
    return [
        *("fetch", "-q", "--no-tags", "--recurse-submodules=no"),
        *("--no-write-fetch-head", "--", url, *oids),
    ]


def _ignore(msg: str) -> None:
    pass


# ---------------------------------------------------------------------------
# The real implementation
# ---------------------------------------------------------------------------


class RealGit:
    """``Git`` and ``Checkout`` over the git command line.

    ``cwd`` is the checkout of the current build. Every command that talks to
    a remote carries ``PROACTIVE_AUTH``. Another repository's commit is read
    in a scratch repository of its own, removed when the read is done, so
    nothing is kept between calls. ``run`` executes one command; tests may
    replace it. ``log`` receives the merge-base times found, which the choice
    of a release line rests on.
    """

    def __init__(
        self, cwd: str = ".", run: Runner = run_command, log: Callable[[str], None] = _ignore
    ) -> None:
        self._cwd = cwd
        self._run = run
        self._log = log

    def _git(
        self, args: Sequence[str], cwd: str | None = None, stdin: bytes | None = None
    ) -> Completed:
        return self._run(["git", *args], cwd or self._cwd, stdin, git_environment(os.environ))

    def _ok(self, args: Sequence[str], cwd: str | None = None, stdin: bytes | None = None) -> bytes:
        c = self._git(args, cwd, stdin)
        if c.returncode != 0:
            shown = " ".join(["git", *args])
            raise ArtifactsError(f"{shown} failed ({c.returncode}): {c.stderr.strip()}")
        return c.stdout

    def _remote(self, args: Sequence[str], cwd: str | None = None) -> bytes:
        """``_ok`` of a command that talks to a remote, with ``PROACTIVE_AUTH``."""
        return self._ok([*PROACTIVE_AUTH, *args], cwd)

    def commits_of(self, url: str, refs: Sequence[str]) -> dict[str, str]:
        """``{ref: commit}`` of those of the full refs ``refs`` that ``url`` has, in one call.

        A tag is also asked for peeled (``<ref>^{}``), so an annotated tag
        gives the commit it names, not the tag object.
        """
        asked = list(dict.fromkeys(refs))
        tags = [r + PEELED for r in asked if r.startswith(TAG_PREFIX)]
        out = self._remote(["ls-remote", "--", url, *asked, *tags])
        return parse_ls_remote(out.decode("utf-8", "replace"), asked)

    def branches(self, url: str) -> dict[str, str]:
        """``{branch: commit}`` of every branch of ``url`` (``git ls-remote --heads``)."""
        out = self._remote(["ls-remote", "--heads", "--", url])
        return parse_branches(out.decode("utf-8", "replace"))

    def read_files(
        self, url: str, commit: str, directory: str, wanted: Callable[[str], bool]
    ) -> dict[str, bytes]:
        """The files under ``directory`` at ``commit`` of ``url`` that ``wanted`` accepts.

        Experiment 33's recipe r4p, in a new scratch repository: one blobless
        fetch of the commit, one listing of ``directory``, one fetch of the
        blobs of the files that count, and ``git cat-file --batch``. Two
        fetches whatever the number of files; none for blobs when no file
        counts.
        """
        with tempfile.TemporaryDirectory(prefix="snapshot-git-") as d:
            self._ok(["init", "-q"], d)
            self._remote(fetch_commit_arguments(url, commit), d)
            files = self._listed("FETCH_HEAD", directory, wanted, d)
            if not files:
                return {}
            self._remote(fetch_objects_arguments(url, list(dict.fromkeys(files.values()))), d)
            return self._contents(files, d)

    def checkout_files(self, directory: str, wanted: Callable[[str], bool]) -> dict[str, bytes]:
        """The files under ``directory`` at HEAD of ``cwd`` that ``wanted`` accepts.

        Read from the tree of the commit, never from the working tree, so a
        file a job wrote or changed there (the generated pipeline) is not
        what is read. Local: no command talks to a remote.
        """
        files = self._listed("HEAD", directory, wanted)
        return self._contents(files) if files else {}

    def _listed(
        self, tree: str, directory: str, wanted: Callable[[str], bool], cwd: str | None = None
    ) -> dict[str, str]:
        """``{path: blob id}`` of the files under ``directory`` of ``tree`` that count."""
        listing = self._ok(["ls-tree", "--full-tree", "-r", "-z", tree, "--", directory], cwd)
        return {path: oid for path, oid in tree_files(listing) if wanted(path)}

    def _contents(self, files: Mapping[str, str], cwd: str | None = None) -> dict[str, bytes]:
        """The content of each of ``files`` (``{path: blob id}``), in one ``cat-file --batch``."""
        oids = list(dict.fromkeys(files.values()))
        batch = self._ok(["cat-file", "--batch"], cwd, "".join(f"{o}\n" for o in oids).encode())
        blobs = parse_batch(batch, oids)
        return {path: blobs[oid] for path, oid in files.items()}

    def head_commit(self) -> str:
        """The commit checked out in ``cwd`` (``git rev-parse HEAD``)."""
        return checkout_commit(self._cwd)

    def _merge_base_time(self, commit: str) -> int:
        """Committer time of the merge-base of HEAD and ``commit``."""
        base = self._ok(["merge-base", "--end-of-options", "HEAD", commit]).decode("ascii")
        shown = self._ok(["show", "-s", "--format=%ct", "--end-of-options", base.strip()])
        return int(shown.decode("ascii").strip())

    def merge_base_times(self, url: str, commits: Sequence[str]) -> dict[str, int]:
        """For each of ``commits`` of ``url``: the committer time of its merge-base with HEAD.

        The commits are fetched into the checkout in one fetch, objects only
        (no ref, no FETCH_HEAD).
        """
        wanted = list(dict.fromkeys(commits))
        if wanted:
            self._remote(fetch_objects_arguments(url, wanted))
        times = {c: self._merge_base_time(c) for c in wanted}
        shown = ", ".join(f"{c[:12]} {t}" for c, t in times.items())
        self._log(f"merge-base times of HEAD with {url}: {shown or 'none asked'}")
        return times
