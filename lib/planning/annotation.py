"""What the plan step and the last step show on the build page. Text only.

A plan is a table; a failure is a fenced code block between a heading and a closing sentence.
A name that a repository chose, such as a branch or a template path, is in a code span or in
the block, because it can hold Markdown characters.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import NamedTuple

from ..forms import name_of, short_commit
from ..job import Build
from ..layout import Folder
from ..markdown import build_link, code, codes, collapsed, table
from .picture import PICTURE
from .plan import REBUILD_ALL, REBUILD_ONE, Plan, Planned, tree_of, trigger_label
from .records import NOT_DECLARED, RULE, Cause, Record

INDENT = "  "

Column = tuple[str, Callable[[Planned], str]]


def fenced(lines: Sequence[str]) -> str:
    return "```\n" + "\n".join(lines) + "\n```"


def shown_folders(folders: Iterable[Folder]) -> str:
    return ", ".join(map(str, folders))


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

REBUILDS = {REBUILD_ONE: "`{root}` is built again", REBUILD_ALL: "every project is built again"}

# The picture of the tree, which the plan step uploads as an artifact of the build.
TREE_PICTURE = (
    f'<img src="artifact://{PICTURE}" alt="The tree of the plan: what is built first on the '
    'left, this build on the right">'
)

# A table cell drops leading spaces; these it keeps.
STEP_IN = "&nbsp;" * 5


class Place(NamedTuple):
    """A row of the tree: a project, how deep it stands there, and whether a row above
    shows it already."""

    project: str
    depth: int
    again: bool


def places(plan: Plan, project: str) -> list[Place]:
    """The tree of ``project``, depth first, a project under each project that takes it.
    What a project takes is under its first row only."""
    found: list[Place] = []
    shown: set[str] = set()

    def visit(p: str, depth: int) -> None:
        found.append(Place(p, depth, p in shown))
        if p not in shown:
            shown.add(p)
            for d in plan.projects[p].deps:
                visit(d, depth + 1)

    visit(project, 0)
    return found


def project_cell(at: Place) -> str:
    """The tree in one column: a project stepped in by its depth, and one shown above in grey
    (``gray`` is a class of Buildkite's page; a pipeline's slug holds no Markdown)."""
    name = f'<span class="gray">{at.project} (above)</span>' if at.again else code(at.project)
    return STEP_IN * at.depth + name


def plan_columns(plan: Plan, planned: Iterable[Planned]) -> list[Column]:
    """A project's commit; and its branch or tag, a column that is there only when a project
    is taken from another than the build's, and says something only for such a project."""

    def elsewhere(n: Planned) -> str:
        return "" if name_of(n.ref) == plan.context else code(name_of(n.ref))

    columns: list[Column] = [("Commit", lambda n: code(short_commit(n.commit)))]
    if any(map(elsewhere, planned)):
        columns.append(("Branch or tag", elsewhere))
    return columns


def plan_table(plan: Plan, project: str) -> str:
    """One row per place of the tree; a project's facts are in its first row."""
    rows = places(plan, project)
    columns = plan_columns(plan, (plan.projects[at.project] for at in rows))

    def row(at: Place) -> list[str]:
        facts = ["" if at.again else cell(plan.projects[at.project]) for _, cell in columns]
        return [project_cell(at), *facts]

    return table(["Project", *(heading for heading, _ in columns)], map(row, rows))


def not_the_latest(plan: Plan, projects: Iterable[str]) -> list[str]:
    """A line for each project whose build does not write its branch's latest files: what
    that means for a person, and why."""

    def line(p: str, branch: str) -> str:
        return (
            f"- {code(p)} is built here against other commits of the projects below it than a "
            f"build of its branch {branch} would take. This build uses the result; it does not "
            f"become the latest successful build of {branch}."
        )

    planned = ((p, plan.projects[p]) for p in projects)
    return [line(p, code(name_of(n.ref))) for p, n in planned if not n.writes_latest_file]


def keys_table(plan: Plan, projects: Sequence[str]) -> str:
    rows = [[code(p), code(plan.projects[p].key)] for p in projects]
    return table(["Project", "Snapshot key"], rows)


