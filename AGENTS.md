# qdb-artifacts-buildkite-plugin

**What**: a Buildkite plugin that keeps what the steps of a build make in an
S3-compatible object store. A step uploads its files, a promote makes an upload
official, and a step of the same pipeline or of another one downloads them. The
plugin also plans snapshot dependencies. Before the jobs of a build run, its
plan step fixes the build of every other pipeline that they download from, and
starts the builds that are missing. The plugin generates no pipeline. A
repository's `.buildkite/pipeline.py` does that with the runner,
`qdb_pipeline.run` of the repository qdb-cicd-tools, and the runner prints the
plan step and the last step that this plugin runs.

`README.md` is the reference for a person who writes a pipeline. These files are
for a person who changes the plugin, and they use the README's words without
defining them again: project, platform, step id, key, `key.json`, manifest,
captured and promoted, the branch's latest file, the plan step, its second run,
the last step, the root build, the plan, snapshot dependency, latest successful
dependency, pin and release line. Read the README's sections "The bucket" and
"Snapshot dependencies" first.

**Map**:

| Path                      | What                                                                                                                                      | When to read                                                                                       |
| ------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `README.md`               | every option, the layout of the bucket, how a build with snapshot dependencies runs and fails, the library for a caller outside Buildkite | writing a pipeline; what an option means; what a build shows of its downloads; calling the library |
| `plugin.yml`              | the options as Buildkite's description of the plugin                                                                                      | adding or renaming an option                                                                       |
| `hooks/`                  | the three hooks that the agent runs, which share `lib/shared.bash`                                                                        | which hook does what; when `pre-command` and `post-command` leave without starting Python          |
| `artifacts.py`            | the command line that the hooks start                                                                                                     | why it lies outside the package                                                                    |
| `lib/`                    | the job's side, which uploads, downloads and promotes, and the library for a caller outside Buildkite                                     | see `lib/AGENTS.md`                                                                                |
| `lib/planning/`           | the plan step, its second run and the last step                                                                                           | see `lib/planning/AGENTS.md`                                                                       |
| `tests/`                  | the suite                                                                                                                                 | see `tests/AGENTS.md`                                                                              |
| `.buildkite/pipeline.yml` | the plugin's own pipeline, which runs ruff and the suite                                                                                  | changing what CI runs; the Python version the suite runs on                                        |
| `ruff.toml`, `pytest.ini` | the lint rules, and the paths from which the suite imports                                                                                | a lint rule; why a test module imports `world`, `builds` and `fakes` by name                       |
| `.gitignore`              | the virtual environments and the caches that the hooks and the tools leave in a checkout                                                  | the name of a new virtual environment or cache                                                     |

**Invariants**:

- The repository is public. No file names an internal host, a bucket or a
  credential. The fixtures under `tests/planning/fixtures/` are five generated
  pipelines that `sanitise.py` reduced to their shape. They keep the step
  keys, the labels and the plugin's blocks of those pipelines, and no command,
  environment or agent. Every other name in the suite is invented.
- `lib/` does not import `lib/planning/` at module level. `lib/cli.py` imports
  it inside `run_plan_side`, for the plan step, its second run and the last
  step. Only those steps install `lib/planning/requirements.txt`, into a virtual
  environment of their own. Every other job, and a caller of the library, runs
  without PyYAML.
- The plugin runs the Python that the agent names in
  `QDB_CICD_AGENT_PLUGIN_PYTHON`. It takes none from `PATH` and checks no
  version.
- `lib/layout.py` builds every object key that the plugin reads or writes. A new
  kind of object gets its function there, and its line in the layout at the top
  of that file and in the README's "The bucket".
