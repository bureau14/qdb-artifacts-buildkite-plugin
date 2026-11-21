"""The plan of a root build.

``make_plan(root, own, latest, git, pipelines)`` fixes, at one instant, the
commit every project of the root's snapshot tree is built from, its key, and
whether a build of it writes its branch's latest files, and returns the plan
(``plan.Plan``). It is a function of its inputs: the root's facts
(``Root``), with what git said of the refs of its name, the declaration read
from the root's own commit, the root's latest successful downloads with the
keys its plan step read for them, and two interfaces it asks and never writes
to:

- ``git.Git``: ``commits_of`` (the commits of full refs), ``read_files`` (the
  files of another repository's commit), and ``branches`` and
  ``merge_base_times`` for the root's release line (``lines.release_line``);
- ``Pipelines``: Buildkite's facts of a pipeline slug
  (``buildkite.PipelineFacts``: its repository, and whether its configuration
  runs ``.buildkite/pipeline.py``).

Each question is asked once: a commit per repository and ref (the refs a
dependency without a pin may be taken from in one ``ls-remote``, none after a
ref known to exist), a read per repository and commit, a Buildkite call per
slug. Whether a build writes its branch's latest files asks nothing more: it
is decided from what git told the plan.

The tree is the root and, recursively, the snapshot dependencies of every
node. Latest successful dependencies are not in it: they are not planned and
not asked about. The walk is breadth first from the root; each project is
resolved once, for the first consumer that reaches it (consumers in the order
reached, their dependencies sorted):

1. The root, at its own commit, with its own declaration. Its repository is
   Buildkite's answer for its slug. Its ref is the branch of its name when its
   repository has one, else the tag of its name: a root at a tag; a name that
   is neither is planned as the branch it names (``root_ref``). The release
   line of a root on a branch (``lines.release_line`` of that repository and
   its branch) is found the first time a dependency without a pin is placed,
   and only then; that of a root at a tag never. Its declaration may hold
   latest successful dependencies: the keys read for them are part of its key.
2. For each snapshot dependency of a node, in this order:

   a. Buildkite's facts of the project, for its repository; a 404 fails the
      plan;
   b. its ref: its pin; else, under a root at a tag, that tag and nothing
      else; else the root's branch when its repository has it; else the
      root's release line when its repository has it; else ``master``; else
      ``main``. A pin names ``refs/heads/<branch>`` or ``refs/tags/<tag>``:
      any other text is a ref no repository has;
   c. its commit, ``git.commits_of``, a tag peeled; the root's repository and
      ref give the root's own commit. A ref that does not exist refuses the
      project;
   d. the three conditions of a snapshot dependency, the first that fails
      refusing the project with its own message (``first_unmet``):
      Buildkite's configuration runs ``.buildkite/pipeline.py``; that file at
      the commit has the runner's shape; its declaration holds no latest
      successful dependency. A template that does not parse fails the plan,
      naming the project and the file (``declaration.TemplateError``);
   e. a project that meets all three is a node, with its declaration, and so
      with its own snapshot dependencies and pins.

3. Pins come from every node of the tree. Two different pins of one project
   fail the plan and name where each was found. The pins decide where their
   projects are taken from, which decides
   which commits are read and so which pins there are: the walk is made again
   with the pins it found until they are the pins it was made with. Pins that
   never settle fail the plan.
4. A refusal left in the settled walk fails the plan, which names every
   refused project, one per line. A refusal is a value until then, so a pin
   found late can still move a project to a commit that gives a snapshot.
5. A cycle fails the plan and is named. Then, dependencies first:

   - whether each build writes its branch's latest files
     (``writes_latest_file``). Every build runs under the name of its ref, a
     branch's or a tag's. The first build writes them. A build started for a
     dependency writes them when its own build, the one a person would start
     on its branch at its commit, takes every project below it at the commit
     this plan takes it at. The plan step plans that own build with the walk
     above, from what git told this plan (``Told``), and counts what git did
     not tell it as no. Its release line is assumed: its branch when that is
     a line; on the first build's branch, the first build's line when its
     repository has a branch of that name; else none, and only its own branch
     counts;
   - the keys, from the plan itself (``plan.keyed``): what a node's key stands
     for is its commit and the keys of its snapshot dependencies, for the root
     also the keys read for its latest successful downloads, with the plan id
     as its build when ``SNAPSHOT_REBUILD`` forces the node (the root under
     ``one``, every node under ``all``).
"""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Union

