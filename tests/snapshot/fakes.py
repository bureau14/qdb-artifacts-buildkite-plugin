"""Fakes behind the interfaces of the planning half, in memory: no network, no subprocess.

- ``MemoryStore``: ``objects.Store`` (get, put, create once, list one level in key
  order), each object with the time it was last written, one millisecond after
  the write before.
- ``Hosting``: repositories as commits of files, branches and tags. ``FakeGit`` asks
  it what ``git.Git`` asks git; ``FakeCheckout`` is one commit of it, as
  ``git.Checkout``.
- ``BuildAgent``: ``agent.Agent`` of one build, over ``AgentState``, the
  meta-data, step outcomes, uploads and annotations of every build.
- ``FakePipelines``: ``planner.Pipelines``, Buildkite's facts by slug.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import hashlib
import itertools
from collections.abc import Callable, Mapping, Sequence

from lib.objects import Listed
from lib.snapshot.agent import MetaDataAbsent
from lib.snapshot.buildkite import PipelineFacts, PipelineNotFound
from lib.snapshot.git import branch_of, branch_ref, tag_ref

EPOCH = datetime.datetime(2026, 11, 20, tzinfo=datetime.timezone.utc)


class MemoryStore:
    """The bucket as ``objects.Store`` sees it: None for an absent object, False for a
    create that finds one."""

    bucket = "example-artifacts"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.times: dict[str, datetime.datetime] = {}
        self.writes = itertools.count(1)

    def write(self, key: str, body: bytes) -> None:
        self.objects[key] = body
        self.times[key] = EPOCH + datetime.timedelta(milliseconds=next(self.writes))

    def get(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def put(self, key: str, body: bytes, content_type: str) -> None:
        self.write(key, body)

    def create(self, key: str, body: bytes, content_type: str) -> bool:
        if key in self.objects:
            return False
        self.write(key, body)
        return True

    def list(self, prefix: str) -> list[Listed]:
        below = (k for k in self.objects if k.startswith(prefix) and "/" not in k[len(prefix) :])
        return [Listed(k, self.times[k]) for k in sorted(below)]


def url(project: str) -> str:
    return f"https://git.example.test/example/{project}.git"


def as_bytes(content: str | bytes) -> bytes:
    return content.encode("utf-8") if isinstance(content, str) else content


class Hosting:
    """Every project's repository at ``url(project)``: its branches, its tags and the files of
    each commit."""

    def __init__(self) -> None:
        self.refs: dict[tuple[str, str], str] = {}
        self.trees: dict[str, dict[str, bytes]] = {}
        self.count = itertools.count(1)

    def commit(
        self, project: str, files: Mapping[str, str | bytes], branch: str | None = "master"
    ) -> str:
        """A new commit of ``files`` at the head of ``branch``, or on no branch; its id."""
        commit = hashlib.sha1(f"{project} {branch} {next(self.count)}".encode()).hexdigest()
        self.trees[commit] = {path: as_bytes(c) for path, c in files.items()}
        if branch is not None:
            self.refs[(url(project), branch_ref(branch))] = commit
        return commit

    def head(self, project: str, branch: str = "master") -> str:
        return self.refs[(url(project), branch_ref(branch))]

    def cut(self, project: str, branch: str, start: str = "master") -> None:
        """A new branch ``branch`` at the head of ``start``."""
        self.refs[(url(project), branch_ref(branch))] = self.head(project, start)

    def tag(self, project: str, tag: str, commit: str) -> None:
        """The tag ``tag`` at ``commit``, put there anew when it was elsewhere."""
        self.refs[(url(project), tag_ref(tag))] = commit

    def checked_out(self, project: str, name: str) -> str:
        """The commit a build under ``name`` checks out: its branch's head, else its tag's."""
        found = self.refs.get((url(project), branch_ref(name)))
        return found or self.refs[(url(project), tag_ref(name))]


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
        known = self.hosting.refs
        return {r: known[(repository, r)] for r in refs if (repository, r) in known}

    def read_files(
        self, repository: str, commit: str, directory: str, wanted: Callable[[str], bool]
    ) -> dict[str, bytes]:
        return files_under(self.hosting.trees[commit], directory, wanted)

    def branches(self, repository: str) -> dict[str, str]:
        found = ((branch_of(r), c) for (u, r), c in self.hosting.refs.items() if u == repository)
        return {b: c for b, c in found if b is not None}

    def merge_base_times(self, repository: str, commits: Sequence[str]) -> dict[str, None]:
        return {c: None for c in commits}


@dataclasses.dataclass(frozen=True)
class FakeCheckout:
    """``git.Checkout`` of one commit of the hosting."""

    hosting: Hosting
    commit: str

    def head_commit(self) -> str:
        return self.commit

    def checkout_files(self, directory: str, wanted: Callable[[str], bool]) -> dict[str, bytes]:
        return files_under(self.hosting.trees[self.commit], directory, wanted)


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
        found = self.state.meta_data[build or self.build]
        if key not in found:
            raise MetaDataAbsent(f"meta-data {key} is not set")
        return found[key]

    def meta_data_set(self, key: str, value: str) -> None:
        self.state.meta_data[self.build][key] = value

    def step_outcome(self, key: str) -> str:
        return self.state.outcomes[self.build].get(key, "")

    def pipeline_upload(self, document: str) -> None:
        self.state.uploads[self.build].append(document)

    def annotate(self, body: str, style: str, context: str) -> None:
        self.state.annotations[self.build][context] = (style, body)


@dataclasses.dataclass(frozen=True)
class FakePipelines:
    """``planner.Pipelines``: the facts of a slug, an unknown slug Buildkite's 404."""

    facts: Mapping[str, PipelineFacts]

    def __call__(self, slug: str) -> PipelineFacts:
        if slug not in self.facts:
            raise PipelineNotFound(f"Buildkite has no pipeline {slug}")
        return self.facts[slug]
