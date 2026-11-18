"""Foreign values and texts in messages: short, ASCII, one line, whatever they are."""

from __future__ import annotations

import pytest

from lib.snapshot.shown import TEXT_MAX, VALUE_MAX, shown_text, shown_value


def is_one_ascii_line(s: str) -> bool:
    return all(" " <= c <= "~" for c in s)


def test_a_short_plain_value_is_shown_as_python_writes_it():
    assert shown_value("quasardb-build") == "'quasardb-build'"
    assert shown_value(7) == "7"
    assert shown_value(None) == "None"
    assert shown_value(["quasardb-build"]) == "['quasardb-build']"


def shared_tree(depth: int) -> list:
    """A list of 10**depth leaves made of depth + 1 objects, as YAML aliases parse."""
    tree: list = [1]
    for _ in range(depth):
        tree = [tree] * 10
    return tree


@pytest.mark.parametrize(
    "value",
    [
        "x" * 1_000_000,
        shared_tree(6),
        {f"k{i}": "v" * 1000 for i in range(1000)},
        10**20_000,
        b"\0" * 100_000,
    ],
    ids=["a long string", "a shared tree", "a large mapping", "a large integer", "bytes"],
)
def test_a_large_value_is_shown_short(value):
    s = shown_value(value)
    assert len(s) <= VALUE_MAX and is_one_ascii_line(s), s


@pytest.mark.parametrize(
    "value",
    [
        "a\nb: forged",
        "\x1b[31mred",
        "caf" + chr(0xE9),
        "\udce9",
        chr(0x202E) + "lmy.",
        "[click](https://evil.example)\r\n",
    ],
    ids=["a newline", "an escape sequence", "not ascii", "a surrogate", "a direction mark", "crlf"],
)
def test_a_value_is_shown_as_one_line_of_ascii(value):
    s = shown_value(value)
    assert is_one_ascii_line(s), s
    s.encode("ascii")


def test_a_text_is_shown_without_quotes_as_one_line_of_ascii():
    assert shown_text("expected <block end>, but found '-'") == (
        "expected <block end>, but found '-'"
    )
    assert shown_text("a\nb \x1b caf" + chr(0xE9)) == "a\\nb \\x1b caf\\xe9"
    long = shown_text("y" * 10_000)
    assert len(long) <= TEXT_MAX and long.endswith("...")
