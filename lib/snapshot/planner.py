"""The plan of a root build (specification sections 2, 3 and 4).

``make_plan(root, own, git, pipelines)`` fixes, at one instant, the commit
every project of the root's snapshot tree is built from, its key, and the
branch name a build of it runs under, and returns the plan (``plan.Plan``). It
is a function of its inputs: the root's facts (``Root``), the declaration read
from the root's own commit, and two interfaces it asks and never writes to:

- ``git.Git``: ``commits_of`` (the commits of full refs), ``read_files`` (the
  files of another repository's commit), and ``branches`` and
  ``merge_base_times`` for the root's release line (``lines.release_line``);
- ``Pipelines``: Buildkite's facts of a pipeline slug
  (``buildkite.PipelineFacts``: its repository, and whether its configuration
  runs ``.buildkite/pipeline.py``).

Each question is asked once: a commit per repository and ref (the refs a
dependency without a pin may be taken from in one ``ls-remote``, none after a
ref known to exist), a read per repository and commit, a Buildkite call per
slug.

The tree is the root and, recursively, the snapshot dependencies of every
node. Latest successful dependencies are not in it: they are not planned and
not asked about. The walk is breadth first from the root; each project is
resolved once, for the first consumer that reaches it (consumers in the order
reached, their dependencies sorted):

1. The root, at its own commit and ref, with its own declaration. Its repository is Buildkite's answer for its slug. Its release line
   (``lines.release_line`` of that repository and its branch) is found the
   first time a dependency without a pin is placed, and only then. Its
   declaration may hold a latest successful dependency: then it has no
   snapshot and no key, and is planned all the same.
2. For each snapshot dependency of a node, in this order:

   a. Buildkite's facts of the project, for its repository; a 404 fails the
      plan;
   b. its ref: its pin; else the root's branch when its repository has it;
      else the root's release line when its repository has it; else
      ``master``; else ``main``. A pin names ``refs/heads/<branch>`` or
      ``refs/tags/<tag>``: any other text is a ref no repository has;
   c. its commit, ``git.commits_of``, a tag peeled; the root's repository and
      branch give the root's own commit. A ref that does not exist refuses
      the project;
   d. the conditions of section 3, the first that fails refusing the project
      with its own message (``first_unmet``): Buildkite's configuration runs
      ``.buildkite/pipeline.py``; that file at the commit has the runner's
      shape; its declaration holds no latest successful dependency. A
      template that does not parse fails the plan, naming the project and
      the file (``declaration.TemplateError``);
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

   - the keys: ``canonical.snapshot_key`` of the node's commit and the keys of
     its snapshot dependencies, with the plan id when ``SNAPSHOT_REBUILD``
     forces the node (the root under ``one``, every node under ``all``); None
     for a root that has no snapshot;
   - the labels: the branch a triggered build runs under, uploads under and
     moves ``LATEST_SUCCESSFUL`` of. A node runs under its own branch only
     when it runs as itself (``runs_as_itself``): its build is the one its own
     pipeline would make for that branch, as far as the plan shows. Any other
     node runs under ``snapshot/<root branch>``, which no branch has. The
     rule needs where each pin was written (``pin_writers``), and the root's
     repository: the plan knows the release line of that repository only.
"""

from __future__ import annotations

import functools
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Callable, Union

from ..forms import BRANCH_PREFIX
from .buildkite import PipelineFacts, PipelineNotFound
from .canonical import snapshot_key
from .declaration import (
    BUILDKITE_DIR,
    LATEST_SUCCESSFUL,
    SNAPSHOT,
    Declaration,
    Entry,
    declared_entries,
    has_snapshot,
    is_template_path,
    read_declaration,
)
from .git import Git, branch_of, branch_ref
from .lines import is_release_line, release_line
from .plan import (
    REBUILD_ALL,
    REBUILD_NONE,
    REBUILD_ONE,
    Node,
    Plan,
    PlanError,
    RootInfo,
)
from .shape import shape_problem
from .shown import shown_text, shown_value

PIPELINE_PY = ".buildkite/pipeline.py"
DIRECTORY = BUILDKITE_DIR.rstrip("/")
LATEST_SUCCESSFUL_ENTRY = "build_id: LATEST_SUCCESSFUL"