from ..forms import branch_of, branch_ref, is_tag, name_of, tag_ref
from .buildkite import PipelineFacts, PipelineNotFound
from .declaration import (
    BUILDKITE_DIR,
    LATEST_SUCCESSFUL,
    SNAPSHOT,
    Declaration,
    Entry,
    declared_entries,
    is_template_path,
    read_declaration,
)
from .git import Git
from .lines import is_release_line, release_line
from .plan import (
    REBUILD_ALL,
    REBUILD_NONE,
    REBUILD_ONE,
    First,
    Latest,
    Plan,
    PlanError,
    Planned,
    keyed,
)
from .shape import has_shape

PIPELINE_PY = ".buildkite/pipeline.py"
DIRECTORY = BUILDKITE_DIR.rstrip("/")
LATEST_SUCCESSFUL_ENTRY = "latest: true"

# The branches a dependency without a pin is taken from when its repository
# has neither the root's branch nor the root's line, in order: as the
# artifacts plugin's own fallback for a download.
LAST_RESORTS = ("master", "main")


# ---------------------------------------------------------------------------
# The inputs
# ---------------------------------------------------------------------------

# Buildkite's facts of one pipeline slug: ``buildkite.fetch_pipeline`` with the
# organisation and the token bound. Raises for an API error, and
# ``buildkite.PipelineNotFound`` for a 404; an error is never an answer.
Pipelines = Callable[[str], PipelineFacts]


@dataclass(frozen=True)
class Root:
    """The root build: its pipeline, name and commit, and how its plan is identified.

    ``name`` is the name it runs under (``BUILDKITE_BRANCH``), and ``refs``
    git's answer for the refs that name may be (``name_refs``): each that its
    repository has, with its commit, a tag peeled. ``commit`` is the commit
    checked out (``git rev-parse HEAD``), ``build_id`` the build's uuid (the
    plan id), ``rebuild`` the setting ``rebuild_setting`` gives.
    """

    project: str
    name: str
    commit: str
    refs: Mapping[str, str]
    build_id: str
    build_number: int
    rebuild: str = REBUILD_NONE


def name_refs(name: str) -> tuple[str, str]:
    """The refs a root's name may be, in the order they count: its branch, then its tag. Its
    plan step asks git for both, and for the tag peeled, in one question."""
    return branch_ref(name), tag_ref(name)


def named_ref(name: str, refs: Mapping[str, str]) -> str:
    """Where a root of ``name`` is taken from, of git's answer ``refs`` for ``name_refs``: the
    first of them its repository has; a name that is neither is planned as the branch it
    names."""
    return next((r for r in name_refs(name) if r in refs), branch_ref(name))


def root_ref(root: Root) -> str:
    """Where ``root`` is taken from (``named_ref``)."""
    return named_ref(root.name, root.refs)


@dataclass(frozen=True)
class Place:
    """Where the chain takes a project from: its repository, the full ref, and its commit."""

    repository: str
    ref: str
    commit: str


def rebuild_setting(value: str | None) -> str:
    """``SNAPSHOT_REBUILD`` (None when unset) as a setting: unset none, ``1`` one, ``all`` all.

    Any other value, also an empty one, fails: a person who sets the variable
    means one of the two, and a guess would build too much or too little.
    """
    if value is None:
        return REBUILD_NONE
    settings = {"1": REBUILD_ONE, "all": REBUILD_ALL}
    if value not in settings:
        raise PlanError(f"SNAPSHOT_REBUILD={value!r}: expected 1 or all")
    return settings[value]


def counts(path: str) -> bool:
    """The files of a commit the planner reads: its templates and ``pipeline.py``."""
    return is_template_path(path) or path == PIPELINE_PY


# ---------------------------------------------------------------------------
# Asking each question once
# ---------------------------------------------------------------------------

Commits = Callable[[str, Sequence[str]], Mapping[str, str]]

# The first of the refs that a repository has, as ``(ref, commit)``; None when
# it has none of them.
FirstRef = Callable[[str, Sequence[str]], "tuple[str, str] | None"]


