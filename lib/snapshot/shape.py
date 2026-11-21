"""Whether a pipeline.py has the shape of one that hands its pipeline to the runner.

The rule::

    A pipeline.py has the shape when its bytes contain qdb_pipeline and run(generate_pipeline).

Both substrings are ASCII and are looked for in the file's bytes; nothing is
decoded or parsed. The runner of ``qdb_pipeline`` applies the same sentence to
its own caller (``qdb_pipeline/shape.py`` in qdb-cicd-tools): the two
repositories share the sentence, not the code.
"""

from __future__ import annotations

SUBSTRINGS = (b"qdb_pipeline", b"run(generate_pipeline)")


def has_shape(source: bytes) -> bool:
    """Whether a pipeline.py with the bytes ``source`` has the shape."""
    return all(s in source for s in SUBSTRINGS)
