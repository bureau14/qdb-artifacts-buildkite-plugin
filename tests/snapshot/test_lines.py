"""The release line of a root build: the rule, over the fake, and over local repositories."""

from __future__ import annotations

import pytest
from fakes import FakeGit
from repos import clone

from lib.snapshot.git import RealGit
from lib.snapshot.lines import choose_release_line, is_release_line, line_rank, release_line

A = "a" * 40
B = "b" * 40
C = "c" * 40
URL = "https://github.com/bureau14/qdb-api-python.git"


@pytest.mark.parametrize(
    "branch, line",
    [
        ("master", True),
        ("main", True),
        ("3.14.x", True),
        ("10.0.x", True),
        ("3.14", False),
        ("3.14.1", False),
        ("3.x", False),
        ("v3.14.x", False),
        ("3.14.x-20990101-pinned", False),
        ("3.14.x\n", False),
        ("master2", False),
        ("feature/master", False),
    ],
)
def test_release_lines_are_named_master_main_or_digits_digits_x(branch, line):
    assert is_release_line(branch) is line


def test_the_line_with_the_most_recent_merge_base_wins():
    assert choose_release_line({"master": 100, "3.14.x": 200}) == "3.14.x"
    assert choose_release_line({"master": 300, "3.14.x": 200}) == "master"


def test_equal_times_prefer_master_then_main_then_the_newest_numbered_line():
    assert line_rank("master") > line_rank("main") > line_rank("3.14.x") > line_rank("3.13.x")
    assert line_rank("10.0.x") > line_rank("9.9.x")
    assert choose_release_line({"3.14.x": 5, "master": 5, "main": 5}) == "master"
    assert choose_release_line({"3.13.x": 5, "3.14.x": 5}) == "3.14.x"


def test_without_a_known_merge_base_the_line_is_master():
    assert choose_release_line({"3.14.x": None}) == "master"
    assert choose_release_line({}) == "master"


def test_a_release_line_is_its_own_line_and_asks_git_nothing():
    g = FakeGit({})
    assert release_line(g, URL, "3.14.x") == "3.14.x"
    assert g.calls == []


def test_only_branches_named_as_lines_are_asked_for_a_merge_base():
    refs = {
        (URL, "refs/heads/master"): A,
        (URL, "refs/heads/3.14.x"): B,
        (URL, "refs/heads/3.14.x-pin"): C,
        (URL, "refs/heads/feature"): C,
        (URL, "refs/tags/4.0.x"): C,
    }
    g = FakeGit(refs, times={A: 1, B: 2, C: 3})
    assert release_line(g, URL, "feature") == "3.14.x", "not the tag, not 3.14.x-pin"
    assert g.calls == [("branches", URL), ("merge_base_times", URL, (A, B))]


# ---------------------------------------------------------------------------
# Over local repositories (the fixture ``lines`` of conftest.py)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.usefixtures("isolated_git")


@pytest.mark.parametrize(
    "branch, line",
    [("from-3.14.x", "3.14.x"), ("from-master", "master"), ("3.14.x", "3.14.x")],
)
def test_release_line_by_the_most_recent_merge_base(lines, tmp_path, branch, line):
    c = clone(lines["url"], tmp_path / "checkout", branch)
    assert release_line(RealGit(cwd=str(c), protocols=("file",)), lines["url"], branch) == line


def test_a_shallow_checkout_falls_back_to_master(lines, tmp_path):
    c = clone(lines["url"], tmp_path / "checkout", "from-3.14.x", "--depth", "1")
    g = RealGit(cwd=str(c), protocols=("file",))
    assert release_line(g, lines["url"], "from-3.14.x") == "master"