def first_ref_asked_once(commits_of: Commits, memo: dict[tuple[str, str], str | None]) -> FirstRef:
    """The first of ``refs`` that ``url`` has, asking each repository for each ref at most once.

    A ref after the first one known to exist cannot be the answer, so it is
    not asked; the refs before it that were not asked before go into one
    call of ``commits_of``. ``memo`` maps ``(repository, ref)`` to its commit,
    None for a ref known to be absent: it holds what is known beforehand, and
    every answer of ``commits_of`` is added to it.
    """

    def first(url: str, refs: Sequence[str]) -> tuple[str, str] | None:
        ordered = list(dict.fromkeys(refs))
        known_at = (i for i, r in enumerate(ordered) if memo.get((url, r)) is not None)
        needed = ordered[: next(known_at, len(ordered) - 1) + 1]
        unasked = [r for r in needed if (url, r) not in memo]
        if unasked:
            found = commits_of(url, unasked)
            memo.update({(url, r): found.get(r) for r in unasked})
        present = ((r, memo[(url, r)]) for r in needed if memo[(url, r)] is not None)
        return next(present, None)

    return first


Files = Callable[[str, str], Mapping[str, bytes]]


def files_read_once(read: Files, memo: dict[tuple[str, str], Mapping[str, bytes]]) -> Files:
    """``read``, each commit of each repository read at most once: every read is added to
    ``memo``, by ``(repository, commit)``."""

    def files(url: str, commit: str) -> Mapping[str, bytes]:
        if (url, commit) not in memo:
            memo[(url, commit)] = read(url, commit)
        return memo[(url, commit)]

    return files


@dataclass(frozen=True)
class Memory:
    """What git told a walk as it asked: the commit of each ref of each repository, None for a
    ref the repository does not have (``refs``), and the files of each commit read
    (``files``)."""

    refs: dict[tuple[str, str], str | None]
    files: dict[tuple[str, str], Mapping[str, bytes]]


@dataclass(frozen=True)
class Sources:
    """What the walk asks, each question answered once, however often the walk is made.

    ``line`` is the release line of the build planned, found the first time a
    dependency without a pin asks for it, which only one under a build on a
    branch does: for the first build, finding it lists its repository and
    fetches every line's tip into the checkout. None means that the line is
    not known, and a dependency without a pin is taken from the build's own
    branch alone.
    """

    pipeline: Callable[[str], PipelineFacts]
    first_ref: FirstRef
    files: Files
    line: Callable[[], str | None]


def known_refs(root: Root, place: Place) -> dict[tuple[str, str], str | None]:
    """What git said of the refs of the root's name, in its repository: each at its commit, an
    absent one None; and the root's own ref at the root's commit, so another pipeline of that
    repository is planned at the root's commit, also when the ref moved since."""
    asked = {(place.repository, r): root.refs.get(r) for r in name_refs(root.name)}
    return {**asked, (place.repository, place.ref): place.commit}


def asked_once(
    git: Git, pipeline: Callable[[str], PipelineFacts], root: Root, place: Place, memory: Memory
) -> Sources:
    """``Sources`` over git and a memoised lookup, for ``root`` taken from ``place``. Git's
    answers and the files read are kept in ``memory``, which holds what is known beforehand
    (``known_refs``)."""

    def read(url: str, commit: str) -> Mapping[str, bytes]:
        return git.read_files(url, commit, DIRECTORY, counts)

    return Sources(
        pipeline=pipeline,
        first_ref=first_ref_asked_once(git.commits_of, memory.refs),
        files=files_read_once(read, memory.files),
        line=functools.cache(lambda: release_line(git, place.repository, root.name)),
    )


# ---------------------------------------------------------------------------
# Nodes, and projects that cannot be nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Member:
    """A node of the tree as the walk found it: where it is taken from, and what it declares."""

    place: Place
    declaration: Declaration


@dataclass(frozen=True)
class Reached:
    """How the walk reached a project: the consumer, and the consumer's entry of the project."""

    consumer: str
    entry: Entry


@dataclass(frozen=True)
class Refused:
    """A project the tree needs that cannot be a node where the chain takes it from.

    A value during the walk, not an error: a pin found later in the walk may
    move the project.
    """

    message: str


Found = Union[Member, Refused]