# What the label of a node that does not run as itself starts with.
SNAPSHOT_LABEL_PREFIX = "snapshot/"

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
    """The root build: its pipeline, branch and commit, and how its plan is identified.

    ``commit`` is the commit checked out (``git rev-parse HEAD``),
    ``build_id`` the build's uuid (the plan id), ``rebuild`` the setting
    ``rebuild_setting`` gives.
    """

    project: str
    branch: str
    commit: str
    build_id: str
    build_number: int
    rebuild: str = REBUILD_NONE


def rebuild_setting(value: str | None) -> str:
    """``SNAPSHOT_REBUILD`` (None when unset) as a setting: unset none, ``1`` one, ``all`` all.

    Any other value, also an empty one, fails: a person who sets the variable
    means one of the two, and a guess would build too much or too little.
    """
    if value is None:
        return REBUILD_NONE
    settings = {"1": REBUILD_ONE, "all": REBUILD_ALL}
    if value not in settings:
        raise PlanError(f"SNAPSHOT_REBUILD={shown_value(value)}: expected 1 or all")
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


def first_ref_asked_once(commits_of: Commits, known: Mapping[tuple[str, str], str]) -> FirstRef:
    """The first of ``refs`` that ``url`` has, asking each repository for each ref at most once.

    A ref after the first one known to exist cannot be the answer, so it is
    not asked; the refs before it that were not asked before go into one
    call of ``commits_of``. ``known`` maps ``(repository, ref)`` to commits
    known beforehand.
    """
    memo: dict[tuple[str, str], str | None] = dict(known)

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


@dataclass(frozen=True)
class Sources:
    """What the walk asks, each question answered once, however often the walk is made.

    ``line`` is the root's release line, found the first time a dependency
    without a pin asks for it: finding it lists the root's repository and
    fetches every line's tip into the checkout.
    """

    pipeline: Callable[[str], PipelineFacts]
    first_ref: FirstRef
    files: Callable[[str, str], Mapping[str, bytes]]
    line: Callable[[], str]


def asked_once(
    git: Git, pipeline: Callable[[str], PipelineFacts], repository: str, branch: str, commit: str
) -> Sources:
    """``Sources`` over git and a memoised lookup, for a root on ``branch`` at ``commit``.

    The root's branch of its ``repository`` is known to be at its commit, so
    another pipeline of that repository is planned at the root's commit.
    """
    return Sources(
        pipeline=pipeline,
        first_ref=first_ref_asked_once(git.commits_of, {(repository, branch_ref(branch)): commit}),
        files=functools.cache(lambda url, c: git.read_files(url, c, DIRECTORY, counts)),
        line=functools.cache(lambda: release_line(git, repository, branch)),
    )


# ---------------------------------------------------------------------------
# Nodes, and projects that cannot be nodes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Place:
    """Where the chain takes a project from: its repository, the full ref, and its commit."""

    repository: str
    ref: str
    commit: str


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
    return f"It is a snapshot dependency of {reached.consumer} ({shown_text(reached.entry.path)})."


# ---------------------------------------------------------------------------
# The four conditions of section 3
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unmet:
    """A condition a project does not meet: what failed, and how the project would meet it."""

    failed: str
    adopt: str


def unmet_buildkite(project: str) -> Unmet:
    return Unmet(
        f"Buildkite's configuration of {project} does not run {PIPELINE_PY} into a pipeline upload",
        f"{project} adopts: the first step of its Buildkite configuration runs "
        f"{PIPELINE_PY} | buildkite-agent pipeline upload",
    )


def unmet_shape(project: str, place: Place, problem: str) -> Unmet:
    return Unmet(
        f"{PIPELINE_PY} {problem}",
        f"{project} adopts on {shown_text(place.ref)}: its {PIPELINE_PY} imports run from "
        'qdb_pipeline and ends with if __name__ == "__main__": run(generate_pipeline)',
    )


def unmet_latest(project: str, place: Place, latest: Entry) -> Unmet:
    path = shown_text(latest.path)
    return Unmet(
        f"its declaration holds a latest successful dependency, which a snapshot never "
        f"contains: {path} downloads {latest.project} with build_id",
        f"{project} takes {latest.project} as a snapshot dependency on {shown_text(place.ref)} "
        f"(no build_id in its entry of {latest.project} in {path})",
    )


