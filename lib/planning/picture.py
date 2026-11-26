"""The tree of a plan as a picture: an SVG that the plan step uploads and its page shows.

What is built first stands on the left and the build itself on the right; an arrow goes from a
project to each project that takes it. A project stands in the column of its depth, the longest
way from the build down to it, so every arrow points right and a wide tree grows downwards.
An arrow that crosses a column gets a place of its own there, and so runs behind no box.
A box has the colours of its project's state (``states``) and says it in a line.

The module measures no font: a box is as wide as the longest text needs at ``CHAR`` a letter.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple
from xml.sax.saxutils import escape

from ..forms import name_of, short_commit
from .plan import Plan, Planned, dependencies_first, tree_of
from .states import BUILDS, BUILT, FAILED, REUSED, State, said

# The artifact of the build that holds the picture.
PICTURE = "snapshot-plan.svg"

SANS = "system-ui, -apple-system, 'Segoe UI', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace"
INK, FAINT, REMARK, LINE = "#222222", "#666666", "#b35900", "#8a929b"
# Of each state: the fill of its box, its border, and its line of words.
COLOURS = {
    REUSED: ("#e6f4ea", "#3c9a5f", "#2a7a48"),
    BUILT: ("#e3eefc", "#5b8def", "#1f55b5"),
    BUILDS: ("#fff3d6", "#c98a00", "#8a5a00"),
    FAILED: ("#fde8e8", "#d64545", "#a12b2b"),
}

# Pixels. CHAR is the width of a letter of a name; a remark is set smaller.
CHAR, REMARK_CHAR, PADDING, NARROWEST = 7.6, 5.8, 24, 150
SHORT, TALL, PASSING = 60, 76, 12
BETWEEN_COLUMNS, BETWEEN_ROWS, MARGIN, LABELS, TIP = 76, 18, 8, 26, 2

# ---------------------------------------------------------------------------
# What a box says
# ---------------------------------------------------------------------------


def remark(plan: Plan, n: Planned) -> str:
    """The line under a project's commit, only for one that differs from the rest: its build
    does not become the latest of its branch, or it is taken from another branch or tag than
    the build's."""
    name = name_of(n.ref)
    if not n.writes_latest_file:
        return f"not the latest of {name}"
    return "" if name == plan.context else f"from {name}"


def box_width(plan: Plan, states: Mapping[str, State]) -> float:
    """One width for every box, so the columns line up."""
    names = max(map(len, states)) * CHAR
    lines = [x for p, s in states.items() for x in (said(s), remark(plan, plan.projects[p]))]
    return max(NARROWEST, names + PADDING, max(map(len, lines)) * REMARK_CHAR + PADDING)


# ---------------------------------------------------------------------------
# Columns
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """A place in a column: the box of ``project``; or, with no project, the arrow from
    ``taken`` to ``taker`` on its way through."""

    project: str | None
    taker: str = ""
    taken: str = ""


def depths(deps: Mapping[str, Sequence[str]], root: str) -> dict[str, int]:
    """The longest way from ``root`` down to each project of ``deps``, its tree."""
    found = {root: 0}
    # A project after every project that takes it, so its own depth is final when it is read.
    for p in reversed(dependencies_first(deps)):
        for d in deps[p]:
            found[d] = max(found.get(d, 0), found[p] + 1)
    return found


def columns(
    deps: Mapping[str, Sequence[str]], depth: Mapping[str, int], root: str
) -> list[list[Slot]]:
    """The slots of each depth, in the order in which the column before names them."""
    found: list[list[Slot]] = [[] for _ in range(max(depth.values()) + 1)]
    found[0].append(Slot(root))
    for k, column in enumerate(found[:-1]):
        for s in column:
            arrows = (
                [(s.project, d) for d in deps[s.project]] if s.project else [(s.taker, s.taken)]
            )
            for taker, taken in arrows:
                slot = Slot(taken) if depth[taken] == k + 1 else Slot(None, taker, taken)
                if slot not in found[k + 1]:
                    found[k + 1].append(slot)
    return found


def way(depth: Mapping[str, int], taker: str, taken: str) -> list[tuple[int, Slot]]:
    """The slots of the arrow from ``taken`` to ``taker``, each with its depth, left to right."""
    passed = [(k, Slot(None, taker, taken)) for k in range(depth[taken] - 1, depth[taker], -1)]
    return [(depth[taken], Slot(taken)), *passed, (depth[taker], Slot(taker))]


# ---------------------------------------------------------------------------
# Places on the canvas
# ---------------------------------------------------------------------------


class Rect(NamedTuple):
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def middle(self) -> float:
        return self.y + self.h / 2


def height(plan: Plan, s: Slot) -> int:
    if s.project is None:
        return PASSING
    return TALL if remark(plan, plan.projects[s.project]) else SHORT


def span(heights: Sequence[int]) -> int:
    return sum(heights) + BETWEEN_ROWS * (len(heights) - 1)


