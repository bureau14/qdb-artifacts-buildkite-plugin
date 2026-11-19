"""What a gate step shows on the build page (specification sections 4, 7 and 9).

Text only: the caller sends it with ``Agent.annotate``. Three kinds:

- the plan (``plan_annotation``): the tree of a node, each node with its
  commit, ref, key and label; for a build that is reused, also the builds
  that hold its variants (``reused_annotation``);
- a refusal or a plan error (``message_annotation``, ``refusal_annotation``):
  what failed before anything was built, and the message's plain lines;
- a failure (``failure_annotation``): a failure record, followed down the
  records of its causes to the builds that failed, and how to retry;
  ``caused_annotation`` is the same text for a build that dependencies left
  without variants, also one without a record of its own.

Everything that is not a heading or the closing sentence is in a fenced code
block, because template paths and branch names hold Markdown characters; the
fence is longer than any run of backticks in the block, so no text closes it
early. A value of a plan or a record that is free text is shown in ASCII
(``shown_text``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from ..pointer import Build
from .plan import Plan, trigger_label
from .records import GENERATOR_FAILED, NOT_DECLARED, RULE, Cause, Record
from .shown import short_commit, shown_text

INDENT = "  "


def longest_backticks(text: str) -> int:
    """The length of the longest run of backticks in ``text``."""
    longest = run = 0
    for c in text:
        run = run + 1 if c == "`" else 0
        longest = max(longest, run)
    return longest


def fenced(lines: Sequence[str]) -> str:
    """``lines`` as a fenced code block whose fence no run of backticks in them closes."""
    text = "\n".join(lines)
    fence = "`" * max(3, longest_backticks(text) + 1)
    return f"{fence}\n{text}\n{fence}"


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------


def plan_heading(plan: Plan, project: str) -> str:
    root = plan.root
    if project == root.project:
        return f"Snapshot plan of `{root.project}` build {root.build_number}"
    return (
        f"Snapshot plan of `{project}`, in the chain of `{root.project}` build {root.build_number}"
    )


def node_line(plan: Plan, project: str, depth: int) -> str:
    """One node: its commit, ref and key, and the branch a build of it runs under."""
    n = plan.nodes[project]
    key = n.key if n.key is not None else "no snapshot"
    at = f"{short_commit(n.commit)} {shown_text(n.ref)}"
    return f"{INDENT * depth}{project} @ {at} {key} runs as {shown_text(n.label)}"


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
        for d in plan.nodes[p].deps:
            visit(d, depth + 1)

    visit(project, 0)
    return lines


def plan_annotation(plan: Plan, project: str) -> str:
    """The plan as ``project`` builds it: the root's branch and rebuild setting, and its tree."""
    context = f"branch {shown_text(plan.context)}, rebuild {plan.rebuild}"
    return f"{plan_heading(plan, project)}\n\n{fenced([context, *tree_lines(plan, project)])}"


def reused_annotation(plan: Plan, project: str, builds: Mapping[str, Build]) -> str:
    """A build that is reused: the build that holds each variant it was asked for, and the plan.

    ``builds`` maps each variant to the build its pointer names.
    """
    held = [f"{v} in build {b.number} {shown_text(b.url)}" for v, b in sorted(builds.items())]
    heading = f"`{project}` is reused: every variant it was asked for exists, so nothing is built"
    return f"{heading}\n\n{fenced(held)}\n\n{plan_annotation(plan, project)}"


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
    NOT_DECLARED: "`{project}` build {number} was asked for a variant it does not promote",
    PLAN_REFUSED: "The snapshot plan of `{project}` build {number} cannot be made",
    STEP_ERROR: "The snapshot step of `{project}` build {number} failed",
}


def refusal_annotation(kind: str, project: str, build: Build, message: str) -> str:
    """A gate step that failed before anything was built: what of the build, and why.

    ``kind`` is a reason of ``records.REASONS``, ``PLAN_REFUSED`` or
    ``STEP_ERROR``; ``message`` holds the facts, one per line.
    """
    heading = REFUSAL_HEADINGS[kind].format(project=project, number=build.number)
    return message_annotation(heading, message)


# ---------------------------------------------------------------------------
# A failure
# ---------------------------------------------------------------------------

