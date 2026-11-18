"""The shape rule against its vectors.

Every file under ``shape/has-shape/`` must have the runner's shape and every
file under ``shape/has-no-shape/`` must not. The two directories are a
copy of ``tests/shape/`` of qdb-cicd-tools, whose runner implements the same
rule, directory names included: the same files with the same bytes, named
``*.pysrc`` in place of ``*.py`` so that no linter or test runner of this
repository takes them for code. Each is given to the rule as bytes, as git
stores it; some differ from others only in bytes (a byte order mark, CRLF line
endings, a null byte).

``CASES`` below are decisions of this implementation that the shared vectors
do not hold yet; the runner's implementation gives each the same answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lib.snapshot.shape import has_runner_shape, shape_problem

VECTORS = Path(__file__).parent / "shape"
EXPECTED = {"has-shape": True, "has-no-shape": False}


def vectors() -> list[tuple[Path, bool]]:
    return [
        (path, expected)
        for directory, expected in EXPECTED.items()
        for path in sorted((VECTORS / directory).iterdir())
    ]


@pytest.mark.parametrize(
    "path, expected", vectors(), ids=[f"{p.parent.name}/{p.stem}" for p, _ in vectors()]
)
def test_vector(path, expected):
    source = path.read_bytes()
    assert has_runner_shape(source) is expected, shape_problem(source)


def test_the_vectors_are_two_directories_of_sources_kept_as_bytes():
    assert sorted(p.name for p in VECTORS.iterdir()) == sorted([".gitattributes", *EXPECTED])
    assert (VECTORS / ".gitattributes").read_text().splitlines()[-1] == "* -text"
    for directory in EXPECTED:
        files = list((VECTORS / directory).iterdir())
        assert files, directory
        assert all(p.suffix == ".pysrc" for p in files), directory
    assert (VECTORS / "has-no-shape" / "empty.pysrc").read_bytes() == b""


HEAD = "from qdb_pipeline import run\n\n\ndef generate_pipeline():\n    return None\n\n\n"
MAIN = 'if __name__ == "__main__":\n'
MINIMAL = HEAD + MAIN + "    run(generate_pipeline)\n"


def main_block(body: str) -> str:
    return HEAD + MAIN + f"    {body}\n"


IN_TRY = (
    HEAD + "try:\n    " + MAIN + "        run(generate_pipeline)\nexcept ImportError:\n    pass\n"
)


CASES = [
    (
        "an import inside if",
        MINIMAL.replace("from qdb_pipeline", "if True:\n    from qdb_pipeline"),
        False,
    ),
    ("an aliased run beside a plain one", "from qdb_pipeline import run as r\n" + MINIMAL, True),
    ("run()", main_block("run()"), False),
    ("a double-starred argument", main_block("run(generate_pipeline, **o)"), False),
    ("the argument as a keyword", main_block("run(generate_pipeline=generate_pipeline)"), False),
    ("comments only", "# a comment\n# and another\n", False),
    ("a trailing semicolon", main_block("run(generate_pipeline);"), True),
    ("two calls on one line", main_block("run(generate_pipeline); run(generate_pipeline)"), False),
    ("a trailing comma", main_block("run(generate_pipeline,)"), True),
    ("a run block and a main() block", MINIMAL + MAIN + "    main()\n", False),
    ("the only block inside try", IN_TRY, False),
    (
        "a nested block beside the top-level one",
        "if x:\n    " + MAIN + "        pass\n" + MINIMAL,
        True,
    ),
]


@pytest.mark.parametrize("source, expected", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_a_case_the_shared_vectors_lack(source, expected):
    data = source.encode("ascii")
    assert has_runner_shape(data) is expected, shape_problem(data)


BLOCK = 'if __name__ == "__main__": block'


@pytest.mark.parametrize(
    "source, reason",
    [
        ("if True\n", "does not parse as Python 3.9: SyntaxError"),
        ("x = '\0'\n", "does not parse"),
        ("match x:\n    case 1:\n        pass\n", "does not parse as Python 3.9"),
        ("", "does not import run from qdb_pipeline at module level without an alias"),
        ("from qdb_pipeline import run\n", 'has 0 if __name__ == "__main__": blocks'),
        (MINIMAL + MINIMAL, 'has 2 if __name__ == "__main__": blocks'),
        (MINIMAL + "else:\n    pass\n", f"has an elif or else branch on its {BLOCK}"),
        (
            MINIMAL + "    pass\n",
            f"does not hold exactly the call run(generate_pipeline) in its {BLOCK}",
        ),
        (main_block("run(other)"), "does not hold exactly the call run(generate_pipeline)"),
    ],
)
def test_the_reason_names_the_first_condition_that_fails(source, reason):
    assert reason in shape_problem(source.encode("ascii"))


@pytest.mark.parametrize(
    "source",
    [
        "x = " + chr(0x20AC) + "\n" + MINIMAL,
        "def " + chr(0xE9) * 100_000 + "(:\n",
        "x = '" + chr(0x1B) + "[31m\n",
    ],
    ids=["a character that is not ascii", "a long name", "an escape sequence"],
)
def test_the_reason_quotes_the_source_short_on_one_line_of_ascii(source):
    reason = shape_problem(source.encode("utf-8"))
    assert reason.startswith("does not parse as Python 3.9")
    assert len(reason) < 300 and all(" " <= c <= "~" for c in reason), reason[:300]


def test_a_source_with_the_shape_has_no_problem():
    assert shape_problem(MINIMAL.encode("ascii")) is None
    assert has_runner_shape(MINIMAL.encode("ascii"))


def test_the_source_is_parsed_never_executed(tmp_path):
    marker = tmp_path / "ran"
    source = f"open({str(marker)!r}, 'w').close()\nraise SystemExit(3)\n" + MINIMAL
    assert has_runner_shape(source.encode("utf-8"))
    assert not marker.exists()


def test_bytes_are_decoded_as_python_decodes_a_source_file():
    body = MINIMAL.encode("ascii")
    assert has_runner_shape(b"# -*- coding: latin-1 -*-\n# caf\xe9\n" + body)
    assert has_runner_shape(b"\xef\xbb\xbf" + body)
    assert not has_runner_shape(b"# caf\xe9\n" + body), "not UTF-8 and no declaration"


@pytest.mark.parametrize("coding", [b"rot13", b"hex", b"no-such-codec"])
def test_a_coding_line_that_names_no_text_encoding_has_no_shape(coding):
    source = b"# -*- coding: " + coding + b" -*-\n" + MINIMAL.encode("ascii")
    assert not has_runner_shape(source)
    assert shape_problem(source).startswith("does not parse as Python 3.9")


def test_deep_nesting_has_no_shape():
    source = "x = " + "(" * 100_000 + ")" * 100_000 + "\n" + MINIMAL
    assert not has_runner_shape(source.encode("ascii"))
