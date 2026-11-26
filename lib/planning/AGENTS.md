# lib/planning/

**What**: snapshot dependencies. This package is the command of three steps of
a build: the plan step, its second run and the last step. The plan step of a
root build makes the plan, which fixes the commit and the key of the build's
project and of every snapshot dependency below it. When no dependency lacks a
folder, the plan step uploads the build's pipeline at once, with the keys of
its downloads written into it. When one does, the plan step starts a build of
it and ends, and its second run uploads the pipeline after those builds ended.
The last step records the steps that did not pass. The package uploads, downloads and promotes no step's files: the jobs do
that with `lib/`.

`plan_step.py` and `last_step.py` hold what the steps decide, in order. Start
there.

**Map**:

| File            | What                                                                                                                                                                                           | When to read                                                                                                                                                     |
| --------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `plan_step.py`  | the plan step and its second run, as a numbered list of decisions                                                                                                                              | what a build does and in which order; when nothing is built; when a build fails before it builds                                                                 |
| `last_step.py`  | the last step                                                                                                                                                                                  | what the failure record of a build's own steps holds                                                                                                             |
| `own_build.py`  | what a build learns of itself before the plan step decides: its generated pipeline, its plan, its key, what it promotes                                                                        | a root build against a build started for a dependency; what the build was asked for (`SNAPSHOT_NEEDS`)                                                           |
| `planner.py`    | `make_plan`: the tree, the pins, who writes its branch's latest files, the keys                                                                                                                | how pins settle; the rule for `SNAPSHOT_BRANCH_BUILD`; `SNAPSHOT_REBUILD`                                                                                        |
| `places.py`     | where one project is taken from, and whether it can be part of a snapshot there                                                                                                                | branch, release line, master, main; a build at a tag; why a project is refused and the way out                                                                   |
| `lines.py`      | the release line of a root build                                                                                                                                                               | which branch names are lines; a branch that left two lines                                                                                                       |
| `plan.py`       | the plan as a document, and the key of each project of it                                                                                                                                      | the members of the plan; what a project's `key.json` holds; the label of a trigger step                                                                          |
| `latest.py`     | the root build's downloads of the latest successful build of another project                                                                                                                   | why the plan step reads their keys once; what happens at a tag; a download that finds no latest file                                                             |
| `templates.py`  | the declaration of a pipeline: its dependencies and the step ids it promotes, read from the templates of one commit                                                                            | which files are templates; snapshot against latest successful; what a pin is; which step ids count                                                               |
| `pipeline.py`   | a generated pipeline as the plan step reads it: steps, configurations of the plugin, folders                                                                                                   | where a configuration of the plugin may lie in a document; what a step makes; a folder promoted twice                                                            |
| `rule.py`       | what a generated pipeline may download and must name, held against the declaration of its commit and, for an entry without `step`, against the step ids that the plan holds for its dependency | why the plan step fails a pipeline before anything is built                                                                                                      |
| `rewrite.py`    | the pipeline as the plan step uploads it                                                                                                                                                       | `keys` in a download entry; the build's `key`; the plugin's commit in every plugin key; the step that replaces one whose folders are all made                    |
| `triggers.py`   | the trigger steps and the step of the second run                                                                                                                                               | what a started build is handed; why a trigger step is `soft_fail`; the order of the trigger steps                                                                |
| `records.py`    | failure records and the decisions over them                                                                                                                                                    | the reasons of a record; when another build's failure fails this one                                                                                             |
| `failing.py`    | how a step fails: log, record, page, error                                                                                                                                                     | a `Refusal`; why an error of the store or of git writes no record                                                                                                |
| `annotation.py` | what the plan step and the last step show on the build page                                                                                                                                    | the plan's table, whose first column is the tree; the line for a build that does not become the latest of its branch; the page of a failed build and its way out |
| `picture.py`    | the tree of a plan as an SVG, which the plan step uploads as the artifact `snapshot-plan.svg` and its page shows                                                                               | how the picture is laid out; what a box says; why it needs no library and measures no font                                                                       |
| `states.py`     | what becomes of each project of a plan, as its page says it: reused, built, failed, or builds and why                                                                                          | the reasons a key is not made and their order; which folders count for a project; why nothing of it is stored                                                    |
| `edge.py`       | `Edge`, everything the steps read and change, and the build meta-data they keep                                                                                                                | the names of the meta-data; what a test fakes; where the plan is stored                                                                                          |
| `git.py`        | everything the steps ask of git                                                                                                                                                                | reading another repository without a checkout; why a failed command is never read as "no such ref"                                                               |
| `buildkite.py`  | what Buildkite's API says of a pipeline                                                                                                                                                        | the token and its scope; retries; why no redirect is followed                                                                                                    |
| `agent.py`      | the `buildkite-agent` command                                                                                                                                                                  | meta-data, step outcomes, pipeline uploads, annotations, the upload of an artifact                                                                               |