def snapshot_entries(d: Declaration) -> dict[str, Entry]:
    """The first entry of each snapshot dependency of ``d``, by project, sorted."""
    first: dict[str, Entry] = {}
    for e in declared_entries(d, SNAPSHOT):
        first.setdefault(e.project, e)
    return first


def deps_of(n: Member) -> list[str]:
    """The snapshot dependencies of ``n``, sorted."""
    return sorted(snapshot_entries(n.declaration))


def dependency_of(reached: Reached) -> str:
    """How a message says who needs the project: the consumer and the template of its entry."""
    return f"It is a snapshot dependency of {reached.consumer} ({reached.entry.path})."


# ---------------------------------------------------------------------------
# The three conditions of a snapshot dependency
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unmet:
    """A condition a project does not meet: what failed, and how the project would meet it."""

    failed: str
    adopt: str


def unmet_buildkite(project: str) -> Unmet:
    return Unmet(
        f"Buildkite's configuration of {project} does not run {PIPELINE_PY} into a pipeline upload",
        f"{project} adopts: its Buildkite configuration runs "
        f"{PIPELINE_PY} | buildkite-agent pipeline upload",
    )


def unmet_shape(project: str, place: Place, problem: str) -> Unmet:
    return Unmet(
        f"{PIPELINE_PY} {problem}",
        f"{project} adopts on {place.ref}: its {PIPELINE_PY} imports run from "
        'qdb_pipeline and ends with if __name__ == "__main__": run(generate_pipeline)',
    )


def unmet_latest(project: str, place: Place, latest: Entry) -> Unmet:
    path = latest.path
    return Unmet(
        f"its declaration holds a latest successful dependency, which a snapshot never "
        f"contains: {path} downloads {latest.project} with latest: true",
        f"{project} takes {latest.project} as a snapshot dependency on {place.ref} "
        f"(no latest: true in its entry of {latest.project} in {path})",
    )


def pipeline_py_problem(source: bytes | None) -> str | None:
    """Why ``.buildkite/pipeline.py`` (None when there is none) lacks the runner's shape."""
    if source is None:
        return "is not there"
    return None if has_shape(source) else "does not contain qdb_pipeline and run(generate_pipeline)"


def first_unmet(
    project: str, place: Place, facts: PipelineFacts, src: Sources
) -> Unmet | Declaration:
    """The first of the three conditions that ``project`` at ``place`` does not meet.

    In this order: Buildkite's configuration runs ``.buildkite/pipeline.py``
    (when it does not, the repository is not read); that file at the commit
    has the runner's shape; the declaration of the commit's templates holds
    no latest successful dependency. Returns the declaration when all three
    hold; a template that does not parse raises ``TemplateError``.
    """
    if not facts.runs_pipeline_py:
        return unmet_buildkite(project)
    files = src.files(place.repository, place.commit)
    problem = pipeline_py_problem(files.get(PIPELINE_PY))
    if problem is not None:
        return unmet_shape(project, place, problem)
    declaration = read_declaration(files, project)
    latest = declared_entries(declaration, LATEST_SUCCESSFUL)
    if latest:
        return unmet_latest(project, place, latest[0])
    return declaration


def refusal(project: str, reached: Reached, place: Place, unmet: Unmet) -> Refused:
    """A dependency that cannot give a snapshot: who needs it, where, what failed, the way out."""
    consumer, path = reached.consumer, reached.entry.path
    at = f"{place.ref} {place.commit} of {place.repository}"
    return Refused(
        f"{project} cannot give a snapshot at {at}: "
        f"{unmet.failed}. {dependency_of(reached)} Way out: {unmet.adopt}; or {consumer} "
        f"takes {project} as a latest successful dependency, with {LATEST_SUCCESSFUL_ENTRY} "
        f"in its entry of {project} in {path}."
    )


# ---------------------------------------------------------------------------
# Where a project is taken from
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pin:
    """A pin as found: the project and its ref, and the node, commit and template that say so."""

    project: str
    ref: str
    node: str
    commit: str
    path: str


def shown_pin(pin: Pin) -> str:
    return f"{pin.ref} (in {pin.path} of {pin.node} at {pin.commit})"


