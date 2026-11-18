"""In-memory stand-ins for the interfaces of the planning half, for tests.

No network, no subprocess. Each fake refuses what the real one refuses, through
the same check functions of the real module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Callable, Union

from lib.snapshot.agent import (
    AgentError,
    MetaDataAbsent,
    build_option,
    check_annotation_style,
    check_meta_data_value,
)
from lib.snapshot.git import (
    GitError,
    branch_of,
    check_full_ref,
    check_object_id,
    check_tree_path,
    check_url,
    tree_path,
)


class RecordingAgent:
    """``Agent`` in memory that records what a job did to its build.

    ``meta_data``: this build's meta-data, read and written. ``builds``: the
    meta-data of other builds by build id, read only. ``outcomes``: step key ->
    outcome; a step that is not listed has the empty outcome (not finished).
    ``uploads`` and ``annotations`` (``(body, style, context)``) are appended
    in call order.
    """

    def __init__(
        self,
        meta_data: Mapping[str, str] | None = None,
        builds: Mapping[str, Mapping[str, str]] | None = None,
        outcomes: Mapping[str, str] | None = None,
    ) -> None:
        self.meta_data = dict(meta_data or {})
        self.builds = {b: dict(m) for b, m in (builds or {}).items()}
        self.outcomes = dict(outcomes or {})
        self.uploads: list[str] = []
        self.annotations: list[tuple[str, str, str]] = []

    def _data(self, build: str | None) -> Mapping[str, str]:
        build_option(build)
        if build is None:
            return self.meta_data
        if build not in self.builds:
            raise AgentError(f"fake agent: no build {build}")
        return self.builds[build]

    def meta_data_exists(self, key: str, build: str | None = None) -> bool:
        return key in self._data(build)

    def meta_data_get(self, key: str, build: str | None = None) -> str:
        data = self._data(build)
        if key not in data:
            raise MetaDataAbsent(f"meta-data {key} is not set")
        return data[key]

    def meta_data_set(self, key: str, value: str) -> None:
        check_meta_data_value(key, value)
        self.meta_data[key] = value

    def step_outcome(self, key: str) -> str:
        return self.outcomes.get(key, "")

    def pipeline_upload(self, document: str) -> None:
        self.uploads.append(document)

    def annotate(self, body: str, style: str, context: str) -> None:
        check_annotation_style(style)
        self.annotations.append((body, style, context))


Content = Union[str, bytes]


def _as_bytes(content: Content) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


def _files_under(
    tree: Mapping[str, Content], directory: str, wanted: Callable[[str], bool]
) -> dict[str, bytes]:
    under = directory + "/"
    paths = [tree_path(p.encode("utf-8", "surrogateescape")) for p in tree if p.startswith(under)]
    return {p: _as_bytes(tree[p]) for p in paths if wanted(p)}


class FakeGit:
    """``Git`` from dictionaries.

    ``refs``: ``(url, full ref) -> commit``, a tag already peeled.
    ``trees``: ``(url, commit) -> {path: content}``, content as text or
    bytes; a commit without a tree is unknown and ``read_files`` raises
    ``GitError``, as the fetch of a missing commit does. ``times``: commit ->
    committer time of its merge-base with the checkout; a commit not listed
    has none.

    ``calls`` records every call as ``(method, url, ref or commit or commits)``.
    """

    def __init__(
        self,
        refs: Mapping[tuple[str, str], str],
        trees: Mapping[tuple[str, str], Mapping[str, Content]] | None = None,
        times: Mapping[str, int] | None = None,
    ) -> None:
        self.refs = dict(refs)
        self.trees = {k: dict(v) for k, v in (trees or {}).items()}
        self.times = dict(times or {})
        self.calls: list[tuple[Any, ...]] = []

    def commits_of(self, url: str, refs: Sequence[str]) -> dict[str, str]:
        check_url(url, ("https",))
        asked = tuple(check_full_ref(r) for r in dict.fromkeys(refs))
        self.calls.append(("commits_of", url, asked))
        return {r: self.refs[(url, r)] for r in asked if (url, r) in self.refs}

    def read_files(
        self, url: str, commit: str, directory: str, wanted: Callable[[str], bool]
    ) -> dict[str, bytes]:
        check_url(url, ("https",))
        check_object_id(commit)
        check_tree_path(directory)
        self.calls.append(("read_files", url, commit))
        if (url, commit) not in self.trees:
            raise GitError(f"fake git: {url} has no commit {commit}")
        return _files_under(self.trees[(url, commit)], directory, wanted)

    def branches(self, url: str) -> dict[str, str]:
        check_url(url, ("https",))
        self.calls.append(("branches", url))
        return {branch_of(r): c for (u, r), c in self.refs.items() if u == url and branch_of(r)}

    def merge_base_times(self, url: str, commits: Sequence[str]) -> dict[str, int | None]:
        check_url(url, ("https",))
        for c in commits:
            check_object_id(c)
        self.calls.append(("merge_base_times", url, tuple(commits)))
        return {c: self.times.get(c) for c in commits}


class FakeCheckout:
    """``Checkout`` from a commit and its tree (``{path: content}``)."""

    def __init__(self, commit: str = "0" * 40, tree: Mapping[str, Content] | None = None) -> None:
        self.commit = check_object_id(commit)
        self.tree = dict(tree or {})

    def head_commit(self) -> str:
        return self.commit

    def checkout_files(self, directory: str, wanted: Callable[[str], bool]) -> dict[str, bytes]:
        check_tree_path(directory)
        return _files_under(self.tree, directory, wanted)
