"""The planning half of snapshot dependencies (story sc-20192).

Ordinary upload and download jobs never import this package. Its modules
import each other and, of the rest of ``lib/``, the forms of values, the
pointer and the library's error (``forms``, ``pointer``, ``errors``), so that
the plan step reads a value by the form the job that wrote it checked.

- ``canonical``: canonical JSON and the key of a snapshot.
- ``configuration``: the artifacts plugin's configuration as written in a
  document, read by its structure: option keys, blocks and download entries,
  wherever they sit.
- ``declaration``: what a commit's step templates declare (snapshot and latest
  successful dependencies, pins), and the rule that holds a generated pipeline
  to it.
- ``shape``: whether a ``.buildkite/pipeline.py`` hands its pipeline to the
  runner of ``qdb_pipeline``.
- ``agent``: the calls to ``buildkite-agent``, behind one small interface.
- ``buildkite``: what Buildkite's REST API says about a pipeline (its
  repository, and whether its configuration runs ``.buildkite/pipeline.py``).
- ``shown``: how a foreign value appears in a message.
"""
