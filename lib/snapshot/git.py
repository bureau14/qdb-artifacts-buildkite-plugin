"""Everything the planning half asks of git (specification sections 2 to 4).

Two interfaces, one for each caller. ``Git`` is what the planner asks:

- ``commits_of(url, refs)``: the commit of each of the full refs ``refs``
  that the repository at ``url`` has, as ``{ref: commit}``, in one question:
  the head of ``refs/heads/<name>``, or the commit ``refs/tags/<name>``
  names, an annotated tag peeled; a ref it does not have is not in the answer;
- ``read_files(url, commit, directory, wanted)``: the regular files under
  ``directory`` in the tree of ``commit`` whose path ``wanted`` accepts, as
  ``{path: bytes}``; which paths count is the caller's business;
- ``branches(url)``: ``{branch: commit}`` of every branch of ``url``;
- ``merge_base_times(url, commits)``: for each of ``commits`` of ``url``, the
  committer time of its merge-base with the commit checked out in the current
  directory, None when there is none or the checkout is shallow.

``Checkout`` is what a build asks of its own checkout, in the current
directory:

- ``head_commit()``: the commit checked out;
- ``checkout_files(directory, wanted)``: the same as ``read_files`` of that
  commit, read from its tree and not from the working tree.

What these facts decide (which branch is a release line, which line a branch
belongs to) is not decided here: see ``lines``.

``RealGit`` runs git and is both; ``tests/snapshot/fakes.py`` holds
``FakeGit`` and ``FakeCheckout``. A command
that fails raises ``GitError``: a transport error is never read as "no such
ref" or "no files". An absent ref is an answer (None).

Every URL, ref, commit id and path that reaches a git command line here comes
from another repository's files or from an API, so it is foreign input. Each
is checked against a strict pattern first (``check_url``, ``check_full_ref``,
``check_object_id``, ``check_tree_path``); every value comes after ``--`` or
``--end-of-options``, so git never reads it as an option; pathspecs are
literal; only the transports the caller allows are enabled; nothing goes
through a shell.
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Protocol

from ..forms import BRANCH_PREFIX, TAG_PREFIX, is_full_ref, is_object_id
from ..options import checkout_commit
from .shown import shown_value

# How ``git ls-remote`` names the commit an annotated tag points to.
PEELED = "^{}"

# Sent on every command that talks to a remote: git then gives the job's
# credential helper's answer at once instead of after a 401 (experiment 33).
# Every job has the helper; without one, git fails at once, even on a public
# repository ("could not read Username").
PROACTIVE_AUTH = ("-c", "http.proactiveAuth=basic")

# ``git merge-base`` exits with this status when the commits have no common ancestor.
MERGE_BASE_NONE = 1

# Modes of the tree entries that are files. A symlink (120000) and a
# submodule (160000) are not, and a submodule is never descended into.
REGULAR_FILE_MODES = ("100644", "100755")


class GitError(RuntimeError):
    """A git command failed, or a value was refused before any command ran."""


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


# ---------------------------------------------------------------------------
# Foreign values
# ---------------------------------------------------------------------------

_TREE_PATH = re.compile(r"[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*")
_URL = re.compile(r"(?P<scheme>[a-z][a-z0-9+.-]*)://[A-Za-z0-9._~:/%+-]+")


def check_full_ref(ref: str) -> str:
    if not is_full_ref(ref):
        raise GitError(
            f"refused ref {shown_value(ref)}: not refs/heads/<branch> or refs/tags/<tag>"
        )
    return ref


def branch_ref(branch: str) -> str:
    """The full ref of ``branch``."""
    return BRANCH_PREFIX + branch


def branch_of(ref: str) -> str | None:
    """The branch a full ref names; None for a tag."""
    return ref[len(BRANCH_PREFIX) :] if ref.startswith(BRANCH_PREFIX) else None


def check_object_id(s: str) -> str:
    if not is_object_id(s):
        raise GitError(f"refused object id {shown_value(s)}: not 40 lowercase hex digits")
    return s


def check_tree_path(path: str) -> str:
    """A relative path of plain components: no ``.`` or ``..``, not starting with ``-``."""
    parts = path.split("/")
    plain = _TREE_PATH.fullmatch(path) is not None and not path.startswith("-")
    if not plain or any(p in (".", "..") for p in parts):
        raise GitError(f"refused path {shown_value(path)}: not a plain relative path")
    return path


def shown_url(url: str) -> str:
    """``url`` for a message, with any user part masked: it may hold a token."""
    return re.sub(r"//[^/]*@", "//***@", url)


def is_url(url: str, protocols: Sequence[str]) -> bool:
    """True for a ``<scheme>://...`` URL of ``protocols``, without spaces, quotes or credentials."""
    m = _URL.fullmatch(url)
    return m is not None and m["scheme"] in protocols