def placed(
    plan: Plan, found: Sequence[Sequence[Slot]], width: float
) -> dict[tuple[int, Slot], Rect]:
    """Where each slot lies: the deepest column on the left, every column centred on the
    tallest one."""
    heights = [[height(plan, s) for s in column] for column in found]
    tallest = max(map(span, heights))
    rects = {}
    for k, column in enumerate(found):
        x = MARGIN + (len(found) - 1 - k) * (width + BETWEEN_COLUMNS)
        y = LABELS + (tallest - span(heights[k])) / 2
        for s, h in zip(column, heights[k]):
            rects[k, s] = Rect(x, y, width, h)
            y += h + BETWEEN_ROWS
    return rects


# ---------------------------------------------------------------------------
# SVG
# ---------------------------------------------------------------------------


def text(
    r: Rect, down: float, words: str, family: str, size: float, colour: str, weight: int
) -> str:
    """``words`` centred over ``r``, their baseline ``down`` from its top."""
    return (
        f'<text x="{r.x + r.w / 2:.1f}" y="{r.y + down:.1f}" text-anchor="middle" '
        f'font-family="{family}" font-size="{size}" font-weight="{weight}" fill="{colour}">'
        f"{escape(words)}</text>"
    )


def box(plan: Plan, r: Rect, project: str, s: State, own: bool) -> str:
    """A project: its name, its commit, its state and its remark. The build's own has a strong
    border. The frame's class names the state and its reason."""
    n = plan.projects[project]
    noted = remark(plan, n)
    fill, edge, ink = COLOURS[s.kind]
    frame = (
        f'<rect class="{f"{s.kind} {s.reason}".strip()}" x="{r.x:.1f}" y="{r.y:.1f}" '
        f'width="{r.w:.1f}" height="{r.h}" rx="6" fill="{fill}" stroke="{edge}" '
        f'stroke-width="{2.5 if own else 1}"/>'
    )
    lines = [
        text(r, 18, project, SANS, 13, INK, 600),
        text(r, 35, short_commit(n.commit), MONO, 11.5, FAINT, 400),
        text(r, 51, said(s), SANS, 10.5, ink, 400),
        text(r, 67, noted, SANS, 10.5, REMARK, 400) if noted else "",
    ]
    return frame + "".join(lines)


def label(r: Rect, words: str) -> str:
    """``words`` over the column of ``r``, in the band above every column."""
    return text(Rect(r.x, 0, r.w, LABELS), 14, words, SANS, 11, FAINT, 400)


def arrow(rects: Sequence[Rect]) -> str:
    """From the right side of the first rect to the left side of the last: a curve between two
    columns, a straight line through each column that it crosses."""
    x, y = rects[0].right, rects[0].middle
    d = f"M{x:.1f},{y:.1f}"
    for i, r in enumerate(rects[1:], start=2):
        last = i == len(rects)
        bend, to = (x + r.x) / 2, r.x - (TIP if last else 0)
        d += f" C{bend:.1f},{y:.1f} {bend:.1f},{r.middle:.1f} {to:.1f},{r.middle:.1f}"
        x, y = r.right, r.middle
        d += "" if last else f" H{x:.1f}"
    return f'<path d="{d}" fill="none" stroke="{LINE}" stroke-width="1.3" marker-end="url(#tip)"/>'


TIP_MARKER = (
    '<defs><marker id="tip" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" '
    f'markerHeight="7" orient="auto"><path d="M0,0 L8,4 L0,8 z" fill="{LINE}"/></marker></defs>'
)


def svg(rects: Iterable[Rect], parts: Iterable[str]) -> str:
    """A document just large enough for ``rects``."""
    across = max(r.right for r in rects) + MARGIN
    down = max(r.y + r.h for r in rects) + MARGIN
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{across:.0f}" height="{down:.0f}" '
        f'viewBox="0 0 {across:.0f} {down:.0f}">{TIP_MARKER}{"".join(parts)}</svg>'
    )


def tree_picture(plan: Plan, project: str, states: Mapping[str, State]) -> str | None:
    """The tree of ``project`` in ``plan`` as an SVG document, each project in its state of
    ``states``; None for a project that takes no other, whose tree is itself."""
    deps = {p: plan.projects[p].deps for p in tree_of(plan, project)}
    if len(deps) == 1:
        return None
    depth = depths(deps, project)
    found = columns(deps, depth, project)
    rects = placed(plan, found, box_width(plan, states))
    labels = [
        label(rects[len(found) - 1, found[-1][0]], "built first"),
        label(rects[0, found[0][0]], "this build"),
    ]
    arrows = [
        arrow([rects[at] for at in way(depth, taker, taken)])
        for taker in deps
        for taken in deps[taker]
    ]
    boxes = [box(plan, rects[depth[p], Slot(p)], p, states[p], own=p == project) for p in deps]
    return svg(list(rects.values()), [*labels, *arrows, *boxes])