- The plugin and the runner state the same rules in two places, and a change to
  one changes the other:

  | Rule                                                             | Here                                                         | In the runner (`qdb_pipeline/`)                          |
  | ---------------------------------------------------------------- | ------------------------------------------------------------ | -------------------------------------------------------- |
  | a `pipeline.py` hands its pipeline to the runner                 | `RUNNER_SHAPE`, `lib/planning/places.py`                     | `SHAPE`, `decision.py`                                   |
  | a plugin key names this plugin                                   | `is_plugin_key`, `lib/planning/pipeline.py`                  | `names_plugin`, `artifacts_plugin.py`                    |
  | the name of an option key                                        | `as_agent_names`, `lib/options.py`                           | `option_name`, `artifacts_plugin.py`                     |
  | the form of a step id                                            | `STEP_ID`, `lib/layout.py`                                   | `STEP_ID`, `templates.py`                                |
  | the options of the plan step and the last step                   | `plan` and `last_step`, `lib/options.py`                     | `plan_steps.py`                                          |
  | the time a build began, as meta-data                             | `ARRIVED`, `lib/planning/edge.py`, which reads it            | `ARRIVED`, `runner.py`, which sets it                    |
  | the meta-data that says how the plan step ended                  | `OUTCOME`, `lib/planning/edge.py`, which the plan step sets  | the last step's command, `plan_steps.py`, which tests it |
  | the priority and the timeout of the plan step and its second run | `PRIORITY` and `TIMEOUT_MINUTES`, `lib/planning/triggers.py` | `plan_steps.py`                                          |

**Why**:

- One plugin does the jobs' work and the planning. The plan step must find a
  step at a key as a job does, with the same store configuration, layout,
  options and walk over the latest files. `lib/planning/` calls the functions
  of `lib/` for that and holds no copy of them.
- A repository declares a dependency once, in a download entry of a committed
  step template. The plan step reads the templates of every project of the plan
  through git, so no second list of dependencies exists that could disagree
  with what the jobs download.
- Every build has a key. The key of a build with a plan step says what the
  build was built from: its project's commit and the key of every build that it
  downloads from another project. The key holds no branch and no tag. Planned
  builds of the same commits therefore share a key under any name, and what is
  made at a key is not built again. From such a key, `key.json` leads down to
  the commit of every project below it. A build without a plan step has a key
  of its commit and its build id, which it shares with no other build.
- The path of every artifact holds the key, the platform and the step id. The
  step id names what one step makes for one platform, so a step never writes a
  record that another step must read to learn that a platform is complete.
- An upload and a promote are two events. An upload captures files also after a
  failed command, because the files of a failed build are evidence. A promote
  says that the files are good to use. A step promotes its own upload, or a
  later step promotes it after it checked the files, and each step id is
  promoted on its own.
- The bucket holds nothing that the pipeline, Buildkite or a manifest can give,
  and no path names a version of the layout. The plan is meta-data of the root
  build. The destination setting names the bucket and the prefix, and the
  plugin adds no prefix of its own.
- No variable exists to turn the planning off. A build of only part of its
  pipeline runs without the plan step, as the README's "Limits" says.
- `SNAPSHOT_REBUILD` exists because an artifact can change without a commit,
  for example after an update of the agents. It puts the plan's id into
  `key.json`, so the forced build gets a key of its own, and the builds of
  other plans keep the key they had.

**How to**:

- Lint: `ruff check .` and `ruff format --check .`. CI runs ruff 0.16.0.
- Test: see `tests/AGENTS.md`.
- CI runs both, in the steps of `.buildkite/pipeline.yml`.
- Try a change on real builds: name the plugin at a branch or a commit in a
  pipeline's steps, as `bureau14/qdb-artifacts#<ref>`. In a build with a plan
  step, the runner resolves that ref to one commit, and every job of the build
  runs the plugin at that commit.

## Documentation strategy

Knowledge lives next to the code that it describes.

1. A fact about one line, one function or one file is a comment in that file.
2. A fact about several files of one directory is in that directory's
   `AGENTS.md`, which holds, in this order and only when non-empty: **What**,
   **Map**, **Invariants**, **Why** and **How to**.
3. A parent `AGENTS.md` indexes its children and states what is common to them.
   It repeats nothing of them. Read from the root to the leaf.
4. `README.md` says what a person who writes a pipeline or calls the library
   relies on. The `AGENTS.md` files say what a person who changes the plugin
   must keep true, and why the plugin is built as it is.
5. A document changes in the same commit as the code that it describes. A change
   that makes an `AGENTS.md` or the README wrong is incomplete.
6. A document states how the plugin is. History is git's job, and no document
   or comment names an earlier version, a plan or a review.