def plan_annotation(plan: Plan, project: str, left_out: Sequence[tuple[str, str, str]]) -> str:
    """The plan as ``project`` builds it: the picture of its tree, when it takes any project;
    the tree as a table; a line for each project that needs a word; and every key behind a
    line that opens. ``left_out`` names each download of the project's own at another ref, as
    step, platform and ref: its key leaves such a download out."""
    root = plan.root
    heading = f"Snapshot plan of `{project}`" + (
        f" build {root.build_number}"
        if project == root.project
        else f", started for `{root.project}` build {root.build_number}"
    )
    rebuilds = REBUILDS.get(plan.rebuild)
    context = f"Branch {code(plan.context)}" + (
        f"; {rebuilds.format(root=root.project)}" if rebuilds else ""
    )
    outside = [
        f"- Not in the key: step {code(step)} takes {code(project)} {code(platform)} of branch "
        f"{code(name_of(ref))}; its job reads that branch's latest build when it runs."
        for step, platform, ref in left_out
    ]
    projects = tree_of(plan, project)
    parts = [
        f"**{heading}**",
        context,
        TREE_PICTURE if len(projects) > 1 else "",
        plan_table(plan, project),
        "\n".join([*outside, *not_the_latest(plan, projects)]),
        collapsed("Snapshot keys", keys_table(plan, projects)),
    ]
    return "\n\n".join(filter(None, parts))


def made_table(builds: Mapping[Folder, Build]) -> str:
    """The build that made each folder: one row per platform and build, with its steps."""
    steps: dict[tuple[str, Build], list[str]] = {}
    for f, b in sorted(builds.items()):
        steps.setdefault((f.platform, b), []).append(f.step)
    rows = [[code(platform), codes(ids), build_link(b)] for (platform, b), ids in steps.items()]
    return table(["Platform", "Steps", "Made by"], rows)


def reused_annotation(plan: Plan, project: str, builds: Mapping[Folder, Build]) -> str:
    """A build that builds nothing: the build that made each folder it was asked for."""
    heading = f"`{project}` is reused: every step it was asked for is made, so nothing is built"
    return f"**{heading}**\n\n{made_table(builds)}\n\n{plan_annotation(plan, project, ())}"


# ---------------------------------------------------------------------------
# A step that failed before anything was built
# ---------------------------------------------------------------------------

# The kinds of refusal that are not a reason of a record.
PLAN_REFUSED = "plan"
PLAN_STEP_ERROR, LAST_STEP_ERROR = "plan_step_error", "last_step_error"

REFUSAL_HEADINGS = {
    RULE: "The generated pipeline of `{project}` build {number} breaks the rule of its templates",
    NOT_DECLARED: "`{project}` build {number} was asked for a step it does not make",
    PLAN_REFUSED: "The snapshot plan of `{project}` build {number} cannot be made",
    PLAN_STEP_ERROR: "The plan step of `{project}` build {number} failed",
    LAST_STEP_ERROR: "The last step of `{project}` build {number} failed",
}


def refusal_annotation(kind: str, project: str, build: Build, message: str) -> str:
    """``kind`` is a reason of a record, ``PLAN_REFUSED`` or a step's error; ``message`` holds
    the facts, one per line."""
    heading = REFUSAL_HEADINGS[kind].format(project=project, number=build.number)
    return f"{heading}\n\n{fenced(message.splitlines())}"


# ---------------------------------------------------------------------------
# A failure, followed down its causes to the builds that failed
# ---------------------------------------------------------------------------

REASON_TEXTS = {
    RULE: "its generated pipeline breaks the rule of its templates",
    NOT_DECLARED: "it was asked for a step it does not make",
}


def which_build(plan: Plan, c: Cause) -> str:
    """A dependency as the plan shown on the same page names it; its key is there."""
    n = plan.projects.get(c.project)
    return f"{c.project} @ {short_commit(n.commit)}" if n else f"{c.project} (key {c.key})"


def remedies(project: str, makes: Mapping[str, Sequence[str]]) -> str:
    """How the entry that asked ``project`` for steps it refused gets what it needs, ``makes``
    being the step ids it makes on each refused platform: the step lists to write, one per
    platform unless some ids are made on all (an empty list would take every step); and the
    download to remove on a platform that makes none."""

    def step_list(ids: Iterable[str]) -> str:
        return f"step: [{', '.join(sorted(ids))}]"

    made = {p: frozenset(ids) for p, ids in makes.items() if ids}
    none = [p for p, ids in makes.items() if not ids]
    common = frozenset.intersection(*made.values()) if made else frozenset()
    lists = (
        step_list(common)
        if common
        else ", ".join(f"{step_list(i)} on {p}" for p, i in made.items())
    )
    write = [f"Write in it the steps it needs, as {lists}."] if made else []
    remove = [f"Remove the download of {project} on {', '.join(none)}."] if none else []
    return " ".join([*write, *remove])


def refused_line(plan: Plan, c: Cause, r: Record) -> str:
    """A dependency whose build ``r`` refused steps it does not make: what it does not make,
    what it makes on that platform, and the entry to change."""
    makes = {x.folder.platform: x.promotes for x in r.refused}
    made = "; ".join(f"for {p} it makes {', '.join(ids) or 'nothing'}" for p, ids in makes.items())
    takes = "every step its templates name" if c.takes is None else ", ".join(c.takes)
    return (
        f"{which_build(plan, c)} does not make {shown_folders(sorted(x.folder for x in r.refused))}; "
        f"{made}. The entry of {c.project} in {c.template} takes {takes}. "
        f"{remedies(c.project, makes)}"
    )