def default_refs(root_at: str, line: Callable[[], str | None]) -> tuple[str, ...]:
    """The refs a dependency without a pin is taken from, in the order they are tried.

    ``root_at`` is the root's ref. Under a root at a tag, that tag and nothing
    else: a release takes no dependency from another ref. Under a root on a
    branch, that branch, its release line (``line``, looked for only here),
    ``master`` and ``main``; that branch alone when its line is not known.
    """
    if is_tag(root_at):
        return (root_at,)
    known = line()
    if known is None:
        return (root_at,)
    return tuple(dict.fromkeys(branch_ref(b) for b in (name_of(root_at), known, *LAST_RESORTS)))


def unplaced(
    project: str, repository: str, reached: Reached, root_at: str, refs: Sequence[str]
) -> Refused:
    """A project whose ``repository`` has none of ``refs``, the refs it may be taken from under
    a root at the ref ``root_at``."""
    if is_tag(root_at):
        tag = name_of(root_at)
        return Refused(
            f"{project} cannot be planned: {repository} has no tag {tag}, and a build at a tag "
            f"takes every dependency from the same tag. {dependency_of(reached)} Way out: push "
            f"the tag {tag} to {repository}."
        )
    return Refused(
        f"{project} cannot be planned: {repository} has none of "
        f"{', '.join(refs)}. {dependency_of(reached)}"
    )


def place_of_pin(project: str, repository: str, pin: Pin, src: Sources) -> Place | Refused:
    """The commit of the pin's ref; a ref that does not exist refuses the project."""
    found = src.first_ref(repository, [pin.ref])
    if found is None:
        return Refused(
            f"{project} cannot be planned: its pin {shown_pin(pin)} is not a ref of {repository}."
        )
    return Place(repository, *found)


def place_by_default(
    project: str, repository: str, reached: Reached, root_at: str, src: Sources
) -> Place | Refused:
    """The first of ``default_refs`` that ``repository`` has; none refuses the project.

    ``root_at`` is the root's ref. All of them are asked in one question.
    """
    refs = default_refs(root_at, src.line)
    found = src.first_ref(repository, refs)
    if found is not None:
        return Place(repository, *found)
    return unplaced(project, repository, reached, root_at, refs)


def facts_of(project: str, reached: Reached, src: Sources) -> PipelineFacts:
    """Buildkite's facts of ``project``; a 404 fails the plan, naming who needs the project."""
    try:
        return src.pipeline(project)
    except PipelineNotFound as exc:
        raise PlanError(f"{exc}. {dependency_of(reached)}") from exc


def resolve(project: str, reached: Reached, pin: Pin | None, root_at: str, src: Sources) -> Found:
    """Where the chain takes ``project`` from, and whether it can give a snapshot there.

    ``root_at`` is the root's ref.
    """
    facts = facts_of(project, reached, src)
    if pin is not None:
        place = place_of_pin(project, facts.repository, pin, src)
    else:
        place = place_by_default(project, facts.repository, reached, root_at, src)
    if isinstance(place, Refused):
        return place
    found = first_unmet(project, place, facts, src)
    if isinstance(found, Unmet):
        return refusal(project, reached, place, found)
    return Member(place, found)


# ---------------------------------------------------------------------------
# The walk
# ---------------------------------------------------------------------------


def walk(
    root_project: str, first: Member, pins: Mapping[str, Pin], src: Sources
) -> dict[str, Found]:
    """Every project of the tree of ``root_project``, breadth first, each resolved once.

    ``first`` is the root as a node. A refused project is not descended into.
    """
    nodes: dict[str, Found] = {root_project: first}
    queue = [(root_project, first)]
    while queue:
        consumer, node = queue.pop(0)
        for project, entry in snapshot_entries(node.declaration).items():
            if project in nodes:
                continue
            reached = Reached(consumer, entry)
            found = resolve(project, reached, pins.get(project), first.place.ref, src)
            nodes[project] = found
            if isinstance(found, Member):
                queue.append((project, found))
    return nodes


def found_pins(nodes: Mapping[str, Found]) -> list[Pin]:
    """The pins of every node; a refused project is never read for pins."""
    return [
        Pin(e.project, e.pin, p, n.place.commit, e.path)
        for p, n in sorted(nodes.items())
        if isinstance(n, Member)
        for e in n.declaration.entries
        if e.pin is not None
    ]


