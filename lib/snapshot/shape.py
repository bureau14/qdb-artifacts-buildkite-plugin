"""Does a ``.buildkite/pipeline.py`` hand its pipeline to the runner (specification section 3).

The source is foreign input: it is parsed with ``ast`` and never executed or
imported. The runner of ``qdb_pipeline`` checks the same rule on its own caller
in a second, separate implementation, ``qdb_pipeline/shape.py`` in
qdb-cicd-tools, whose docstring is the precise rule; the test vectors under
``tests/snapshot/shape/`` (one directory per answer) are a copy of that
repository's ``tests/shape/`` and hold the two together.

The rule is defined on the file's bytes. Let ``T`` be the bytes decoded
strictly in the encoding Python reads a source file in
(``tokenize.detect_encoding``: a UTF-8 byte order mark, a coding line in the
first two lines, else UTF-8), and ``M`` be ``ast.parse(T, feature_version=(3,
9))``. A source has the runner's shape when all four hold:

1. ``T`` and ``M`` exist. Any exception while decoding or parsing means no
   shape: bytes that do not decode, a coding line that names no text
   encoding, a null byte, a source nested deeper than the parser goes.
2. It imports the runner. A statement of the module body itself (not nested
   in a function, class, ``if``, ``try``, ``with`` or loop) is
   ``from qdb_pipeline import ...``: absolute (no leading dots), module
   exactly ``qdb_pipeline``, and one of its names is ``run`` with no ``as``
   clause. Other names in the statement do not matter; where the statement
   stands in the module body does not matter.
3. Exactly one statement of the module body is an ``if`` whose test is
   exactly ``__name__ == "__main__"``: the name ``__name__``, the operator
   ``==`` once, then the string ``"__main__"``.
4. That ``if`` has no ``elif`` and no ``else``, and its body is one
   statement: the bare call ``run(generate_pipeline)``, the name ``run`` with
   one positional argument that is the name ``generate_pipeline``, and no
   keyword argument (``**x`` included).

``feature_version=(3, 9)`` makes a newer parser refuse most syntax that Python
3.9 cannot parse, so that the plugin on 3.9 and the runner on a newer Python
answer alike (the runner's docstring lists what the emulation misses).

The rule is decided on the syntax tree, so comments, blank lines, quotes,
parentheses, line continuations and a trailing comma in the call do not
matter. Nothing else is looked at: statements before or after the block, and
other bindings of the name ``run`` (a later ``def run``), change nothing.

Decisions, each with its vector:

- ``from qdb_pipeline import run`` inside a function, ``try``, ``if`` or
  ``with`` does not count; only the module body is read.
- ``from qdb_pipeline import run as r``, and also ``run as run``, do not
  count: the import has an alias. A plain ``run`` import elsewhere still counts.
- ``import qdb_pipeline`` with ``qdb_pipeline.run(...)``: no shape; the name
  ``run`` is not imported and the call is not a call of the name ``run``.
- ``from .qdb_pipeline import run``, ``from qdb_pipeline.runner import run``
  and ``from qdb_pipeline import *`` do not count.
- ``run()``, ``run(a, b)``, ``run(a, check=True)``, ``run(generate=a)``,
  ``run(*a)`` and ``run(**a)``: no shape. Nor ``run(other)`` or
  ``run(generate_pipeline())``: the one argument is the name
  ``generate_pipeline``.
- ``sys.exit(run(a))`` and ``x = run(a)``: no shape; the statement must be the
  bare call.
- A second statement in the block (``pass``, a string, ``run(a); run(b)``):
  no shape.
- An ``else`` or ``elif`` branch: no shape.
- ``"__main__" == __name__``, ``__name__ != "__main__"`` and
  ``__name__ == "__main__" and x`` are not the main block's test; a module
  whose only guard is written so has no main block and no shape.
- Two module-level main blocks: no shape, also when both are one call of
  ``run``. A main block nested in another statement is not a main block.
- An empty file, or one of comments only: no shape.
"""

from __future__ import annotations

import ast
import io
import tokenize

from .shown import shown_text

RUNNER_MODULE = "qdb_pipeline"
RUNNER = "run"
GENERATOR = "generate_pipeline"
MAIN_TEST = 'if __name__ == "__main__":'
FEATURE_VERSION = (3, 9)


def source_text(source: bytes) -> str:
    """``source`` decoded strictly in the encoding of a Python source file.

    Decoding here, not in ``ast.parse``, gives every Python the same answer:
    Python 3.9 parses bytes that are not UTF-8 inside a comment, later
    versions do not.
    """
    encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
    return source.decode(encoding)


def parse_source(source: bytes) -> ast.Module | str:
    """The syntax tree of ``source``, or why it does not parse. Never runs it.

    Whatever exception decoding or parsing raises means the source does not
    parse: foreign bytes can make the codec lookup, the decoder and the
    parser fail in more ways than any list names.
    """
    try:
        return ast.parse(source_text(source), feature_version=FEATURE_VERSION)
    except Exception as exc:  # noqa: BLE001 -- rule 1: whatever is refused has no shape
        return f"does not parse as Python 3.9: {type(exc).__name__}: {shown_text(str(exc))}"


def imports_runner(statement: ast.stmt) -> bool:
    """True for ``from qdb_pipeline import ..., run, ...`` with ``run`` not aliased."""
    return (
        isinstance(statement, ast.ImportFrom)
        and statement.level == 0
        and statement.module == RUNNER_MODULE
        and any(a.name == RUNNER and a.asname is None for a in statement.names)
    )


def is_main_test(test: ast.expr) -> bool:
    """True for exactly ``__name__ == "__main__"``."""
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and isinstance(test.comparators[0].value, str)
        and test.comparators[0].value == "__main__"
    )


def is_main_block(statement: ast.stmt) -> bool:
    return isinstance(statement, ast.If) and is_main_test(statement.test)


def is_name(node: ast.AST, identifier: str) -> bool:
    return isinstance(node, ast.Name) and node.id == identifier


def is_one_runner_call(statement: ast.stmt) -> bool:
    """True for the bare expression statement ``run(generate_pipeline)``."""
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return False
    call = statement.value
    return (
        is_name(call.func, RUNNER)
        and len(call.args) == 1
        and is_name(call.args[0], GENERATOR)
        and not call.keywords
    )


def block_problem(block: ast.If) -> str | None:
    """Why the main block is not the one call ``run(generate_pipeline)``; None when it is."""
    if block.orelse:
        return f"has an elif or else branch on its {MAIN_TEST} block"
    if len(block.body) != 1 or not is_one_runner_call(block.body[0]):
        return f"does not hold exactly the call {RUNNER}({GENERATOR}) in its {MAIN_TEST} block"
    return None


def shape_problem(source: bytes) -> str | None:
    """Why ``source`` does not have the runner's shape; None when it has it.

    The rule is in the module docstring. The text names the first of its
    conditions that fails, worded to follow the name of the file, for the
    plan's message.
    """
    module = parse_source(source)
    if isinstance(module, str):
        return module
    if not any(imports_runner(s) for s in module.body):
        return f"does not import {RUNNER} from {RUNNER_MODULE} at module level without an alias"
    blocks = [s for s in module.body if is_main_block(s)]
    if len(blocks) != 1:
        return f"has {len(blocks)} {MAIN_TEST} blocks at module level, not one"
    return block_problem(blocks[0])


def has_runner_shape(source: bytes) -> bool:
    """True when ``source`` hands its pipeline to the runner (module docstring)."""
    return shape_problem(source) is None
