"""The Markdown of an annotation, as Buildkite renders it: a code span, a table, a block that
opens on a click.

This is job code, as the forms are: nothing here imports ``planning``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from .job import Build


def code(text: object) -> str:
    """``text`` as a code span, which shows the Markdown characters of a branch name or a path
    as they are. A pipe is escaped: in a table it would end the cell."""
    return "`" + str(text).replace("|", "\\|") + "`"


def codes(texts: Iterable[object]) -> str:
    """Each of ``texts`` as a code span, side by side; a long cell wraps between them."""
    return ", ".join(map(code, texts))


def build_link(b: Build) -> str:
    return f"[build {b.number}]({b.url})"


def table(headers: Sequence[str], rows: Iterable[Sequence[str]]) -> str:
    def line(cells: Sequence[str]) -> str:
        return "| " + " | ".join(cells) + " |"

    return "\n".join([line(headers), line(["---"] * len(headers)), *map(line, rows)])


def collapsed(summary: str, body: str) -> str:
    """``body`` behind the line ``summary``, closed until a person opens it. The blank lines
    make ``body`` Markdown inside the HTML block."""
    return f"<details><summary>{summary}</summary>\n\n{body}\n\n</details>"