def check_url(url: str, protocols: Sequence[str]) -> str:
    if not is_url(url, protocols):
        shown = shown_value(shown_url(url))
        raise GitError(f"refused repository URL {shown}: not a plain URL of {', '.join(protocols)}")
    return url


# ---------------------------------------------------------------------------
# Reading git's output
# ---------------------------------------------------------------------------


def parse_ls_remote(output: str, refs: Sequence[str]) -> dict[str, str]:
    """``{ref: commit}`` of those of ``refs`` that ``git ls-remote`` output lists exactly.

    ``git ls-remote <url> <pattern>`` matches the pattern against the tail of
    ref names, so ``refs/heads/x`` also matches ``refs/heads/a/refs/heads/x``;
    only the exact name counts. An annotated tag is listed as the tag object
    under its name and, when asked for, as the commit it names under
    ``<name>^{}``; that peeled line wins.
    """
    rows = {}
    for row in output.splitlines():
        parts = row.split("\t")
        if len(parts) == 2 and is_object_id(parts[0]):
            rows[parts[1]] = parts[0]
    found = {ref: rows.get(ref + PEELED) or rows.get(ref) for ref in refs}
    return {ref: commit for ref, commit in found.items() if commit is not None}


def parse_branches(output: str) -> dict[str, str]:
    """``{branch: commit}`` from the output of ``git ls-remote --heads``."""
    out = {}
    for row in output.splitlines():
        parts = row.split("\t")
        if len(parts) == 2 and parts[1].startswith(BRANCH_PREFIX) and is_object_id(parts[0]):
            out[parts[1][len(BRANCH_PREFIX) :]] = parts[0]
    return out


# Control characters, and every character Python's ``splitlines`` breaks a line at.
_UNNAMEABLE = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def tree_path(raw: bytes) -> str:
    """A path of a tree as text: UTF-8 without a control character or line break.

    Raises ``GitError`` for any other path: a path is quoted in messages, one
    refusal per line, and in an annotation that is UTF-8.
    """
    try:
        path = raw.decode("utf-8")
    except UnicodeDecodeError:
        path = None
    if path is None or _UNNAMEABLE.search(path):
        shown = shown_value(raw)
        raise GitError(f"refused path {shown} in the tree: not UTF-8 without control characters")
    return path


def tree_files(listing: bytes) -> list[tuple[str, str]]:
    """``(path, blob id)`` of every regular file in ``git ls-tree -r -z`` output.

    Each record is ``<mode> <type> <id>TAB<path>`` ended by NUL; with ``-z``
    paths are not quoted, so a path may hold spaces. A path that is not
    ``tree_path`` raises ``GitError``.
    """
    out = []
    for record in listing.split(b"\0"):
        meta, tab, path = record.partition(b"\t")
        fields = meta.decode("ascii", "replace").split(" ")
        if not tab or len(fields) != 3:
            continue
        mode, kind, oid = fields
        if kind == "blob" and mode in REGULAR_FILE_MODES and is_object_id(oid):
            out.append((tree_path(path), oid))
    return out