def pipeline_py_problem(source: bytes | None) -> str | None:
    """Why ``.buildkite/pipeline.py`` (None when there is none) lacks the runner's shape."""
    if source is None:
        return "is not there"
    return shape_problem(source)


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
    consumer, path = reached.consumer, shown_text(reached.entry.path)
    at = f"{shown_text(place.ref)} {place.commit} of {place.repository}"
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
    return f"{shown_text(pin.ref)} (in {shown_text(pin.path)} of {pin.node} at {pin.commit})"


def default_refs(context: str, line: str) -> tuple[str, ...]:
    """The refs a dependency without a pin is taken from, in the order they are tried."""
    return tuple(dict.fromkeys(branch_ref(b) for b in (context, line, *LAST_RESORTS)))


def place_of_pin(project: str, repository: str, pin: Pin, src: Sources) -> Place | Refused:
    """The commit of the pin's ref; a ref that does not exist refuses the project."""
    found = src.first_ref(repository, [pin.ref])
    if found is None:
        return Refused(
            f"{project} cannot be planned: its pin {shown_pin(pin)} is not a ref of {repository}."
        )
    return Place(repository, *found)


def place_by_default(
    project: str, repository: str, reached: Reached, context: str, src: Sources
) -> Place | Refused:
    """The first of ``default_refs`` that ``repository`` has; none refuses the project.

    ``context`` is the root's branch. All of them are asked in one question.
    """
    refs = default_refs(context, src.line())
    found = src.first_ref(repository, refs)
    if found is not None:
        return Place(repository, *found)
    return Refused(
        f"{project} cannot be planned: {repository} has none of "
        f"{', '.join(shown_text(r) for r in refs)}. {dependency_of(reached)}"
    )


def facts_of(project: str, reached: Reached, src: Sources) -> PipelineFacts:
    """Buildkite's facts of ``project``; a 404 fails the plan, naming who needs the project."""
    try:
        return src.pipeline(project)
    except PipelineNotFound as exc:
        raise PlanError(f"{exc}. {dependency_of(reached)}") from exc


def resolve(project: str, reached: Reached, pin: Pin | None, context: str, src: Sources) -> Found:
    """Where the chain takes ``project`` from, and whether it can give a snapshot there."""
    facts = facts_of(project, reached, src)
    if pin is not None:
        place = place_of_pin(project, facts.repository, pin, src)
    else:
        place = place_by_default(project, facts.repository, reached, context, src)
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
    root: str, first: Member, context: str, pins: Mapping[str, Pin], src: Sources
) -> dict[str, Found]:
    """Every project of the tree of ``root``, breadth first, each resolved once.

    ``context`` is the root's branch. A refused project is not descended into.
    """
    nodes: dict[str, Found] = {root: first}
    queue = [(root, first)]
    while queue:
        consumer, node = queue.pop(0)
        for project, entry in snapshot_entries(node.declaration).items():
            if project in nodes:
                continue
            found = resolve(project, Reached(consumer, entry), pins.get(project), context, src)
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
    return "{" + ", ".join(f"{p}: {shown_text(r)}" for p, r in sorted(refs.items())) + "}"


def settled_walk(root: Root, first: Member, src: Sources) -> dict[str, Found]:
    """The walk made with the pins it finds.

    It starts from the root's own pins; while the pins found differ from those
    the walk was made with, it is made again with the pins found. A set of
    pins seen before means they never settle, which fails the plan.
    """
    pins = chain_pins({root.project: first})
    tried: list[dict[str, str]] = []
    while True:
        nodes = walk(root.project, first, root.branch, pins, src)
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
# Keys and labels, dependencies first
# ---------------------------------------------------------------------------


def forced_by(root: Root, project: str) -> str | None:
    """The plan id when ``SNAPSHOT_REBUILD`` forces ``project`` to be built again; else None."""
    every = root.rebuild == REBUILD_ALL
    forced = every or (root.rebuild == REBUILD_ONE and project == root.project)
    return root.build_id if forced else None


def compute_keys(
    tree: Mapping[str, Member], order: Sequence[str], root: Root
) -> dict[str, str | None]:
    """The key of every project of ``order`` (dependencies first); None without a snapshot."""
    keys: dict[str, str | None] = {}
    for p in order:
        n = tree[p]
        deps = {d: keys[d] for d in deps_of(n)}
        snapshot = has_snapshot(n.declaration)
        keys[p] = snapshot_key(p, n.place.commit, deps, forced_by(root, p)) if snapshot else None
    return keys