def left_no_record(plan: Plan, c: Cause) -> str:
    """A dependency whose build ended without the folders a build needs and said nothing."""
    return (
        f"the build started for {c.project} ended without {shown_folders(c.missing)} and left "
        f"no record; see the step {trigger_label(plan, c.project)}"
    )


def cause_line(plan: Plan, c: Cause, r: Record | None, nested: bool) -> str:
    if c.template is not None and r is not None:
        return ("caused by " if nested else "") + refused_line(plan, c, r)
    if nested:
        return f"caused by {which_build(plan, c)}, which has no {shown_folders(c.missing)}."
    return f"{which_build(plan, c)} has no {shown_folders(c.missing)}."


SHARED = "build {number} of the same snapshot failed after the root build of this plan started."


def failed_in(project: str, b: Build) -> str:
    return f"FAILED in {project} build {b.number}: {b.url}"


def failed_steps(r: Record) -> list[str]:
    return [f"{f.folder}: step {f.step_key}: {f.outcome or 'did not finish'}" for f in r.failed]


def failure_lines(
    plan: Plan, r: Record, records: Mapping[str, Record]
) -> tuple[list[str], list[tuple[str, Build]]]:
    """What the record ``r`` says, each cause followed down to the builds that failed; and
    the builds whose failed job a person retries, each with its pipeline. Many ways can lead
    to one record, which is shown once."""
    lines: list[str] = []
    retry: list[tuple[str, Build]] = []
    shown = {r.key}

    def say(depth: int, *texts: str) -> None:
        lines.extend(INDENT * depth + t for t in texts)

    def record(r: Record, depth: int) -> None:
        """Its reason, which no retry cures; its failed steps, or the build it shares them
        with; and each of its causes."""
        failed = failed_steps(r)
        if r.reason is not None:
            say(depth, REASON_TEXTS[r.reason])
        if r.shared_with is not None:
            say(depth, SHARED.format(number=r.shared_with.number))
            say(depth + 1, failed_in(r.project, r.shared_with))
            say(depth + 2, *failed)
            retry.append((r.project, r.shared_with))
        elif failed:
            say(depth, *failed)
            retry.append((r.project, r.build))
        for c in r.caused_by:
            cause(c, depth)

    def cause(c: Cause, depth: int) -> None:
        found = records.get(c.key)
        say(depth, cause_line(plan, c, found, nested=depth > 1))
        if c.key in shown:
            say(depth + 1, "its record is shown above")
        elif found is None:
            say(depth + 1, left_no_record(plan, c))
        else:
            shown.add(c.key)
            say(depth + 1, failed_in(found.project, found.build))
            record(found, depth + 2)

    record(r, 1)
    return lines, retry


def retry_sentence(builds: Iterable[tuple[str, Build]], root: str) -> str:
    """How to retry: each failed job, then a new build of ``root``; with no job to retry, a
    new build once the cause is fixed."""
    jobs = list(dict.fromkeys(f"`{pipeline}` `{b.number}`" for pipeline, b in builds))
    reused = "Everything that passed is reused."
    if not jobs:
        return f"To retry: start a new build of `{root}` when the cause is fixed. {reused}"
    named = jobs[0] if len(jobs) == 1 else f"{', '.join(jobs[:-1])} and {jobs[-1]}"
    one = len(jobs) == 1
    return (
        f"To retry: retry the failed job{'' if one else 's'} in {named}. When "
        f"{'it passes' if one else 'they pass'}, start a new build of `{root}`. {reused}"
    )


def failure_annotation(plan: Plan, r: Record, records: Mapping[str, Record]) -> str:
    """The failure of the build that wrote ``r``. ``records`` holds, by key, the record of this
    plan for each key a record names as a cause."""
    if r.caused_by:
        title = "Snapshot dependency failed"
    elif r.shared_with is not None:
        title = "Snapshot failed in another build of it during this plan"
    else:
        title = "Snapshot failed"
    # A build whose own steps failed did not give its snapshot; any other was not built.
    built = r.reason is None and not r.caused_by and r.shared_with is None
    why = "did not give its snapshot:" if built else "was not built, because:"
    head = f"{r.project} build {r.build.number} {why}"
    lines, retry = failure_lines(plan, r, records)
    return f"{title}\n\n{fenced([head, *lines])}\n\n{retry_sentence(retry, plan.root.project)}"