def parse_batch(output: bytes, oids: Sequence[str]) -> dict[str, bytes]:
    """The content of each blob of ``oids`` in ``git cat-file --batch`` output.

    For each id asked, in order: ``<id> blob <size>``, a newline, the content
    and a newline. An object that is missing or not a blob, or output that
    does not have this form, raises ``GitError``.
    """
    blobs: dict[str, bytes] = {}
    pos = 0
    for oid in oids:
        end = output.find(b"\n", pos)
        header = output[pos:end].decode("ascii", "replace").split(" ") if end >= 0 else []
        if len(header) != 3 or header[0] != oid or header[1] != "blob" or not header[2].isdigit():
            shown = shown_value(" ".join(header) or "nothing")
            raise GitError(f"git cat-file --batch: expected blob {oid}, got {shown}")
        start = end + 1
        size = int(header[2])
        if output[start + size : start + size + 1] != b"\n":
            raise GitError(f"git cat-file --batch: blob {oid} is cut short")
        blobs[oid] = output[start : start + size]
        pos = start + size + 1
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
    """Run ``args`` in ``cwd`` with ``env``; never raises on exit status.

    A command that cannot be started exits 127, as in a shell.
    """
    try:
        p = subprocess.run(
            list(args), cwd=cwd, env=dict(env), input=stdin, capture_output=True, check=False
        )
    except OSError as exc:
        return Completed(127, b"", str(exc))
    return Completed(p.returncode, p.stdout, p.stderr.decode("utf-8", "replace"))


# Variables that would point git at another repository than the one ``cwd``
# holds: the scratch repository and the checkout are found by ``cwd`` alone.
REPOSITORY_VARIABLES = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
)