`requirements.txt` holds what the package imports beyond `lib/requirements.txt`:
PyYAML, which only these three steps install, and urllib3. `__init__.py` holds
the package's docstring and no code.

**Invariants**:

- In Buildkite the steps keep only the meta-data of their own build:
  `snapshot-plan`, `snapshot-outcome` and `snapshot-promotes`; and one artifact,
  the picture of the plan, `snapshot-plan.svg`, which nothing reads. In the bucket
  they write `key.json`, latest files and failure records. Of these only the
  failure records are read by the planning alone.
- The plan step of the root build makes the plan and stores it. Its second run
  and every build that it starts read the plan back. A build started for a
  dependency computes no key and reads no template of the projects below it.
  It reads its own templates from its checkout, to hold its pipeline to the
  rule. Its project must be in the plan at the commit that it checked out.
- The steps reach the outside through `Edge`, with one exception: `own_build`
  reads the generated pipeline from the path that the step's command wrote.
  `plan_step.py`, `own_build.py` and `last_step.py` act through `Edge`. The
  modules below them (`planner`, `places`, `records`, `rewrite`, `rule`,
  `triggers`, `annotation`) take values and return values. The tests run the
  same steps over the fakes of `tests/planning/fakes.py`.
- A failure record never says whether a step is made. The manifest of the
  folder says that. A record saves a waiting build from compiling in vain, and
  it lets a dependent name the cause.
- A step fails by raising a `Refusal`, and `failing.run_step` logs the cause
  before it writes a record or a page. Any other exception fails the step and
  writes no record, because an error of the store, of git or of an API says
  nothing of the build of a key.
- A message that a retry cannot cure does not tell the person to retry the
  failed job.
- `rewrite.planned` returns a new document and never changes the one that it
  was given.
- `git.py` reads files from the tree of a commit and never from a working tree.
  Every value that it hands to git comes after `--` or `--end-of-options`, and
  nothing goes through a shell.
- No message and no log line holds the Buildkite API token, and the token goes
  to the API's host only. `tests/test_security.py` holds the plugin to that.
- The runner prints the plan step and the last step, and this package reads
  what it wrote: their options, and the meta-data `snapshot-arrived`. See the
  table in the root `AGENTS.md`.

**Why**:

- The plan step writes into the pipeline, before any job exists, the build's
  own key and the key of every step that a download takes from another
  project. A pipeline that only says "download project X, platform Y" leaves
  the build open, and two jobs could find two builds.
- A dependency's steps are read from its templates, as its own dependencies
  are. A step id counts when a `promote` of a template names it, as its `step`
  or else as its step's `step_id`, and a template step writes it as `step_id`.
  A download entry without `step` takes all of them for its platform. When a
  dependency does not promote one of them on that platform, the build started
  for it fails before it builds, and the dependent's page names the step list
  to write in its entry. A dependent that needs less names its steps.
- A dependency is taken from its pin. Without a pin it is taken from the first
  of these that its repository has: the root build's branch, the release line
  of the root build's commit, `master`, `main`. A story branch of one name in
  several repositories is therefore built together, and a repository without
  the branch gives the line that the branch left.
- The plugin has no notion of a release. A build at a tag differs only in the
  refs that it reads. A dependency without a pin is taken from the same tag and
  from no other ref. A latest successful download reads its own `git_ref` and
  falls back to neither master nor main. A repository of the tree that lacks
  the tag fails the plan before anything is built. A pin still wins over the
  tag.
- A project can be part of a snapshot only when Buildkite runs its
  `.buildkite/pipeline.py` into a pipeline upload, that file hands its pipeline
  to the runner, and its templates take no latest successful build. Without the
  first two the started build would get no plan step. Without the third its
  pipeline would hold a download for which the plan has no key, because the
  plan reads the keys of latest successful downloads for the root build only.
  The plan fails before anything is built, and its message names both ways
  out: the project comes to meet the condition, or the dependent takes it with
  `latest: true`.
- A latest successful download is not planned and never built, but its keys are
  in the build's key. The plan step reads them from the latest files once, and
  the plan holds them, so the second run and every job take the same keys and
  the key says what the build was built from. A download that finds no latest
  file fails the plan before anything runs.
- A download of the pipeline's own project at another branch is left out of the
  key. Its job reads that branch's latest build when it runs, and the plan
  step's page names it. If it were in the key, a build of the other branch
  would change this branch's key for the same commit, and the next dependent
  would have the commit built a second time.
- A build started for a dependency runs under the name of the ref that the
  dependency is taken from: one taken from `refs/heads/master` runs as
  `master`. An agent whose git mirror lacks the build's commit fetches the
  build's branch by name, and a name that is no ref fails that checkout. A
  root build runs under the name that a person or Buildkite gave it.
