"""Where a project of a plan is taken from, and whether it can be part of a snapshot there.

Git and Buildkite are asked each question once (``Ask``). A project that cannot be planned
where it is taken from is ``Refused``, which ``resolve`` returns as a value: a pin found
later can still move it.
"""

from __future__ import annotations

import functools
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass

from ..forms import branch_ref, is_commit, is_tag, name_of, tag_ref
from .buildkite import PipelineFacts, PipelineNotFound
from .lines import release_line
from .plan import PlanError
from .templates import Declaration, is_template_path, latest_dependencies, read_declaration

PIPELINE_PY = ".buildkite/pipeline.py"
# What a pipeline.py that hands its pipeline to the runner contains.
RUNNER_SHAPE = (b"qdb_pipeline", b"run(generate_pipeline)")
LAST_RESORTS = ("master", "main")


@dataclass(frozen=True)
class Place:
    """Where a project is taken from."""

    repository: str
    ref: str
    commit: str


@dataclass(frozen=True)
class Ask:
    """``pipeline(slug)``: Buildkite's facts. ``commit(repository, ref)``: the ref's commit,
    None when the repository lacks it. ``reaches(repository, tip, commit)``: whether ``commit``
    is ``tip`` or before it. ``files(repository, commit)``: its templates and pipeline.py.
    ``line()``: the root build's release line. ``line_of(repository, branch, commit)``: the
    release line of another branch, whose tip ``commit`` is."""

    pipeline: Callable[[str], PipelineFacts]
    commit: Callable[[str, str], str | None]
    reaches: Callable[[str, str, str], bool]
    files: Callable[[str, str], Mapping[str, bytes]]
    line: Callable[[], str]
    line_of: Callable[[str, str, str], str]


def is_read(path: str) -> bool:
    """A file the plan reads of a repository: a template, or its pipeline.py."""
    return is_template_path(path) or path == PIPELINE_PY


def asking(
    git,
    pipeline: Callable[[str], PipelineFacts],
    at: Place,
    name: str,
    named: Mapping[str, str | None],
) -> Ask:
    """``Ask`` for the plan of a root build that runs under ``name`` and is taken from ``at``.
    ``named`` is what git said of the refs of its name in its repository. Its own ref is known
    at its own commit, so another pipeline of its repository is planned there, also when the
    ref moved."""
    known = {**named, at.ref: at.commit}

    @functools.cache
    def commit(url: str, ref: str) -> str | None:
        if url == at.repository and ref in known:
            return known[ref]
        return git.commits_of(url, [ref]).get(ref)

    @functools.cache
    def files(url: str, commit: str) -> Mapping[str, bytes]:
        return git.read_files(url, commit, ".buildkite", is_read)

    line = functools.cache(lambda: release_line(git, at.repository, name))
    line_of = functools.cache(lambda url, branch, tip: release_line(git, url, branch, tip))
    return Ask(pipeline, commit, functools.cache(git.reaches), files, line, line_of)


@dataclass(frozen=True)
class Member:
    """A project of the tree: its place, and what its templates declare there."""

    place: Place
    declaration: Declaration


class Refused(Exception):
    """Why a project the tree needs cannot be planned where it is taken from."""


@dataclass(frozen=True)
class Pin:
    """A pin as found: the project that writes it, at which commit, in which template."""

    project: str
    ref: str
    by: str
    commit: str
    path: str

    def __str__(self) -> str:
        return f"{self.ref} (in {self.path} of {self.by} at {self.commit})"


@dataclass(frozen=True)
class Dependent:
    """Who needs a project: the project whose template ``path`` downloads it."""

    project: str
    path: str

    def __str__(self) -> str:
        return f"It is a snapshot dependency of {self.project} ({self.path})."


def default_refs(at: str, line: Callable[[], str | None]) -> Iterator[str]:
    """The refs a dependency without a pin is taken from under a build at the ref ``at``, in
    order. At a tag, that tag alone: a release takes nothing from another ref. On a branch:
    that branch, its release line, ``master`` and ``main``; that branch alone when ``line()``
    is None. The line is asked for only when the branch is not enough."""
    yield at
    known = None if is_tag(at) else line()
    if known is not None:
        yield from (r for r in dict.fromkeys(map(branch_ref, (known, *LAST_RESORTS))) if r != at)


def first_ref(ask: Ask, repository: str, refs: Iterable[str]) -> tuple[str, str] | None:
    """``(ref, commit)`` of the first of ``refs`` that ``repository`` has."""
    found = ((r, ask.commit(repository, r)) for r in refs)
    return next(((r, c) for r, c in found if c is not None), None)


def refs_named(given: str) -> tuple[str, ...]:
    """The refs a name a person gave may be, in the order they count: a full ref is itself,
    another name a branch, then a tag."""
    return (given,) if given.startswith("refs/") else (branch_ref(given), tag_ref(given))


