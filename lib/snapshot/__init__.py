"""The planning half of snapshot dependencies (story sc-20192).

Ordinary upload and download jobs never import this package. Its modules
import each other and, of the rest of ``lib/``, the forms of values, the
pointer and the library's error (``forms``, ``pointer``, ``errors``), so that
the plan step reads a value by the form the job that wrote it checked. Only
``steps``, the edge of the gate steps, opens the store as a job opens it and
reads the job's options and variables.

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
- ``members``: a member of a JSON object read back from elsewhere, of the
  type its reader expects.
- ``git``: everything asked of git (the commit of a branch or tag, the files
  of another repository's commit, the branches of a repository and their
  merge-bases with the checkout, the checkout's commit and files), behind one
  small interface.
- ``lines``: the release line of a root build, decided on git's facts.
- ``plan``: the plan of a chain as data and as the JSON document kept in the
  root build's meta-data.
- ``planner``: the plan of a root build: its snapshot tree, from git and
  Buildkite, each behind an interface.
- ``records``: failure records as values, and the decisions the gate steps
  make over the records they have read.
- ``document``: the generated pipeline as data: what the plan step finds in
  it, the one resolution it writes into it, and the trigger and resume steps.
- ``annotation``: the texts a gate step shows on the build page: the plan, a
  refusal, a failure and how to retry it.
- ``steps``: enter, resume and leave, the decisions of the gate steps in the
  specification's order, over the store, the agent, git, Buildkite's API,
  the clock and the log.
"""
