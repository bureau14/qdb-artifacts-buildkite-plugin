"""What a gate step shows on the build page.

Text only: the caller sends it with ``Agent.annotate``. Three kinds:

- the plan (``plan_annotation``): the tree of a node, each node with its
  commit, ref, key and whether it writes its branch's latest files, and
  each download of the node's own project at another ref, which its key
  leaves out; for a build that is reused, also the builds that made its
  steps (``reused_annotation``);
- a refusal or a plan error (``message_annotation``, ``refusal_annotation``):
  what failed before anything was built, and the message's plain lines;
- a failure (``failure_annotation``): a failure record, followed down the
  records of its causes to the builds that failed, and how to retry; a
  dependency that refused steps it does not make is shown with the entry to
  change (``refused_line``); ``caused_annotation`` is the same text for a
  build that dependencies left without steps it needs.

A step of a platform is shown as its folder, ``<platform>/<step id>``.

Everything that is not a heading or the closing sentence is in a fenced code
block, because template paths and branch names hold Markdown characters.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from ..forms import name_of
from ..layout import Folder
from ..manifest import Build
from .plan import LeftOut, Plan, short_commit, trigger_label
from .records import GENERATOR_FAILED, NOT_DECLARED, RULE, Cause, Record

INDENT = "  "


def fenced(lines: Sequence[str]) -> str:
    """``lines`` as a fenced code block."""
    return "```\n" + "\n".join(lines) + "\n```"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def plan_heading(plan: Plan, project: str) -> str:
    first = plan.first
    if project == first.project:
        return f"Snapshot plan of `{first.project}` build {first.build_number}"
    return (
        f"Snapshot plan of `{project}`, in the chain of `{first.project}` build "
        f"{first.build_number}"
    )


def node_line(plan: Plan, project: str, depth: int) -> str:
    """One project: its commit, ref and key, and whether a build of it writes its branch's
    latest files."""
    n = plan.projects[project]
    written = (
        f"writes the latest files of {name_of(n.ref)}"
        if n.writes_latest_file
        else "writes no latest file"
    )
    return f"{INDENT * depth}{project} @ {short_commit(n.commit)} {n.ref} {n.key} {written}"


def tree_lines(plan: Plan, project: str) -> list[str]:
    """The tree of ``project``, depth first; a node shown before is named, not repeated."""
    lines: list[str] = []
    shown: set[str] = set()

    def visit(p: str, depth: int) -> None:
        if p in shown:
            lines.append(f"{INDENT * depth}{p} (above)")
            return
        shown.add(p)
        lines.append(node_line(plan, p, depth))
        for d in plan.projects[p].deps:
            visit(d, depth + 1)

    visit(project, 0)
    return lines


def outside_line(project: str, o: LeftOut) -> str:
    """A download of the build's own project at another ref, which the key leaves out."""
    return (
        f"not in the key: step {o.step} takes {project} {o.platform} of branch "
        f"{name_of(o.ref)}; its job reads that branch's latest build when it runs"
    )


def plan_annotation(plan: Plan, project: str, outside: Sequence[LeftOut]) -> str:
    """The plan as ``project`` builds it: the root's name, which Buildkite's branch field holds
    for a branch and a tag alike, and its rebuild setting; its tree; and each download of
    ``outside``, of the project's own at another ref."""
    context = f"branch {plan.context}, rebuild {plan.rebuild}"
    left_out = [outside_line(project, o) for o in outside]
    lines = [context, *tree_lines(plan, project), *left_out]
    return f"{plan_heading(plan, project)}\n\n{fenced(lines)}"


def reused_annotation(plan: Plan, project: str, builds: Mapping[Folder, Build]) -> str:
    """A build that is reused: the build that made each step it was asked for, and the plan.

    ``builds`` maps each folder to the build its manifest says promoted it.
    """
    made = [f"{f} made by build {b.number} {b.url}" for f, b in sorted(builds.items())]
    heading = f"`{project}` is reused: every step it was asked for is made, so nothing is built"
    return f"{heading}\n\n{fenced(made)}\n\n{plan_annotation(plan, project, ())}"