def chain_pins(nodes: Mapping[str, Found]) -> dict[str, Pin]:
    """``{project: pin}`` of the pins of every node.

    Two different pins of one project fail the plan and name where each was
    found. One ref written in two places is one pin.
    """
    by_project: dict[str, list[Pin]] = {}
    for pin in found_pins(nodes):
        by_project.setdefault(pin.project, []).append(pin)
    for project, pins in sorted(by_project.items()):
        if len({p.ref for p in pins}) > 1:
            shown = "; ".join(shown_pin(p) for p in pins)
            raise PlanError(f"{project} has different pins in this chain: {shown}")
    return {project: pins[0] for project, pins in by_project.items()}


def pin_refs(pins: Mapping[str, Pin]) -> dict[str, str]:
    return {project: pin.ref for project, pin in pins.items()}


def shown_pins(refs: Mapping[str, str]) -> str:
    return "{" + ", ".join(f"{p}: {r}" for p, r in sorted(refs.items())) + "}"


def settled_walk(project: str, first: Member, src: Sources) -> dict[str, Found]:
    """The walk of the tree of ``project``, which ``first`` is, made with the pins it finds.

    It starts from the pins of ``first``'s own templates; while the pins found
    differ from those the walk was made with, it is made again with the pins
    found. A set of pins seen before means they never settle, which fails the
    plan.
    """
    pins = chain_pins({project: first})
    tried: list[dict[str, str]] = []
    while True:
        nodes = walk(project, first, pins, src)
        found = chain_pins(nodes)
        if pin_refs(found) == pin_refs(pins):
            return nodes
        tried.append(pin_refs(pins))
        if pin_refs(found) in tried:
            shown = " -> ".join(shown_pins(p) for p in (*tried, pin_refs(found)))
            raise PlanError(f"the pins of this chain never settle, each set moves another: {shown}")
        pins = found


def tree_of(nodes: Mapping[str, Found]) -> dict[str, Member]:
    """The nodes of a settled walk; refused projects fail the plan, each named on a line."""
    refused = [n.message for _, n in sorted(nodes.items()) if isinstance(n, Refused)]
    if refused:
        raise PlanError("\n".join(refused))
    return {p: n for p, n in nodes.items() if isinstance(n, Member)}


def bottom_up(tree: Mapping[str, Member], start: str) -> list[str]:
    """Every project reachable from ``start``, each after its dependencies.

    Raises ``PlanError`` naming the cycle when there is one.
    """
    order: list[str] = []
    done: set[str] = set()

    def visit(p: str, stack: tuple[str, ...]) -> None:
        if p in done:
            return
        if p in stack:
            cycle = (*stack[stack.index(p) :], p)
            raise PlanError(f"dependency cycle: {' -> '.join(cycle)}")
        for d in deps_of(tree[p]):
            visit(d, (*stack, p))
        done.add(p)
        order.append(p)

    visit(start, ())
    return order


# ---------------------------------------------------------------------------
# Who writes the branch's latest files, dependencies first
# ---------------------------------------------------------------------------


def compute_below(tree: Mapping[str, Member], order: Sequence[str]) -> dict[str, frozenset[str]]:
    """The nodes below each project of ``order`` (dependencies first): all that it reaches."""
    below: dict[str, frozenset[str]] = {}
    for p in order:
        deps = deps_of(tree[p])
        below[p] = frozenset(deps).union(*(below[d] for d in deps))
    return below


class Untold(Exception):
    """A fact git did not give this plan: a ref it did not ask, or a commit it did not read."""


@dataclass(frozen=True)
class Told:
    """What git told this plan: every ``ls-remote`` answer (``refs``, an absent ref None), the
    files of every commit it read (``files``), the first build's place, and its release line
    as ``known_line`` gives it (None: not known to the settled tree, also when an earlier pass
    of the walk looked it up)."""

    refs: Mapping[tuple[str, str], str | None]
    files: Mapping[tuple[str, str], Mapping[str, bytes]]
    first: Place
    line: str | None