def git_environment(base: Mapping[str, str], protocols: Sequence[str]) -> dict[str, str]:
    """``base`` with what every git command here runs under.

    No prompt for credentials; messages in English; only ``protocols`` as
    transports, also for anything git starts itself; pathspecs taken
    literally, without glob or magic; no inherited repository location.
    The job's credential helper (``GIT_CONFIG_*``) is kept.
    """
    env = {k: v for k, v in base.items() if k not in REPOSITORY_VARIABLES}
    env.update(
        GIT_TERMINAL_PROMPT="0",
        LC_ALL="C",
        GIT_ALLOW_PROTOCOL=":".join(protocols),
        GIT_LITERAL_PATHSPECS="1",
    )
    return env


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

    ``cwd`` is the checkout of the current build. ``protocols`` are the
    transports a URL may use (``https`` in a build; tests use ``file``).
    Every command that talks to a remote carries ``PROACTIVE_AUTH``. Another
    repository's commit is read in a scratch repository of its own, removed
    when the read is done, so nothing is kept between calls. ``run`` executes
    one command; tests may replace it. ``log`` receives the merge-base times
    found, which the choice of a release line rests on.
    """

    def __init__(
        self,
        cwd: str = ".",
        protocols: Sequence[str] = ("https",),
        run: Runner = run_command,
        log: Callable[[str], None] = _ignore,
    ) -> None:
        self._cwd = cwd
        self._protocols = tuple(protocols)
        self._run = run
        self._log = log

    def _git(
        self, args: Sequence[str], cwd: str | None = None, stdin: bytes | None = None
    ) -> Completed:
        env = git_environment(os.environ, self._protocols)
        return self._run(["git", *args], cwd or self._cwd, stdin, env)

    def _ok(self, args: Sequence[str], cwd: str | None = None, stdin: bytes | None = None) -> bytes:
        c = self._git(args, cwd, stdin)
        if c.returncode != 0:
            shown = " ".join(["git", *args])
            raise GitError(f"{shown} failed ({c.returncode}): {c.stderr.strip()}")
        return c.stdout

    def _remote(self, args: Sequence[str], cwd: str | None = None) -> bytes:
        """``_ok`` of a command that talks to a remote, with ``PROACTIVE_AUTH``."""
        return self._ok([*PROACTIVE_AUTH, *args], cwd)

    def _url(self, url: str) -> str:
        return check_url(url, self._protocols)

    def commits_of(self, url: str, refs: Sequence[str]) -> dict[str, str]:
        """``{ref: commit}`` of those of the full refs ``refs`` that ``url`` has, in one call.

        A tag is also asked for peeled (``<ref>^{}``), so an annotated tag
        gives the commit it names, not the tag object.
        """
        asked = [check_full_ref(r) for r in dict.fromkeys(refs)]
        tags = [r + PEELED for r in asked if r.startswith(TAG_PREFIX)]
        out = self._remote(["ls-remote", "--", self._url(url), *asked, *tags])
        return parse_ls_remote(out.decode("utf-8", "replace"), asked)

    def branches(self, url: str) -> dict[str, str]:
        """``{branch: commit}`` of every branch of ``url`` (``git ls-remote --heads``)."""
        out = self._remote(["ls-remote", "--heads", "--", self._url(url)])
        return parse_branches(out.decode("utf-8", "replace"))

    def read_files(
        self, url: str, commit: str, directory: str, wanted: Callable[[str], bool]
    ) -> dict[str, bytes]:
        """The regular files under ``directory`` at ``commit`` of ``url`` that ``wanted`` accepts.

        Experiment 33's recipe r4p, in a new scratch repository: one blobless
        fetch of the commit, one listing of ``directory``, one fetch of the
        blobs of the files that count, and ``git cat-file --batch``. Two
        fetches whatever the number of files; none for blobs when no file
        counts.
        """
        self._url(url)
        check_object_id(commit)
        check_tree_path(directory)
        with tempfile.TemporaryDirectory(prefix="snapshot-git-") as d:
            self._ok(["init", "-q"], d)
            self._remote(fetch_commit_arguments(url, commit), d)
            files = self._listed("FETCH_HEAD", directory, wanted, d)
            if not files:
                return {}
            self._remote(fetch_objects_arguments(url, list(dict.fromkeys(files.values()))), d)
            return self._contents(files, d)

    def checkout_files(self, directory: str, wanted: Callable[[str], bool]) -> dict[str, bytes]:
        """The regular files under ``directory`` at HEAD of ``cwd`` that ``wanted`` accepts.

        Read from the tree of the commit, never from the working tree, so a
        file a job wrote or changed there (the generated pipeline) is not
        what is read. Local: no command talks to a remote.
        """
        check_tree_path(directory)
        files = self._listed("HEAD", directory, wanted)
        return self._contents(files) if files else {}

    def _listed(
        self, tree: str, directory: str, wanted: Callable[[str], bool], cwd: str | None = None
    ) -> dict[str, str]:
        """``{path: blob id}`` of the regular files under ``directory`` of ``tree`` that count."""
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

    def _is_shallow(self) -> bool:
        out = self._ok(["rev-parse", "--is-shallow-repository"])
        return out.decode("ascii", "replace").strip() == "true"

    def _merge_base_time(self, commit: str) -> int | None:
        """Committer time of the merge-base of HEAD and ``commit``; None when there is none."""
        base = self._git(["merge-base", "--end-of-options", "HEAD", commit])
        if base.returncode == MERGE_BASE_NONE:
            return None
        if base.returncode != 0:
            raise GitError(f"git merge-base HEAD {commit} failed: {base.stderr.strip()}")
        sha = check_object_id(base.stdout.decode("ascii", "replace").strip())
        shown = self._ok(["show", "-s", "--format=%ct", "--end-of-options", sha])
        return int(shown.decode("ascii", "replace").strip())

    def merge_base_times(self, url: str, commits: Sequence[str]) -> dict[str, int | None]:
        """For each of ``commits`` of ``url``: the committer time of its merge-base with HEAD.

        None where there is none. The commits are fetched into the checkout in
        one fetch, objects only (no ref, no FETCH_HEAD). A shallow checkout
        has not the history a merge-base needs: nothing is fetched and every
        answer is None.
        """
        self._url(url)
        wanted = [check_object_id(c) for c in dict.fromkeys(commits)]
        if self._is_shallow():
            self._log("the checkout is shallow: no merge-base is known")
            return {c: None for c in wanted}
        if wanted:
            self._remote(fetch_objects_arguments(url, wanted))
        times = {c: self._merge_base_time(c) for c in wanted}
        shown = ", ".join(f"{c[:12]} {'none' if t is None else t}" for c, t in times.items())
        self._log(f"merge-base times of HEAD with {shown_url(url)}: {shown or 'none asked'}")
        return times
