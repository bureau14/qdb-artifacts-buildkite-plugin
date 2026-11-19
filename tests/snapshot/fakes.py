"""Fakes behind the interfaces of the planning half, in memory: no network, no subprocess.

- ``MemoryStore``: ``objects.Store`` (get, put, create once, list in key order).
- ``Hosting``: repositories as commits of files. ``FakeGit`` asks it what
  ``git.Git`` asks git; ``FakeCheckout`` is one commit of it, as ``git.Checkout``.
- ``BuildAgent``: ``agent.Agent`` of one build, over ``AgentState``, the
  meta-data, step outcomes, uploads and annotations of every build.
- ``FakePipelines``: ``planner.Pipelines``, Buildkite's facts by slug.

Each refuses what the real one refuses, through the check functions of the
real module.
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import itertools
from collections.abc import Callable, Mapping, Sequence

from lib.objects import StoredObject
from lib.snapshot.agent import (
    MetaDataAbsent,
    build_option,
    check_annotation_style,
    check_meta_data_value,
)
from lib.snapshot.buildkite import PipelineFacts, PipelineNotFound
from lib.snapshot.git import (
    GitError,
    branch_of,
    branch_ref,
    check_full_ref,
    check_object_id,
    check_tree_path,
    check_url,
)


class MemoryStore:
    """The bucket as ``objects.Store`` sees it: None for an absent object, False for a
    create that finds one."""

    bucket = "example-artifacts"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def get(self, key: str) -> StoredObject | None:
        body = self.objects.get(key)
        return None if body is None else StoredObject(body, None)

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self.objects[key] = body

    def create(self, key: str, body: bytes, content_type: str) -> bool:
        if key in self.objects:
            return False
        self.objects[key] = body
        return True

    def list(self, prefix: str, limit: int | None = None) -> list[tuple[str, int]]:
        found = sorted((k, len(b)) for k, b in self.objects.items() if k.startswith(prefix))
        return found if limit is None else found[:limit]


def url(project: str) -> str:
    return f"https://git.example.test/example/{project}.git"


def as_bytes(content: str | bytes) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


class Hosting:
    """Every project's repository at ``url(project)``: its branches and the files of each commit."""

    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str] = {}
        self.trees: dict[str, dict[str, bytes]] = {}
        self.count = itertools.count(1)

    def commit(self, project: str, files: Mapping[str, str | bytes], branch: str = "master") -> str:
        """A new commit of ``files`` at the head of ``branch``; its id."""
        commit = hashlib.sha1(f"{project} {branch} {next(self.count)}".encode()).hexdigest()
        self.trees[commit] = {path: as_bytes(c) for path, c in files.items()}
        self.refs[(url(project), branch_ref(branch))] = commit
        return commit

    def head(self, project: str, branch: str = "master") -> str:
        return self.refs[(url(project), branch_ref(branch))]


def files_under(
    tree: Mapping[str, bytes], directory: str, wanted: Callable[[str], bool]
) -> dict[str, bytes]:
    return {p: c for p, c in tree.items() if p.startswith(directory + "/") and wanted(p)}


@dataclasses.dataclass(frozen=True)
class FakeGit:
    """``git.Git`` over the hosting. No merge-base is known, so a root's release line is
    ``master`` unless its branch is one."""

    hosting: Hosting

    def commits_of(self, repository: str, refs: Sequence[str]) -> dict[str, str]:
        check_url(repository, ("https",))
        asked = [check_full_ref(r) for r in refs]
        return {
            r: self.hosting.refs[(repository, r)]
            for r in asked
            if (repository, r) in self.hosting.refs
        }

    def read_files(
        self, repository: str, commit: str, directory: str, wanted: Callable[[str], bool]
    ) -> dict[str, bytes]:
        check_url(repository, ("https",))
        check_tree_path(directory)
        if check_object_id(commit) not in self.hosting.trees:
            raise GitError(f"{repository} has no commit {commit}")
        return files_under(self.hosting.trees[commit], directory, wanted)

    def branches(self, repository: str) -> dict[str, str]:
        check_url(repository, ("https",))
        return {branch_of(r): c for (u, r), c in self.hosting.refs.items() if u == repository}

    def merge_base_times(self, repository: str, commits: Sequence[str]) -> dict[str, None]:
        return {check_object_id(c): None for c in commits}


@dataclasses.dataclass(frozen=True)
class FakeCheckout:
    """``git.Checkout`` of one commit of the hosting."""

    hosting: Hosting
    commit: str

    def head_commit(self) -> str:
        return self.commit

    def checkout_files(self, directory: str, wanted: Callable[[str], bool]) -> dict[str, bytes]:
        return files_under(self.hosting.trees[self.commit], check_tree_path(directory), wanted)


@dataclasses.dataclass
class AgentState:
    """What the agent keeps of every build, by build id."""

    meta_data: dict = dataclasses.field(default_factory=lambda: collections.defaultdict(dict))
    outcomes: dict = dataclasses.field(default_factory=lambda: collections.defaultdict(dict))
    uploads: dict = dataclasses.field(default_factory=lambda: collections.defaultdict(list))
    annotations: dict = dataclasses.field(default_factory=lambda: collections.defaultdict(dict))


@dataclasses.dataclass(frozen=True)
class BuildAgent:
    """``agent.Agent`` of the job of one build."""

    state: AgentState
    build: str

    def meta_data_get(self, key: str, build: str | None = None) -> str:
        build_option(build)
        found = self.state.meta_data[build or self.build]
        if key not in found:
            raise MetaDataAbsent(f"meta-data {key} is not set")
        return found[key]

    def meta_data_set(self, key: str, value: str) -> None:
        check_meta_data_value(key, value)
        self.state.meta_data[self.build][key] = value

    def step_outcome(self, key: str) -> str:
        return self.state.outcomes[self.build].get(key, "")

    def pipeline_upload(self, document: str) -> None:
        self.state.uploads[self.build].append(document)

    def annotate(self, body: str, style: str, context: str) -> None:
        check_annotation_style(style)
        self.state.annotations[self.build][context] = (style, body)


@dataclasses.dataclass(frozen=True)
class FakePipelines:
    """``planner.Pipelines``: the facts of a slug, an unknown slug Buildkite's 404."""

    facts: Mapping[str, PipelineFacts]

    def __call__(self, slug: str) -> PipelineFacts:
        if slug not in self.facts:
            raise PipelineNotFound(f"Buildkite has no pipeline {slug}")
        return self.facts[slug]