def known_line(project: str, tree: Mapping[str, Member], src: Sources) -> str | None:
    """The release line of the first build, ``project``, when its settled tree has it: a first
    build on a release line has its branch, with no question to git; one on another branch
    has it when the tree holds a project placed without a pin, for which the walk looked the
    line up, and ``src.line`` keeps it. Else None."""
    branch = branch_of(tree[project].place.ref)
    if branch is None:
        return None
    placed_by_default = tree.keys() - chain_pins(tree).keys() - {project}
    return src.line() if is_release_line(branch) or placed_by_default else None


def own_line(at: Place, told: Told, first_ref: FirstRef) -> str | None:
    """The release line of a first build at ``at``, as far as this plan knows it: its branch
    when that is a line; on the first build's branch, the first build's line in the first
    build's repository, whose branches gave that line, and in another repository when this
    plan knows that it has a branch of that name, a branch of one name being taken to leave
    one line in every repository that has it; else None, not known, and only its own branch
    counts. At a tag no line is needed."""
    branch = branch_of(at.ref)
    if branch is None or is_release_line(branch):
        return branch
    if at.ref != told.first.ref or told.line is None:
        return None
    if at.repository == told.first.repository:
        return told.line
    try:
        return told.line if first_ref(at.repository, [branch_ref(told.line)]) else None
    except Untold:
        return None


def told_sources(at: Place, told: Told, src: Sources) -> Sources:
    """``Sources`` of the own build of the project at ``at`` that answer from ``told`` alone: a
    ref git was not asked, or a commit this plan did not read, raises ``Untold``."""

    def untold(url: str, refs: Sequence[str]) -> Mapping[str, str]:
        raise Untold(f"{', '.join(refs)} of {url}")

    def files(url: str, commit: str) -> Mapping[str, bytes]:
        if (url, commit) not in told.files:
            raise Untold(f"{commit} of {url}")
        return told.files[(url, commit)]

    first_ref = first_ref_asked_once(untold, dict(told.refs))
    line = functools.cache(lambda: own_line(at, told, first_ref))
    return Sources(src.pipeline, first_ref, files, line)


def writes_latest_file(
    p: str,
    tree: Mapping[str, Member],
    below: Mapping[str, frozenset[str]],
    told: Told,
    src: Sources,
) -> bool:
    """Whether the build of ``p`` started for a dependency writes its branch's latest files:
    when its own build, a first build of ``p`` at its ref and commit planned from what git
    told this plan, can be planned and takes every project below ``p`` at the commit this plan
    takes it at. What git did not tell this plan counts as no."""
    at = tree[p].place
    try:
        own = tree_of(settled_walk(p, tree[p], told_sources(at, told, src)))
    except (PlanError, Untold):
        return False
    return all(m in own and own[m].place.commit == tree[m].place.commit for m in below[p])


def planned_node(n: Member, writes: bool, latest: tuple[Latest, ...]) -> Planned:
    """The node ``n`` as the plan holds it, with whether its build writes its branch's latest
    files and its latest successful downloads, before its key."""
    p = n.place
    return Planned(p.ref, p.commit, None, writes, tuple(deps_of(n)), n.declaration.steps, latest)


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def make_plan(
    root: Root, own: Declaration, latest: tuple[Latest, ...], git: Git, pipelines: Pipelines
) -> Plan:
    """The plan of ``root``; see the module docstring.

    ``own`` is the declaration read from the root's own commit, ``latest`` the
    root's latest successful downloads with the keys read for them. Raises
    ``PlanError`` when the tree cannot be planned; other errors of git, of
    Buildkite and of templates pass through unchanged.
    """
    pipeline = functools.cache(pipelines)
    first = Member(Place(pipeline(root.project).repository, root_ref(root), root.commit), own)
    memory = Memory(known_refs(root, first.place), {})
    src = asked_once(git, pipeline, root, first.place, memory)
    tree = tree_of(settled_walk(root.project, first, src))
    order = bottom_up(tree, root.project)
    below = compute_below(tree, order)
    told = Told(memory.refs, memory.files, first.place, known_line(root.project, tree, src))
    writes = {p: p == root.project or writes_latest_file(p, tree, below, told, src) for p in order}
    unkeyed = Plan(
        id=root.build_id,
        context=root.name,
        rebuild=root.rebuild,
        first=First(root.project, root.build_number),
        projects={
            p: planned_node(tree[p], writes[p], latest if p == root.project else ()) for p in order
        },
    )
    return keyed(unkeyed)