# ---------------------------------------------------------------------------
# A refusal or a plan error
# ---------------------------------------------------------------------------


def message_annotation(heading: str, message: str) -> str:
    """``heading``, and the plain lines of ``message`` in a fenced code block."""
    return f"{heading}\n\n{fenced(message.splitlines())}"


# The kinds of refusal that are not a reason of a record.
PLAN_REFUSED = "plan"
STEP_ERROR = "error"

REFUSAL_HEADINGS = {
    GENERATOR_FAILED: "The pipeline generator of `{project}` build {number} failed",
    RULE: "The generated pipeline of `{project}` build {number} breaks the rule of its templates",
    NOT_DECLARED: "`{project}` build {number} was asked for a step it does not make",
    PLAN_REFUSED: "The snapshot plan of `{project}` build {number} cannot be made",
    STEP_ERROR: "The snapshot step of `{project}` build {number} failed",
}


def refusal_annotation(kind: str, project: str, build: Build, message: str) -> str:
    """A gate step that failed before anything was built: what of the build, and why.

    ``kind`` is the reason of a record (``GENERATOR_FAILED``, ``RULE``,
    ``NOT_DECLARED``), ``PLAN_REFUSED`` or ``STEP_ERROR``; ``message`` holds
    the facts, one per line.
    """
    heading = REFUSAL_HEADINGS[kind].format(project=project, number=build.number)
    return message_annotation(heading, message)


# ---------------------------------------------------------------------------
# A failure
# ---------------------------------------------------------------------------

REASON_TEXTS = {
    GENERATOR_FAILED: "its pipeline generator failed",
    RULE: "its generated pipeline breaks the rule of its templates",
    NOT_DECLARED: "it was asked for a step it does not make",
}

# Each part of a failure below gives its lines, the builds whose failed job a
# person retries (each with its pipeline, the project of the record that names
# it), and the keys whose record is shown so far. A record names its causes,
# whose records name theirs: the three parts call each other, down to the
# builds that failed. Records of one chain form a graph in which many ways can
# lead to one record, so a record is shown once and named after.


def entry_lines(r: Record, depth: int) -> list[str]:
    return [
        f"{INDENT * depth}{f.folder}: step {f.step_key}: {f.outcome or 'did not finish'}"
        for f in r.failed
    ]


def shown_folders(folders: Iterable[Folder]) -> str:
    return ", ".join(map(str, folders))


def which_build(plan: Plan, c: Cause) -> str:
    """The build of a cause's dependency: its project, commit and key."""
    n = plan.projects.get(c.project)
    commit = short_commit(n.commit) if n is not None else "a commit not in the plan"
    return f"{c.project} @ {commit} ({c.key})"


def step_list(ids: Iterable[str]) -> str:
    """``ids`` as an entry writes them."""
    return f"step: [{', '.join(sorted(ids))}]"


def lists_to_write(made: Mapping[str, frozenset[str]]) -> str:
    """What an entry writes to take only steps its dependency makes, ``made`` being the step
    ids of each platform that makes any: the ids made on all of them, else one list per
    platform, since an empty list reads as no list, which takes every step."""
    common = frozenset.intersection(*made.values())
    if common:
        return step_list(common)
    return ", ".join(f"{step_list(ids)} on {p}" for p, ids in made.items())


def remedies(project: str, makes: Mapping[str, Sequence[str]]) -> list[str]:
    """How the entry that asked ``project`` for steps it refused gets what it needs, ``makes``
    being the step ids it makes on each refused platform: the lists to write, for the
    platforms that make some; the download goes on a platform that makes none."""
    made = {p: frozenset(ids) for p, ids in makes.items() if ids}
    none = [p for p, ids in makes.items() if not ids]
    write = [f"Write in it the steps it needs, as {lists_to_write(made)}."] if made else []
    remove = [f"Remove the download of {project} on {', '.join(none)}."] if none else []
    return [*write, *remove]