def compute_below(tree: Mapping[str, Member], order: Sequence[str]) -> dict[str, frozenset[str]]:
    """The nodes below each project of ``order`` (dependencies first): all that it reaches."""
    below: dict[str, frozenset[str]] = {}
    for p in order:
        deps = deps_of(tree[p])
        below[p] = frozenset(deps).union(*(below[d] for d in deps))
    return below


def pin_writers(tree: Mapping[str, Member]) -> dict[str, frozenset[str]]:
    """``{project: the nodes whose templates write its pin}``, for every project a pin places.

    One ref of a pin may be written in several places; every writer counts.
    """
    writers: dict[str, set[str]] = {}
    for pin in found_pins(tree):
        writers.setdefault(pin.project, set()).add(pin.node)
    return {p: frozenset(w) for p, w in writers.items()}


def runs_as_itself(
    p: str,
    tree: Mapping[str, Member],
    below: Mapping[str, frozenset[str]],
    writers: Mapping[str, frozenset[str]],
    root: Place,
) -> bool:
    """Whether a build of ``p`` is the build its own pipeline would make for its branch.

    Its tree is ``p`` and the nodes below it; ``root`` is where the root is
    taken from. A node at a tag never runs as itself. A node on a branch does
    when one of these holds:

    1. Its ref is the root's, no node below it was placed by a pin written
       outside its tree, and every node below it that no pin written inside
       its tree placed is at that ref, unless its line is known to be the
       root's. Its own build takes a dependency without a pin from its own
       line, where the chain takes it from the root's; the two are one line
       when ``p`` is of the root's repository, or when the root's branch is a
       line, which is then the line of both.
    2. Its ref is a release line, and every node below it is at that ref or
       was placed by a pin written inside its tree.
    """
    place = tree[p].place
    branch = branch_of(place.ref)
    if branch is None:
        return False
    inside = below[p] | {p}

    def pinned_from_inside(m: str) -> bool:
        return bool(writers.get(m, frozenset()) & inside)

    def at_its_ref(m: str) -> bool:
        return tree[m].place.ref == place.ref

    same_line = place.repository == root.repository or is_release_line(branch)

    def placed_as_by_itself(m: str) -> bool:
        if m in writers:
            return pinned_from_inside(m)
        return same_line or at_its_ref(m)

    on_roots_branch = place.ref == root.ref and all(placed_as_by_itself(m) for m in below[p])
    on_its_line = is_release_line(branch) and all(
        at_its_ref(m) or pinned_from_inside(m) for m in below[p]
    )
    return on_roots_branch or on_its_line


def label_of(p: str, as_itself: bool, tree: Mapping[str, Member], context: str) -> str:
    """The branch a build of ``p`` runs under: its own when it runs as itself, else another.

    A node that runs as itself is on a branch (``runs_as_itself``). The other
    is ``snapshot/<root branch>``: no branch has that name, so a build under it
    moves no ``LATEST_SUCCESSFUL`` that anybody reads.
    """
    own = tree[p].place.ref[len(BRANCH_PREFIX) :]
    return own if as_itself else SNAPSHOT_LABEL_PREFIX + context


def plan_node(n: Member, key: str | None, label: str) -> Node:
    p = n.place
    return Node(p.ref, p.commit, key, label, tuple(deps_of(n)))


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def make_plan(root: Root, own: Declaration, git: Git, pipelines: Pipelines) -> Plan:
    """The plan of ``root``; see the module docstring.

    ``own`` is the declaration read from the root's own commit. Raises
    ``PlanError`` when the tree cannot be planned; other errors of git, of
    Buildkite and of templates pass through unchanged.
    """
    pipeline = functools.cache(pipelines)
    repository = pipeline(root.project).repository
    first = Member(Place(repository, branch_ref(root.branch), root.commit), own)
    src = asked_once(git, pipeline, repository, root.branch, root.commit)
    tree = tree_of(settled_walk(root, first, src))
    order = bottom_up(tree, root.project)
    keys = compute_keys(tree, order, root)
    below = compute_below(tree, order)
    writers = pin_writers(tree)
    as_itself = {p: runs_as_itself(p, tree, below, writers, first.place) for p in order}
    return Plan(
        id=root.build_id,
        context=root.branch,
        rebuild=root.rebuild,
        root=RootInfo(root.project, root.build_number),
        nodes={
            p: plan_node(tree[p], keys[p], label_of(p, as_itself[p], tree, root.branch))
            for p in order
        },
    )