- A build writes its branch's latest files only when its project is taken at
  the commit git names for its ref, and the build that a person would start
  there without `SNAPSHOT_REFS` would take every project below it at the
  commits of this plan. A build compiled against a story branch would
  otherwise put its files where a download of the branch's latest successful
  build takes them. The rule holds for the root build too: a ref a person gave
  moves a project below it. `planner.writes_latest_file` decides, and the plan
  step writes the answer into the `env` of the pipeline it uploads, as
  `SNAPSHOT_BRANCH_BUILD`, so the root build's jobs and a started build's get
  it the same way. Only the text `false` turns the writing off. On the root
  build's branch the rule takes a branch of one name to have left the same
  release line in every repository that has it. For a project on another
  branch, as one given in `SNAPSHOT_REFS`, the plan asks git for the line that
  branch left, with a fetch of the histories without files, because the plan
  step has no checkout of that repository. It asks only when the project is at
  its branch's commit and a repository below it lacks the branch.
- A ref a person gives in `SNAPSHOT_REFS` counts before a pin, and is not a
  pin: `writes_latest_file` never sees it, because the build a person starts
  later does not have it. A commit is taken only from the ref the project
  would come from without it. The started build runs under that ref's name,
  and an agent whose mirror lacks the commit fetches that ref, which holds it.
  Any other commit would need a ref name to run under that nobody gave.
- A plan step that finds folders made at its key points its branch's latest
  files at the key only when git names the build's commit as the newest of its
  ref. A branch cut at built commits so gets its latest files at its first
  build, and an older build that runs again does not move them back. A build
  started for a dependency asks git at that moment, because its ref may have
  moved since the plan was made.
- The plan step starts a build with a trigger step and ends. Its second run is
  a step of its own that waits for the trigger steps. A trigger step is
  `soft_fail`, because the manifests say whether a dependency is usable and the
  colour of its build does not.
- `SNAPSHOT_NEEDS` names the folders that the dependent downloads, which the
  started build must make. A started build that finds all of them made at its
  key builds nothing. Otherwise it builds every step of its pipeline that is
  not made at its key, on all its platforms.
- The plan step writes the plugin's commit into every key that names the
  plugin. Every job of a build then runs one commit of the plugin, also when
  the templates name a branch.
- The failure records, the last step and the page that follows a failure down
  to the builds that failed exist for the person who must find what went wrong
  in a tree of builds. A failed step is also not built twice for nothing: a
  build that needs a key whose build failed under the same plan fails at once.
- The page says of each project whether it is reused, and why not when it is
  not. `states.py` derives that from what the store holds at the planned key
  each time the page is drawn: the manifests, the failure records and the other
  keys of the same commit. The plan holds none of it, because the plan is fixed
  before the plan step decides and a state changes while the builds run. The
  second run draws the page again, so it then shows what was built. A project is
  reused or built by the time of its promotes against the time the root build
  began, which is why no build reports what it did.
- The state of the build's own project and of the projects that it takes comes
  from the folders that the plan step's decision reads (`OwnBuild.needs` and
  `OwnBuild.takes`), so the page and the decision agree. A build does not know
  which folders are asked of a project further down: only the build above that
  project reads its generated pipeline. For such a project the folders found at
  the key count, and the README states the one case in which that is wrong.
- The plan's page shows the tree as a picture and as a table. The picture shows
  each project once, at its depth, with every project that takes it. The table
  holds the same tree as text that a person can select and search, and repeats a
  project that several take. An annotation shows an image only from an address,
  and `artifact://` is the address of an artifact of the build, so the plan step
  uploads the picture as one. It is an SVG that `picture.py` writes as text, so
  an agent needs nothing installed to draw it. What is built first stands on
  the left: a tree with many projects at one depth then grows downwards, where
  a page has room.
- A retried plan step of a root build plans again, and it reads the latest
  files and the newest commits of the branches again. A key can therefore
  change when that step is retried in the middle of a build.
- The steps name no agents, so they run on the default queue.

**How to**:

- Follow a decision: read the numbered list at the top of `plan_step.py`.
  Decisions 1 to 4 are in `own_build.own_build`, and the rest are in
  `first_run` and `second_run`, in the same order.
- Add a reason for which a build fails before it builds: raise a `Refusal`
  with `failing.refusal`, and give its kind a heading in
  `annotation.REFUSAL_HEADINGS`. When the refusal writes a failure record,
  give its reason a constant in `records.py` and a text in
  `annotation.REASON_TEXTS`.
- Test: see `tests/AGENTS.md`. `tests/planning/test_builds.py` plays whole
  trees of builds over the fakes, and `tests/planning/test_planning.py` checks
  the plan, the declaration and the rewrite without builds.