def refused_line(plan: Plan, c: Cause, r: Record) -> str:
    """What leads to the remedy of a dependency whose build ``r`` refused steps it does not
    make: what it does not make, what it makes on that platform, and the entry to change."""
    folders = shown_folders(sorted(x.folder for x in r.refused))
    makes = {x.folder.platform: x.promotes for x in r.refused}
    made = "; ".join(f"for {p} it makes {', '.join(ids) or 'nothing'}" for p, ids in makes.items())
    takes = "every step its templates name" if c.takes is None else ", ".join(c.takes)
    return (
        f"{which_build(plan, c)} does not make {folders}; {made}. The entry of {c.project} in "
        f"{c.template} takes {takes}. {' '.join(remedies(c.project, makes))}"
    )


def cause_line(plan: Plan, c: Cause, depth: int, nested: bool) -> str:
    which = which_build(plan, c)
    missing = shown_folders(c.missing)
    if nested:
        return f"{INDENT * depth}caused by {which}, which has no {missing}."
    return f"{INDENT * depth}{which} has no {missing}."


Part = tuple[list[str], list[tuple[str, Build]], frozenset[str]]


def cause_part(
    plan: Plan,
    c: Cause,
    records: Mapping[str, Record],
    depth: int,
    shown: frozenset[str],
    nested: bool,
) -> Part:
    """A cause, and the record of its key followed down; a record shown before is named. A
    cause whose dependency refused steps shows what leads to the remedy."""
    r = records.get(c.key)
    if c.template is not None and r is not None:
        line = f"{INDENT * depth}{'caused by ' if nested else ''}{refused_line(plan, c, r)}"
    else:
        line = cause_line(plan, c, depth, nested)
    below = INDENT * (depth + 1)
    if c.key in shown:
        return [line, f"{below}its record is shown above"], [], shown
    if r is None:
        return [line, f"{below}it left no record"], [], shown
    lines, retry, shown = failure_part(plan, r, records, depth + 1, shown | {c.key})
    return [line, *lines], retry, shown


def content_part(
    plan: Plan,
    r: Record,
    records: Mapping[str, Record],
    depth: int,
    shown: frozenset[str],
    nested: bool,
) -> Part:
    """What a record says, each of its causes followed down to the build that failed.

    Its reason, which no retry of the build cures, so it names no build to
    retry; its failed steps, or the build it shares them with; its causes.
    """
    pad = INDENT * depth
    lines: list[str] = []
    retry: list[tuple[str, Build]] = []
    if r.reason is not None:
        lines.append(pad + REASON_TEXTS[r.reason])
    if r.shared_with is not None:
        s = r.shared_with
        lines.append(
            f"{pad}build {s.number} of the same snapshot failed after the root build of this "
            "chain started."
        )
        lines.append(f"{pad}{INDENT}FAILED in {r.project} build {s.number}: {s.url}")
        lines.extend(entry_lines(r, depth + 2))
        retry.append((r.project, s))
    elif r.failed:
        lines.extend(entry_lines(r, depth))
        retry.append((r.project, r.build))
    for c in r.caused_by:
        more, again, shown = cause_part(plan, c, records, depth, shown, nested)
        lines.extend(more)
        retry.extend(again)
    return lines, retry, shown


def failure_part(
    plan: Plan, r: Record, records: Mapping[str, Record], depth: int, shown: frozenset[str]
) -> Part:
    """The record of a dependency's build: where it failed, and what its record says."""
    b = r.build
    head = f"{INDENT * depth}FAILED in {r.project} build {b.number}: {b.url}"
    lines, retry, shown = content_part(plan, r, records, depth + 1, shown, nested=True)
    return [head, *lines], retry, shown