REASON_TEXTS = {
    GENERATOR_FAILED: "its pipeline generator failed",
    RULE: "its generated pipeline breaks the rule of its templates",
    NOT_DECLARED: "it was asked for a variant it does not promote",
}

# Each part of a failure below gives its lines, the builds whose failed job a
# person retries (each with its pipeline, the project of the record that names
# it), and the keys whose record is shown so far. A record names its causes,
# whose records name theirs: the three parts call each other, down to the
# builds that failed. Records of one chain form a graph in which many ways can
# lead to one record, so a record is shown once and named after.


def entry_lines(r: Record, depth: int) -> list[str]:
    return [
        f"{INDENT * depth}{shown_text(f.variant)}: step {shown_text(f.step)}: "
        f"{shown_text(f.outcome) or 'did not finish'}"
        for f in r.failed
    ]


def cause_line(plan: Plan, c: Cause, depth: int, nested: bool) -> str:
    node = plan.nodes.get(c.project)
    commit = short_commit(node.commit) if node is not None else "a commit not in the plan"
    which = f"{c.project} @ {commit} ({shown_text(c.key)})"
    variants = ", ".join(shown_text(v) for v in c.variants)
    if nested:
        return f"{INDENT * depth}caused by {which}, which has no {variants}."
    return f"{INDENT * depth}{which} has no {variants}."


Part = tuple[list[str], list[tuple[str, Build]], frozenset[str]]


def cause_part(
    plan: Plan,
    c: Cause,
    records: Mapping[str, Record],
    depth: int,
    shown: frozenset[str],
    nested: bool,
) -> Part:
    """A cause, and the record of its key followed down; a record shown before is named."""
    line = cause_line(plan, c, depth, nested)
    below = INDENT * (depth + 1)
    if c.key in shown:
        return [line, f"{below}its record is shown above"], [], shown
    r = records.get(c.key)
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

    Its reason; its failed variants, or the build it shares them with; its causes.
    """
    pad = INDENT * depth
    lines: list[str] = []
    retry: list[tuple[str, Build]] = []
    if r.reason is not None:
        lines.append(pad + REASON_TEXTS[r.reason])
        retry.append((r.project, r.build))
    if r.shared_with is not None:
        s = r.shared_with
        lines.append(
            f"{pad}build {s.number} of the same snapshot failed while this build waited behind it."
        )
        lines.append(f"{pad}{INDENT}FAILED in {r.project} build {s.number}: {shown_text(s.url)}")
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
    head = f"{INDENT * depth}FAILED in {r.project} build {b.number}: {shown_text(b.url)}"
    lines, retry, shown = content_part(plan, r, records, depth + 1, shown, nested=True)
    return [head, *lines], retry, shown


def and_list(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def retry_sentence(builds: Iterable[tuple[str, Build]], root: str) -> str:
    """The closing sentence of section 9: retry each failed job, then start a new build.

    ``builds`` are the builds to retry, each with its pipeline.
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
        return "Snapshot failed in the build this one waited behind"
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
    closing = retry_sentence(retry, plan.root.project)
    return f"{failure_title(record)}\n\n{fenced([head, *lines])}\n\n{closing}"


# ---------------------------------------------------------------------------
# A build that dependencies of its chain left without what it needs
# ---------------------------------------------------------------------------


def left_no_record(plan: Plan, c: Cause) -> str:
    """A dependency this build triggered whose build ended without the variants and said nothing."""
    variants = ", ".join(shown_text(v) for v in c.variants)
    return (
        f"the build of {c.project} triggered by this build ended without {variants} and left "
        f"no record; see the step {trigger_label(plan, c.project)}"
    )


def caused_annotation(
    plan: Plan, project: str, build: Build, causes: Sequence[Cause], records: Mapping[str, Record]
) -> str:
    """The failure of a build of ``project`` that dependencies left without variants it needs.

    ``causes`` name each dependency, its key and the variants it lacks;
    ``records`` holds, by key, the record of this chain the caller read for
    each key a cause names and for the keys those records name. A cause with
    a record is followed down as by ``failure_annotation``; one without is a
    build this build triggered that left nothing to follow, and the line
    names the trigger step. A root without a snapshot writes no record, so
    the text is made from the causes and not from a record.
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
    closing = retry_sentence(retry, plan.root.project)
    return f"{DEPENDENCY_FAILED}\n\n{fenced([head, *lines])}\n\n{closing}"
