"""Everything the planning side asks of git.

Of another repository (``url``):

- ``commits_of(url, refs)``: ``{ref: commit}`` of those of the full refs ``refs`` it has, an
  annotated tag peeled;
- ``read_files(url, commit, directory, wanted)``: ``{path: bytes}`` of the files under
  ``directory`` in the tree of ``commit`` whose path ``wanted`` accepts;
- ``branches(url)``: ``{branch: commit}`` of every branch;
- ``merge_base_times(url, commits)``: for each commit, the committer time of its merge-base
  with the commit checked out; None for one that shares no history with it.

Of the build's own checkout: ``head_commit()``, and ``checkout_files(directory, wanted)``,
read from the commit's tree and never from the working tree.

A command that fails raises ``ArtifactsError``: a transport error is never read as "no such
ref". Every value comes after ``--`` or ``--end-of-options``, so git never reads it as an
option, and nothing goes through a shell.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from ..errors import ArtifactsError
from ..forms import branch_of, is_tag
from ..job import checkout_commit

# How ``git ls-remote`` names the commit an annotated tag points to.
PEELED = "^{}"

# Git then gives the job's credential helper's answer at once instead of after a 401; without
# it git fails at once, even on a public repository.
PROACTIVE_AUTH = ("-c", "http.proactiveAuth=basic")

FETCH = ("fetch", "-q", "--no-tags", "--recurse-submodules=no")


def listed_refs(output: str) -> dict[str, str]:
    """``{ref: commit}`` of every line of ``git ls-remote`` output (``<commit> TAB <ref>``)."""
    rows = (row.partition("\t") for row in output.splitlines())
    return {ref: commit for commit, _, ref in rows}


def parse_ls_remote(output: str, refs: Sequence[str]) -> dict[str, str]:
    """``{ref: commit}`` of those of ``refs`` that the output lists exactly: ls-remote matches
    a pattern against the tail of ref names. A tag's peeled line wins."""
    rows = listed_refs(output)
    found = {ref: rows.get(ref + PEELED) or rows.get(ref) for ref in refs}
    return {ref: commit for ref, commit in found.items() if commit is not None}


def tree_files(listing: bytes) -> list[tuple[str, str]]:
    """``(path, blob id)`` of every blob in ``git ls-tree -r -z`` output. A submodule is listed
    as a commit, not a blob, and is never descended into."""
    out = []
    for record in listing.split(b"\0"):
        meta, tab, path = record.partition(b"\t")
        _, kind, oid = meta.decode("ascii").split(" ") if tab else ("", "", "")
        if kind == "blob":
            out.append((path.decode("utf-8"), oid))
    return out


def parse_batch(output: bytes, oids: Sequence[str]) -> dict[str, bytes]:
    """The content of each blob of ``oids`` in ``git cat-file --batch`` output: for each id, in
    order, ``<id> blob <size>``, a newline, the content and a newline."""
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        end = output.index(b"\n", pos)
        size = int(output[pos:end].split(b" ")[2])
        blobs[oid] = output[end + 1 : end + 1 + size]
        pos = end + 1 + size + 1
    return blobs


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: bytes
    stderr: str


def run_command(
    args: Sequence[str], cwd: str, stdin: bytes | None, env: Mapping[str, str]
) -> Completed:
    p = subprocess.run(
        list(args), cwd=cwd, env=dict(env), input=stdin, capture_output=True, check=False
    )
    return Completed(p.returncode, p.stdout, p.stderr.decode("utf-8", "replace"))


def unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