def and_list(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def retry_sentence(builds: Iterable[tuple[str, Build]], root: str) -> str:
    """The closing sentence of a failure: retry each failed job, then start a new build.

    ``builds`` are the builds to retry, each with its pipeline; none, when no
    job failed or a retry cures nothing, says to start a new build once the
    cause is fixed.
    """
    jobs = list(dict.fromkeys(f"`{pipeline}` `{b.number}`" for pipeline, b in builds))
    reused = "Everything that passed is reused."
    if not jobs:
        return f"To retry: start a new build of `{root}` when the cause is fixed. {reused}"
    if len(jobs) == 1:
        return (
            f"To retry: retry the failed job in {jobs[0]}. When it passes, start a new build of "
            f"`{root}`. {reused}"
        )
    return (
        f"To retry: retry the failed jobs in {and_list(jobs)}. When they pass, start a new build "
        f"of `{root}`. {reused}"
    )


DEPENDENCY_FAILED = "Snapshot dependency failed"


def failure_title(r: Record) -> str:
    if r.caused_by:
        return DEPENDENCY_FAILED
    if r.shared_with is not None:
        return "Snapshot failed in another build of it during this chain"
    return "Snapshot failed"


def head_line(project: str, build: Build, built: bool) -> str:
    why = "did not give its snapshot:" if built else "was not built, because:"
    return f"{project} build {build.number} {why}"


def failure_head(r: Record) -> str:
    """A build whose own steps failed did not give its snapshot; any other was not built."""
    built = r.reason is None and not r.caused_by and r.shared_with is None
    return head_line(r.project, r.build, built)


def failure_annotation(plan: Plan, record: Record, records: Mapping[str, Record]) -> str:
    """The failure of the build that wrote ``record``, followed down to the builds that failed.

    ``records`` holds, by key, the record of this chain the caller read for
    each key a record names as a cause. The annotation ends with how to retry.
    """
    head = failure_head(record)
    lines, retry, _ = content_part(plan, record, records, 1, frozenset({record.key}), nested=False)
    closing = retry_sentence(retry, plan.first.project)
    return f"{failure_title(record)}\n\n{fenced([head, *lines])}\n\n{closing}"


# ---------------------------------------------------------------------------
# A build that dependencies of its chain left without what it needs
# ---------------------------------------------------------------------------


def left_no_record(plan: Plan, c: Cause) -> str:
    """A dependency this build triggered whose build ended without the steps and said nothing."""
    return (
        f"the build of {c.project} triggered by this build ended without "
        f"{shown_folders(c.missing)} and left no record; see the step "
        f"{trigger_label(plan, c.project)}"
    )


def caused_annotation(
    plan: Plan, project: str, build: Build, causes: Sequence[Cause], records: Mapping[str, Record]
) -> str:
    """The failure of a build of ``project`` that dependencies left without steps it needs.

    ``causes`` name each dependency, its key and the folders it lacks;
    ``records`` holds, by key, the record of this chain the caller read for
    each key a cause names and for the keys those records name. A cause with
    a record is followed down as by ``failure_annotation``; one without is a
    build this build triggered that left nothing to follow, and the line
    names the trigger step.
    """
    lines: list[str] = []
    retry: list[tuple[str, Build]] = []
    shown: frozenset[str] = frozenset()
    for c in causes:
        if c.key in records:
            more, again, shown = cause_part(plan, c, records, 1, shown, nested=False)
        else:
            more, again = [cause_line(plan, c, 1, False), INDENT * 2 + left_no_record(plan, c)], []
        lines.extend(more)
        retry.extend(again)
    head = head_line(project, build, built=False)
    closing = retry_sentence(retry, plan.first.project)
    return f"{DEPENDENCY_FAILED}\n\n{fenced([head, *lines])}\n\n{closing}"