def given_place(project: str, repository: str, given: str, at: str, ask: Ask) -> Place:
    """Where ``project`` is taken from when a person gave ``given`` for it under a root build
    at the ref ``at``: a branch, a tag or a full ref of its repository, or a commit written in
    full. A commit is taken only from the ref the project would come from without it, so the
    build started of it runs under that ref's name and git is asked for one history."""
    if not is_commit(given):
        found = first_ref(ask, repository, refs_named(given))
        if found is None:
            raise Refused(
                f"{project} cannot be planned: SNAPSHOT_REFS gives it {given}, which is neither "
                f"a branch nor a tag of {repository}. A commit is written in full, as its 40 "
                "characters."
            )
        return Place(repository, *found)
    found = first_ref(ask, repository, default_refs(at, ask.line))
    if found is None or not ask.reaches(repository, found[1], given):
        tried = "none of " + ", ".join(default_refs(at, ask.line)) if found is None else found[0]
        raise Refused(
            f"{project} cannot be planned: SNAPSHOT_REFS gives it the commit {given}, which is "
            f"not in the history of the ref {project} is taken from without it ({repository} "
            f"has {tried}). Way out: give the branch or tag that holds the commit."
        )
    return Place(repository, found[0], given)


def place_of(
    project: str,
    repository: str,
    pin: Pin | None,
    given: str | None,
    at: str,
    dependent: Dependent,
    ask: Ask,
) -> Place:
    """Where ``project`` is taken from under a root build at the ref ``at``: where a person
    gave it, else at its pin, else at the first of ``default_refs`` its repository has."""
    if given is not None:
        return given_place(project, repository, given, at, ask)
    if pin is not None:
        commit = ask.commit(repository, pin.ref)
        if commit is None:
            raise Refused(
                f"{project} cannot be planned: its pin {pin} is not a ref of {repository}."
            )
        return Place(repository, pin.ref, commit)
    found = first_ref(ask, repository, default_refs(at, ask.line))
    if found is not None:
        return Place(repository, *found)
    if is_tag(at):
        tag = name_of(at)
        raise Refused(
            f"{project} cannot be planned: {repository} has no tag {tag}, and a build at a tag "
            f"takes every dependency from the same tag. {dependent} Way out: push the tag {tag} "
            f"to {repository}."
        )
    tried = ", ".join(default_refs(at, ask.line))
    raise Refused(f"{project} cannot be planned: {repository} has none of {tried}. {dependent}")


def snapshot_declaration(
    project: str, at: Place, dependent: Dependent, facts: PipelineFacts, ask: Ask
) -> Declaration:
    """The declaration of ``project`` at ``at``, when it can be part of a snapshot: Buildkite's
    configuration runs pipeline.py (else its repository is not read), that file hands its
    pipeline to the runner, and its templates take no latest successful build. The first
    condition that fails raises ``Refused``."""

    def unmet(failed: str, met: str) -> Refused:
        """What ``failed``, and the two ways out: the project comes to meet the condition, as
        ``met`` says, or its dependent takes its latest successful build."""
        return Refused(
            f"{project} cannot give a snapshot at {at.ref} {at.commit} of {at.repository}: "
            f"{failed}. {dependent} Way out: {met}; or {dependent.project} "
            f"takes {project} as a latest successful dependency, with latest: true in its entry "
            f"of {project} in {dependent.path}."
        )

    if not facts.runs_pipeline_py:
        raise unmet(
            f"Buildkite's configuration of {project} does not run {PIPELINE_PY} into a pipeline "
            "upload",
            f"Buildkite's configuration of {project} runs {PIPELINE_PY} | buildkite-agent "
            "pipeline upload",
        )
    files = ask.files(at.repository, at.commit)
    source = files.get(PIPELINE_PY)
    if source is None or not all(s in source for s in RUNNER_SHAPE):
        shape = "qdb_pipeline and run(generate_pipeline)"
        raise unmet(
            f"{PIPELINE_PY} {'is not there' if source is None else f'does not contain {shape}'}",
            f"on {at.ref} the {PIPELINE_PY} of {project} imports run from qdb_pipeline and ends "
            'with if __name__ == "__main__": run(generate_pipeline)',
        )
    declaration = read_declaration(files, project)
    latest = latest_dependencies(declaration)
    if latest:
        e = latest[0]
        raise unmet(
            "its declaration holds a latest successful dependency, which a snapshot never "
            f"contains: {e.path} downloads {e.project} with latest: true",
            f"{project} takes {e.project} as a snapshot dependency on {at.ref} (no latest: true "
            f"in its entry of {e.project} in {e.path})",
        )
    return declaration


def resolve(
    project: str, dependent: Dependent, pin: Pin | None, given: str | None, at: str, ask: Ask
) -> Member | Refused:
    """``project`` where it is taken from under a root build at the ref ``at``, or why it
    cannot be planned there. ``given`` is what a person gave for it, which counts before its
    pin. A pipeline Buildkite does not have fails the plan."""
    try:
        facts = ask.pipeline(project)
    except PipelineNotFound as exc:
        raise PlanError(f"{exc}. {dependent}") from exc
    try:
        place = place_of(project, facts.repository, pin, given, at, dependent, ask)
        return Member(place, snapshot_declaration(project, place, dependent, facts, ask))
    except Refused as refused:
        return refused