class RealGit:
    """Git over its command line, in the checkout ``cwd`` of the build. ``run`` executes one
    command; ``log`` receives the merge-base times a release line is chosen by."""

    def __init__(self, cwd: str = ".", run=run_command, log: Callable[[str], None] = print) -> None:
        self._cwd = cwd
        self._run = run
        self._log = log

    def _git(self, args: Sequence[str], cwd: str | None, stdin: bytes | None = None) -> Completed:
        # No prompt for credentials, and messages in English; the job's credential helper stays.
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        return self._run(["git", *args], cwd or self._cwd, stdin, env)

    def _ok(self, args: Sequence[str], cwd: str | None = None, stdin: bytes | None = None) -> bytes:
        c = self._git(args, cwd, stdin)
        if c.returncode != 0:
            shown = " ".join(["git", *args])
            raise ArtifactsError(f"{shown} failed ({c.returncode}): {c.stderr.strip()}")
        return c.stdout

    def _remote(self, args: Sequence[str], cwd: str | None = None) -> bytes:
        return self._ok([*PROACTIVE_AUTH, *args], cwd)

    def commits_of(self, url: str, refs: Sequence[str]) -> dict[str, str]:
        asked = unique(refs)
        tags = [r + PEELED for r in asked if is_tag(r)]
        out = self._remote(["ls-remote", "--", url, *asked, *tags])
        return parse_ls_remote(out.decode("utf-8", "replace"), asked)

    def branches(self, url: str) -> dict[str, str]:
        out = self._remote(["ls-remote", "--heads", "--", url]).decode("utf-8", "replace")
        return {branch_of(ref): commit for ref, commit in listed_refs(out).items()}

    def read_files(
        self, url: str, commit: str, directory: str, wanted: Callable[[str], bool]
    ) -> dict[str, bytes]:
        """In a scratch repository of its own: one fetch of the commit without blobs, then one
        fetch of the blobs of the files that count."""
        with tempfile.TemporaryDirectory(prefix="snapshot-git-") as d:
            self._ok(["init", "-q"], d)
            self._remote([*FETCH, "--depth", "1", "--filter=blob:none", "--", url, commit], d)
            files = self._listed("FETCH_HEAD", directory, wanted, d)
            if not files:
                return {}
            self._remote([*FETCH, "--no-write-fetch-head", "--", url, *unique(files.values())], d)
            return self._contents(files, d)

    def checkout_files(self, directory: str, wanted: Callable[[str], bool]) -> dict[str, bytes]:
        files = self._listed("HEAD", directory, wanted)
        return self._contents(files) if files else {}

    def _listed(
        self, tree: str, directory: str, wanted: Callable[[str], bool], cwd: str | None = None
    ) -> dict[str, str]:
        """``{path: blob id}`` of the files under ``directory`` of ``tree`` that count."""
        listing = self._ok(["ls-tree", "--full-tree", "-r", "-z", tree, "--", directory], cwd)
        return {path: oid for path, oid in tree_files(listing) if wanted(path)}

    def _contents(self, files: Mapping[str, str], cwd: str | None = None) -> dict[str, bytes]:
        oids = unique(files.values())
        batch = self._ok(["cat-file", "--batch"], cwd, "".join(f"{o}\n" for o in oids).encode())
        blobs = parse_batch(batch, oids)
        return {path: blobs[oid] for path, oid in files.items()}

    def head_commit(self) -> str:
        return checkout_commit(self._cwd)

    def _merge_base_time(self, commit: str) -> int | None:
        """Committer time of the merge-base of HEAD and ``commit``; None when they share no
        history, for which ``git merge-base`` exits 1 and prints nothing."""
        args = ["merge-base", "--end-of-options", "HEAD", commit]
        c = self._git(args, None)
        if c.returncode == 1 and not c.stdout.strip() and not c.stderr.strip():
            return None
        if c.returncode != 0:
            raise ArtifactsError(
                f"git {' '.join(args)} failed ({c.returncode}): {c.stderr.strip()}"
            )
        base = c.stdout.decode("ascii").strip()
        shown = self._ok(["show", "-s", "--format=%ct", "--end-of-options", base])
        return int(shown.decode("ascii").strip())

    def merge_base_times(self, url: str, commits: Sequence[str]) -> dict[str, int | None]:
        """The commits are fetched into the checkout in one fetch, objects only."""
        wanted = unique(commits)
        if wanted:
            self._remote([*FETCH, "--no-write-fetch-head", "--", url, *wanted])
        times = {c: self._merge_base_time(c) for c in wanted}
        shown = ", ".join(
            f"{c[:12]} {'no shared history' if t is None else t}" for c, t in times.items()
        )
        self._log(f"merge-base times of HEAD with {url}: {shown or 'none asked'}")
        return times
