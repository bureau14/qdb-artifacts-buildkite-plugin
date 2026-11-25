# tests/

**What**: the plugin's suite. It has two worlds. In the job's world a test runs
the real hooks as an agent runs them, against an S3 and SSM server on the local
host. In the world of builds a test plays the builds of a whole tree of
projects in process, with the decisions of `lib/planning/` over fakes. The suite
runs no real Buildkite build and reaches no real store.

**Map**:

| File                            | What                                                                                                                                                                   | When to read                                                                                          |
| ------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `conftest.py`                   | loads the two worlds as pytest plugins                                                                                                                                 | where a fixture comes from                                                                            |
| `world.py`                      | the job's world: the store server, a fake `buildkite-agent`, the plugin as an agent checks it out, jobs                                                                | what a hook sees; how a step's options become variables; which Python makes the virtual environments  |
| `test_jobs.py`                  | what a step relies on, through the hooks: download, upload, promote, the library, and a build started for a dependency                                                 | how a property of the job's side is stated over generated cases                                       |
| `test_security.py`              | an archive writes nothing outside its output directory, and the Buildkite API token shows nowhere                                                                      | the two properties that are no happy path                                                             |
| `test_entry_filters.py`         | what an entry filter extracts, and under which name, from a tar, a tar.gz and a zip                                                                                    | the stripping of an entry filter's directory from output paths                                        |
| `outside_caller.py`             | a program that calls `download_artifacts`, `Branch`, `Key`, `load_store_config` and `read_manifest` as the README shows, run by `test_jobs.py` in a process of its own | changing one of those names or its arguments                                                          |
| `planning/builds.py`            | the world of builds: projects generated from a graph, a platform matrix and step ids, and a simulated Buildkite                                                        | what a generated project looks like; how a trigger step, a job and a failed step are played           |
| `planning/fakes.py`             | the fakes behind `Edge`: the store, git, the agent and Buildkite's pipelines, in memory                                                                                | what a fake answers; adding a question to `Edge`                                                      |
| `planning/test_builds.py`       | the four guarantees of snapshot dependencies, and the rule for a branch's latest files, over played builds                                                             | which guarantee a change touches; the layouts of branches, tags and pins that the rule is tested over |
| `planning/test_planning.py`     | the key, the declaration of a template, the step ids of a plan and the rewrite, without builds, and the release line of a branch, asked of real git                    | a change to the key, to the reading of templates, to the rewrite or to `lines.release_line`           |
| `planning/fixtures/*.yml`       | five real generated pipelines, reduced to their shape                                                                                                                  | what the plan step must read of a real pipeline                                                       |
| `planning/fixtures/sanitise.py` | the script that reduces a generated pipeline to a fixture                                                                                                              | adding or refreshing a fixture                                                                        |
| `requirements.txt`              | what the suite installs: the requirements of `lib/` and `lib/planning/`, moto with flask and flask-cors for its server, and pytest                                     | changing a dependency of the suite                                                                    |

**Invariants**:

- A test states one property and checks it over generated cases. No test pins
  the wording of a message. Some properties are of a build that fails: in
  `planning/test_builds.py` a failed step, a failure that two builds share and
  a dependency that stops the plan, and in `test_jobs.py` an agent that names
  no `QDB_CICD_AGENT_PLUGIN_PYTHON`. `test_security.py` holds the two
  properties that guard against an attack.
- The job's world copies `hooks/`, `lib/` and `artifacts.py` from the working
  tree into a repository of one commit, as an agent checks the plugin out. It
  runs the hooks there with bash, in the environment that an agent gives a
  job.
- The world of builds runs the plugin's own functions for the planning and for
  the parts of a job: `plan_step`, `plan_step_again`, `last_step`,
  `options.options_of`, `resolve.entry_steps`, `upload.capture_step` and
  `promote.promote_folder`. A test of it therefore fails when one of them
  decides otherwise. `World.job` and `World.own_key` of `planning/builds.py`
  restate the order of `cli.post_command` and `key.own_key`, and every played
  job exits 0. `test_jobs.py` holds those two to the hooks.
- A fixture holds no command, no environment, no agent and no configuration of
  another plugin. `sanitise.py` keeps the step structure, the labels, the keys
  and this plugin's blocks, and it writes the `git_ref` of a branch as
  `refs/heads/feature`. Read a fixture before it is committed.
- Every organisation, host and URL in the suite is invented. So is every
  project but the five of the fixtures, which `REAL` of `planning/builds.py`
  names.

**How to**:

- Run the suite with Python 3.13 and git on the host:

  ```bash
  python3 -m venv .env/
  .env/bin/python3 -m pip install -r tests/requirements.txt
  .env/bin/python3 -B -m pytest -p no:cacheprovider tests
  ```

- Run one world: `tests/test_jobs.py` for the hooks, `tests/planning` for the
  played builds, which need no server.
- Add a fixture: generate the pipeline in its repository, run
  `python tests/planning/fixtures/sanitise.py <generated.yml> <out.yml>`, and
  read the result before it is committed.
