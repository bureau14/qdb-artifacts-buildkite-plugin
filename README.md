# qdb-artifacts-buildkite-plugin

A [Buildkite plugin](https://buildkite.com/docs/plugins) for uploading and downloading build artifacts to S3-compatible object stores (AWS S3 and Cloudflare R2).

## What it does

- **Upload** -- after a build step's command completes, glob-matches local files and uploads them to `s3://<bucket>/<prefix>/<project>/<key>/<platform>/<step id>/...`, preserving directory structure, with optional exclusions, then writes the folder's `manifest.json`. This includes failed builds, so diagnostic artifacts remain available.
- **Download** -- before a step's command runs, fetches the files of steps of a build (cross-step sharing), with optional extraction and entry-level filtering. It takes a step at a key it is given, or the latest successful build of a branch, with a fallback to the master and main branches.
- **Promote** -- after a successful command, makes a step's upload official in its manifest, once, and points the branch's latest file at the build's key, so that downstream steps can take the latest successful build without knowing its key.

Uploaded artifacts are stored with `Content-Disposition: attachment` so browsers won't try to display them, instead prompting to download the file.

Artifacts are scoped by project, key, platform and step id: the key says what a build was built from, and each step of a build keeps what it makes for one platform in a folder of its own, so test steps can address a specific build step's output.

## Requirements

- **Python 3.13** on the host agent, named by the agent in `QDB_CICD_AGENT_PLUGIN_PYTHON`, which every agent image sets for its plugins. The plugin runs that Python and no other, none from `PATH`, and does not check its version; the plugin's pipeline tests on 3.13.
- The agent must have IAM permissions (S3) or SSM-stored R2 credentials configured. See [Backend configuration](#backend-configuration).

## Usage

A step that uploads or promotes names its step id at the top of its configuration, as `step`: what it makes for its platform, the same on every platform (`build`, `api`). A `pipeline.py` writes `step_id: <id>` in the step's template in place of its key, and the runner's `load_template` (qdb-cicd-tools) makes the key and writes this option; a pipeline written in YAML by hand writes `step` itself. A step that promotes what another step uploaded names that step id in its promote block instead (see [Promote what another step uploaded](#promote-what-another-step-uploaded)). A step id is lower-case letters, digits and `_`, and starts with a letter. A step that uploads or promotes without one fails before its command.

### Upload (build step)

```yaml
steps:
  - label: ":hammer: Build"
    key: build-linux-amd64-release
    command: make release
    plugins:
      - bureau14/qdb-artifacts#v1.0.0:
          step: build
          upload:
            variant: "linux-amd64-release"
            files: "artifacts/**/*.tar.zst"
```

### Upload with base directory stripped

You can use `base_dir` to strip a directory prefix from the uploaded S3 keys, similar to `tar -C`.

```yaml
steps:
  - label: ":hammer: Build Wheels"
    command: python setup.py bdist_wheel
    plugins:
      - bureau14/qdb-artifacts#v1.0.0:
          step: build
          upload:
            variant: "linux-amd64-release-python"
            files: "dist/quasardb*.whl"
            base_dir: "dist"
```

In this example, a file located at `dist/quasardb.whl` will be uploaded such that its object key suffix is `quasardb.whl` rather than `dist/quasardb.whl`.

### Upload with exclusions

Use `exclude` to skip files after the `files` glob has matched. Exclude patterns
are matched against the uploaded artifact path after `base_dir` is stripped.

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      step: build
      upload:
        variant: "linux-amd64-release"
        files: "dist/**/*.tar.zst"
        base_dir: "dist"
        exclude:
          - "*-debug.tar.zst"
          - "*-tests.tar.zst"
```

### Allow an empty upload

Set `upload.allow_empty: true` to skip uploading without an error when no files
match `files` or all matches are excluded. Defaults to `false`, so empty uploads
fail. Skipping requires no object-store configuration or credentials, creates
no manifest or upload annotation, and skips promotion when `promote` is
configured in the same step.

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      step: build
      upload:
        variant: "linux-amd64-release"
        files: "artifacts/**/*.tar.zst"
        allow_empty: true
```

### Download (test step, from its own build, with extraction and entry filtering)

An entry of the pipeline's own project without a `git_ref`, or with the build's
own, takes the steps it names from the build's own key; it must name them.

```yaml
steps:
  - label: ":test_tube: Test"
    command: ./run-tests.sh
    plugins:
      - bureau14/qdb-artifacts#v1.0.0:
          download:
            clean: true
            projects:
              - variant: "linux-amd64-release"
                step: build
                output-dir: artifacts
                extract: true
                files:
                  - "*-c-api.tar.zst!bin/*"
                  - "*-c-api.tar.zst!lib/*"
                  - "*-server.tar.zst!bin/*"
                  - "*-tests.tar.zst!bin/*"
```

### Download (cross-project, with extraction and entry filtering)

```yaml
steps:
  - label: ":test_tube: Build depending on quasardb artifacts"
    command: ./build.sh
    plugins:
      - bureau14/qdb-artifacts#v1.0.0:
          download:
            clean: true
            projects:
              - project_id: quasardb
                variant: "linux-amd64-release"
                git_ref: "refs/heads/main"
                output-dir: artifacts
                extract: true
                files:
                  - "*-c-api.tar.zst!lib/*"
                  - "*-server.tar.zst!bin/*"
                  - "*-tests.tar.zst!bin/*"
```

Which steps an entry takes, and at which key:

- `step` names one step id or a list of them; `step: []` is the same as no `step`.
- `keys`, which the plan step of a build with snapshot dependencies writes, gives the key of each step the entry takes.
- An entry of the pipeline's own project (no `project_id`, or `BUILDKITE_PIPELINE_SLUG`) whose `git_ref` is absent or the build's own ref takes the steps it names at the build's own key.
- Any other entry takes the latest successful build of its `git_ref`: a named step takes the key its own latest file names, and an entry without `step` takes every step whose latest file names the newest key of the platform. A key counts only when the step's manifest says promoted. When the ref has none, the download tries `refs/heads/master`, then `refs/heads/main`, unless the entry is of one of those or says `require_strict_ref_match: true`.
- An entry fails only when none of its steps gives a file that matches. Steps are downloaded in the order of their ids; of two files of one path the last stays.

### Download without extraction

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      download:
        projects:
          - variant: "linux-amd64-release"
            step: build
            output-dir: dist
            files:
              - "*.tar.zst"
```

### Download with exclusions

Use `exclude` to skip artifacts after the `files` patterns have matched. Exclude
patterns are matched against the artifact path, not archive entries.

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      download:
        projects:
          - project_id: "qdb-api-jni"
            variant: "linux-amd64-release"
            git_ref: "refs/heads/main"
            output-dir: "artifacts/javadoc"
            extract: true
            files:
              - "jni-*-javadoc.jar"
            exclude:
              - "jni-*-test-javadoc.jar"
```

### Upload with tuned parallelism

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      step: build
      upload:
        variant: "linux-amd64-release"
        files: "dist/**/*.tar.zst"
        parallel: 8
        concurrency: 16
```

### Upload without Buildkite annotation

Set `annotate: false` to skip creating the Buildkite job annotation after upload.

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      step: build
      upload:
        variant: "linux-amd64-release"
        files: "dist/**/*.tar.zst"
        annotate: false
```

### Upload and promote in one step

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      step: build
      upload:
        variant: "linux-amd64-release"
        files: "dist/**/*.tar.zst"
      promote:
        variant: "linux-amd64-release"
```

### Promote what another step uploaded

A promote promotes its own step id by default. A step that checks what another
step of the same pipeline uploaded names that step id in its promote block, and
promotes it once its own command passed; it names no step id of its own. The
step that uploads then has no `promote`, and its files stay captured until the
check promotes them. The runner (qdb-cicd-tools) makes the checking step wait
for the step that uploads.

```yaml
steps:
  - label: ":mag: Check"
    key: check-linux-amd64-release
    command: ./check.sh
    plugins:
      - bureau14/qdb-artifacts#v1.0.0:
          download:
            projects:
              - variant: "linux-amd64-release"
                step: build
                files: ["*.tar.zst"]
          promote:
            variant: "linux-amd64-release"
            step: build
```

## Configuration reference

### Top-level keys

| Key        | Type    | Description                                                                                                                                                                                                                           |
| ---------- | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `step`     | string  | The step id of a step that uploads or promotes. Required for those steps. See [Usage](#usage).                                                                                                                                        |
| `key`      | string  | Written by the plan step, never by a person: the key of the build. Without it, each job computes a key of its own build. See [The bucket](#the-bucket).                                                                               |
| `upload`   | object  | Upload configuration block.                                                                                                                                                                                                           |
| `download` | object  | Download configuration block.                                                                                                                                                                                                         |
| `promote`  | object  | Makes an upload of its platform official, of this step's id or of the one its `step` names, and moves the branch's latest file.                                                                                                       |
| `debug`    | boolean | Enable bash `set -x` debug tracing in all hooks. Default: `false`.                                                                                                                                                                    |
| `plan`     | string  | Written by the runner (`qdb_pipeline.run`), never by a person: the gate step that enters a build with snapshot dependencies, and the path of the pipeline its command generated. See [Snapshot dependencies](#snapshot-dependencies). |
| `resume`   | string  | Written by the plan step, never by a person: the gate step that goes on once the triggered dependency builds have ended, and the path of the generated pipeline. See [Snapshot dependencies](#snapshot-dependencies).                 |
| `leave`    | boolean | Written by the runner (`qdb_pipeline.run`), never by a person: the gate step that runs after every other step and records which steps did not pass. See [Snapshot dependencies](#snapshot-dependencies).                              |

### `upload` object keys

| Key           | Type             | Required | Description                                                                                                               |
| ------------- | ---------------- | -------- | ------------------------------------------------------------------------------------------------------------------------- |
| `variant`     | string           | yes      | The platform the step uploads (e.g. `linux-amd64-release`).                                                               |
| `files`       | string           | yes      | Glob pattern for files to upload (e.g. `artifacts/**/*.tar.zst`).                                                         |
| `exclude`     | array of strings |          | Glob patterns to skip after `files` has matched. Matched against the uploaded artifact path after `base_dir` is stripped. |
| `base_dir`    | string           |          | Optional base directory to strip from uploaded object keys (e.g. `dist`).                                                 |
| `parallel`    | integer          |          | Files uploaded simultaneously. Default: `4`.                                                                              |
| `concurrency` | integer          |          | Multipart threads per upload. Default: `32`.                                                                              |
| `annotate`    | boolean          |          | Create a Buildkite job annotation listing uploaded artifacts. Default: `true`.                                            |
| `allow_empty` | boolean          |          | Skip upload without error when no files match or all matches are excluded. Default: `false`.                              |

The project is the pipeline's (`BUILDKITE_PIPELINE_SLUG`) and the branch the
build's (`BUILDKITE_BRANCH`); an upload or promote block names neither.

### `promote` object keys

| Key       | Type   | Required | Description                                                                                                                                                    |
| --------- | ------ | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `variant` | string | yes      | The platform whose upload of this step to make official.                                                                                                       |
| `step`    | string |          | The step id to promote, when it is not this step's own: what another step of the same pipeline uploaded for the platform. See [Promote what another step uploaded](#promote-what-another-step-uploaded). |

### `download` object keys

| Key           | Type    | Required | Description                                                                                                   |
| ------------- | ------- | -------- | ------------------------------------------------------------------------------------------------------------- |
| `projects`    | array   | yes      | List of entries to download, each of one project and platform.                                                |
| `clean`       | boolean |          | Remove `output-dir` of all configured projects before downloading. Useful for retried jobs. Default: `false`. |
| `parallel`    | integer |          | Files downloaded simultaneously. Default: `4`.                                                                |
| `concurrency` | integer |          | Multipart threads per download. Default: `32`.                                                                |

#### `projects` item keys

| Key                        | Type                       | Required | Description                                                                                                                     |
| -------------------------- | -------------------------- | -------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `project_id`               | string                     |          | Unique project identifier of the artifacts to download. Defaults to `BUILDKITE_PIPELINE_SLUG`.                                  |
| `variant`                  | string                     | yes      | The platform to download.                                                                                                       |
| `step`                     | string or array of strings |          | The step ids to download. Required from the build's own key; without it, an entry of another project takes every step.          |
| `keys`                     | object                     |          | Written by the plan step, never by a person: the key of each step the entry takes.                                              |
| `latest`                   | boolean                    |          | The entry takes the latest successful build of another project, which a build with snapshot dependencies does not plan.         |
| `git_ref`                  | string                     |          | The ref whose latest successful build to take (e.g. `refs/heads/main`). Required in an entry of another project without `keys`. |
| `require_strict_ref_match` | boolean                    |          | Disable branch fallback. Default: `false`.                                                                                      |
| `files`                    | array of strings           | yes      | Archive glob patterns, optionally with entry filters (see [Entry filtering](#entry-filtering)).                                 |
| `exclude`                  | array of strings           |          | Archive glob patterns to skip after `files` patterns have matched.                                                              |
| `output-dir`               | string                     |          | Destination directory. Default: `.` (current working directory).                                                                |
| `extract`                  | boolean                    |          | Stream-extract archives on download (no intermediate file on disk). Default: `false`.                                           |

### Entry filtering

File patterns in `files` support an optional `!entry_filter` suffix:

```
*-server.tar.zst!bin/*
```

This matches archives by glob (`*-server.tar.zst`), extracts only entries matching `bin/*`, and strips the `bin/` prefix from output paths — so `bin/qdb` lands at `<output-dir>/qdb`.

Multiple entry filters may match the same archive. Their matching entries are
extracted as a union in one archive download and decompression pass, while each
filter keeps its own stripping prefix. For example:

```
*-c-api.tar.zst!bin/*
*-c-api.tar.zst!lib/*
```

extracts both `bin/qdb` and `lib/libqdb.so` from one `*-c-api.tar.zst` download,
placing them at `<output-dir>/qdb` and `<output-dir>/libqdb.so` respectively.

Supported archive formats for extraction: `.tar.gz`, `.tar.zst` / `.tar.zstd`, `.zip`, `.jar`.

## How it works

| Hook            | Trigger                 | Action                                                                                                                                                                                                                                                                                                   |
| --------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `post-checkout` | After repo checkout     | Prints the path and version of the Python that `QDB_CICD_AGENT_PLUGIN_PYTHON` names, fails when that is not set or names nothing that runs, and makes the step's venv anew with it: `<plugin-dir>/.venv` with the dependencies from `lib/requirements.txt`, or the venv of a gate step, described below. |
| `pre-command`   | Before the step command | If `download`, `upload` files or a `promote` platform are configured, runs `artifacts.py pre-command`, which fails a step that uploads or promotes without a step id, then downloads (and optionally extracts) the files of the steps each entry takes.                                                  |
| `post-command`  | After the step command  | If `upload` files or a `promote` platform are configured, runs `artifacts.py post-command`. It uploads matched files to S3/R2, including after a failed command, then promotes only after a successful command and an upload that was not empty.                                                         |

`pre-command` and `post-command` start `artifacts.py` with the Python of the venv that `post-checkout` made, and the name of their phase; `lib/cli.py` decides what the phase runs.

On a gate step of a build with snapshot dependencies (option `plan`, `resume` or `leave`), `post-command` runs the gate command of the step, also after a failed command, and nothing else. `post-checkout` installs `lib/snapshot/requirements.txt` (PyYAML) besides `lib/requirements.txt` on the steps that carry `plan` or `resume`, and on no other step: no other job needs it, on any platform. Those steps make their venv in `<plugin-dir>/.venv-snapshot`: every job of one plugin commit on a host shares the plugin's checkout and makes its venv again, and no other job's venv then replaces theirs.

### Failed builds

If the build command exits with a non-zero status, `post-command` still uploads configured artifacts, making logs and other diagnostic outputs available. It never promotes those artifacts, so a step's manifest says promoted, and a branch's latest file names a key, only for a build whose command succeeded.

### The bucket

```text
<prefix>/<project>/<key>/key.json                           what the key stands for
<prefix>/<project>/<key>/<platform>/<step id>/<files>       what one step made for one platform
<prefix>/<project>/<key>/<platform>/<step id>/manifest.json the files of that upload, and who promoted them
<prefix>/<project>/<key>/failures/<build id>.json           a build of this key failed, and why
<prefix>/<project>/refs/heads/<branch>/<platform>/<step id> the branch's latest file: its text is a key
```

**The key** says what a build was built from: the 40 hex digits of the
project's commit, a dash, and the first 32 hex digits of the SHA-256 of
`key.json`, the canonical JSON (members sorted, no white space, UTF-8) of
`{"commit": <40 hex>, "deps": [[<project>, <key>], ...], "project": <slug>}`.
`deps` holds the key of the build taken of each snapshot dependency, sorted.
A key of one build's own also holds `"build": "<build id>"`, so that no other
build has it. The plan step of a build with snapshot dependencies computes the
keys of its build and of the builds it starts, writes its own as the option
`key` into every configuration of the plugin, and creates `key.json` once. A
build without a plan step has no `key` option: each of its jobs computes the
key of its own build (its project, the commit of its checkout, no dependency,
its build id), and its first upload creates that `key.json`.

**The manifest** of a step's folder, canonical JSON:
`{"files": [{"path", "size"}, ...], "promoted": null or {"at", "branch", "id", "number", "url"}, "uploaded": {"branch", "id", "number", "url"}}`.
An upload writes the files, reads the manifest again, and writes it with
`promoted` null: the step is captured. An upload into a folder whose manifest
says promoted writes nothing and counts as empty, so the promote of its step
does not run. A promote writes the manifest back once, with this build, its
branch and the time: the step is made at the key, and the first promote wins.
A promote that finds the folder promoted already leaves it as it is.

**The branch's latest file** names the key of the latest build of a branch
that made a step for a platform. A promote writes it right after it promoted,
under `refs/heads/<BUILDKITE_BRANCH>`; the last writer wins. A build under a
`snapshot/` name, which a plan step starts for a dependency, writes none.

## Snapshot dependencies

A build that downloads the artifacts of another pipeline gets them from the
build of exactly the commits its root planned, and every job of a chain of
builds downloads them at the same keys. A repository configures nothing for
this beyond what its step templates already say: which project each step
downloads, and the step id of each step that uploads.

### Words

- A **project** is a Buildkite pipeline, named by its slug. A **platform** is
  what the option `variant` names, such as `linux-amd64-release`; a **step
  id** names what one step uploads for one platform.
- The **key** of a build says what it was built from (see
  [The bucket](#the-bucket)). A step is **made** at a key when its manifest
  there says promoted; nothing changes a promoted manifest.
- A **root** is a build without `SNAPSHOT_PLAN` in its environment: one
  started by a person, a push or a schedule. Its plan step makes the **plan**:
  the commit, the key, the step ids and the branch of every project of its
  snapshot tree. A **chain node** is a build that a trigger step started for
  the plan, with `SNAPSHOT_PLAN` in its environment; a **chain** is a root and
  its chain nodes.
- The **branch** of a chain node is the name its build runs under: its own
  branch when the build is the one its own pipeline would make for that
  branch, else `snapshot/<root branch>`, a name no branch has, under which it
  writes no latest file.

### Two types of dependency

A download entry of another project declares a dependency when it is in a
committed `.yml` or `.yaml` file under `.buildkite/`, under a key that names
the plugin exactly: `bureau14/qdb-artifacts` or `bureau14/qdb-artifacts#<ref>`.
Whether the entry says `latest: true` gives its type.

A **snapshot** dependency does not:

```yaml
download:
  projects:
    - project_id: "quasardb"
      variant: "linux-amd64-release"
      git_ref: "{git_ref}"
      files: ["*-c-api.tar.zst"]
```

Before any job runs, the plan step writes into the entry the key of the
planned build for each step it takes, as `keys`, in place of its `step`,
`git_ref` and `require_strict_ref_match`. An entry without `step` takes every
step id the dependency's templates promote at the planned commit, which the
plan carries. A `git_ref` that is literal text in the template, such as
`refs/heads/3.14.x`, is a pin: the project is planned at that ref for the
whole chain. A ref that only `pipeline.py` gives, through `by_project` for
example, is no pin: the plan step reads templates and never runs another
repository's `pipeline.py`.

A **latest successful** dependency says `latest: true`:

```yaml
download:
  projects:
    - project_id: "quasardb"
      latest: true
      variant: "linux-amd64-release"
      git_ref: "{git_ref}"
      files: ["*-c-api.tar.zst"]
```

It is not planned, not triggered and part of no key: the plan step leaves the
entry as generated, and the job takes the latest successful build of the ref
when it runs. A snapshot tree never contains one. A project whose templates
hold a latest successful dependency has no snapshot: as a root it is built
every time, at a key of each build's own, and its snapshot dependencies are
planned all the same; no project can take it as a snapshot dependency.

### What a repository changes to adopt

- Its `.buildkite/pipeline.py` hands its pipeline to the runner of
  `qdb_pipeline` (qdb-cicd-tools). The runner, on its own caller, and the plan
  step, on every dependency, apply one rule: a pipeline.py has the shape when
  its bytes contain qdb_pipeline and run(generate_pipeline). The file ends as
  the runner's README shows:

  ```python
  from qdb_pipeline import run

  ...

  if __name__ == "__main__":
      run(generate_pipeline)
  ```

- Its configuration in Buildkite runs
  `.buildkite/pipeline.py | buildkite-agent pipeline upload`: the plan step
  looks for that text in it.
- Every template of a step that uploads writes `step_id: <id>` in place of
  its key. The step ids of a commit are those its templates' steps promote
  (a promote block's `step`, else the step's own `step_id`) that some
  template writes as `step_id`; a `step_id` that is not a step id fails the
  plan step like a template that does not parse.
- Every download of another project that its generated pipeline makes is in a
  committed template, with the same type; the `git_ref` of a snapshot download
  is the build's own ref or a pin written in a committed template; and no key
  names the plugin another way (`Bureau14/qdb-artifacts`, say). The plan step
  fails a build whose pipeline breaks this rule, and names the step and why.
- A project that others take as a snapshot dependency holds no latest
  successful dependency itself.

A dependency that cannot give a snapshot fails the plan of the root before
anything is triggered. The message names who needs it, which condition failed
and the way out, which includes this one: the consumer takes it as a latest
successful dependency. A dependency whose ref its repository does not have
fails the plan the same way, without a way out. A template of a dependency
that does not parse fails the plan step with an error that names the project
and the file.

### How a build runs

1. The pipeline's first step prints the gate in place of the pipeline: an
   enter step, which carries `plan`, and a leave step, which carries `leave`.
   Both are in one concurrency group per pipeline and commit, so one build of a
   commit passes the gate at a time.
2. Enter: its command generates the pipeline, then the plugin plans. A root
   makes the plan and keeps it as meta-data of its own build; a chain node
   reads its root's. Then a build that has a key creates its `key.json`, from
   the plan alone. A build whose pipeline promotes at least one step, and
   whose every step and platform asked for is made at its key, is reused and
   builds nothing. Else the dependencies that lack a step are triggered, with
   a resume step that carries `resume`. Else the pipeline is uploaded with the
   key of every step a snapshot download takes, and its own key, written into
   it.
3. Resume: once the triggered builds have ended, it uploads the pipeline, or
   fails and names the dependency that failed.
4. The jobs run as ever. A step that promotes makes its folder at the key
   after its command passed and its upload, if it names files, was not empty.
5. Leave runs after every other step and records the steps whose job did not
   pass.

A key some of whose steps are made builds only what is missing: a job all of
whose folders are made is replaced by one that names the build that made them.

### Variables a person can set

| Variable               | Effect                                                                                                               |
| ---------------------- | -------------------------------------------------------------------------------------------------------------------- |
| `SNAPSHOT_REBUILD=1`   | The root builds again, under a key of the root build's own; its dependencies are reused.                             |
| `SNAPSHOT_REBUILD=all` | Every project of the tree builds again, each under such a key. Any other value of `SNAPSHOT_REBUILD` fails the plan. |
| `SNAPSHOT_DISABLE=1`   | The root runs its plain pipeline, without the gate (the runner reads it).                                            |

The plan step of a root asks Buildkite's API (`read_pipelines`) for the
repository and the configuration of every pipeline of its tree, with the token
in `ARTIFACTS_BUILDKITE_TOKEN`, else in the SSM parameter
`/services/buildkite/credentials/artifacts/buildkite-api-token`, and tries a
call again on no answer, 429 and 5xx, five times in all. A chain node asks
nothing.

### What is stored, and where

| What                 | Where                                                      | Written                                                         |
| -------------------- | ---------------------------------------------------------- | --------------------------------------------------------------- |
| key.json             | `<project>/<key>/key.json` in the bucket                   | once, by the plan step; without one, by each job's first upload |
| manifest             | `<project>/<key>/<platform>/<step id>/manifest.json`       | by each upload; once promoted, by the first promote             |
| branch's latest file | `<project>/refs/heads/<branch>/<platform>/<step id>`       | by each promote, but under a `snapshot/` name                   |
| failure record       | `<project>/<key>/failures/<build uuid>.json` in the bucket | once, by a gate step of a build that has a key                  |
| plan                 | meta-data `snapshot-plan` of the root build                | by the root's enter step; a retried enter writes it again       |
| marks                | meta-data `snapshot-mode` and `snapshot-promotes`          | by the enter and resume steps of each build                     |
| arrival              | meta-data `snapshot-arrived`                               | by the runner, when it prints the gate                          |

A trigger step hands the build it starts `SNAPSHOT_PLAN`, the root build's
uuid, and `SNAPSHOT_NEEDS`, the platforms and step ids its consumer
downloads. The plan goes away with the root build; what lies under a key
stays.

### What a failed build shows

A build that failed for its snapshot says why on its build page. A failure of
its own names the steps that did not pass; a dependency that failed is
followed down its failure records to the builds that failed. The annotation
ends with how to retry. For one failed build: "To retry: retry the failed job
in `<pipeline>` `<number>`. When it passes, start a new build of `<root
pipeline>`. Everything that passed is reused." For several it names each; with
none it says to start a new build of the root pipeline when the cause is
fixed.

If a snapshot fails, it fails everywhere: a step whose job hard failed fails,
without compiling it again, every build of that key in a chain whose root
build arrived at the gate more than 5 seconds before the failure was
recorded, also one that waited at the gate meanwhile and one that reaches the
dependency later. A root build that arrived later, or 5 seconds or less
before the failure, builds the key again. Within one chain a key is built at
most once.

### Limits

- The gate steps name no agents, so they run on the default queue; the steps
  that upload, download and promote run on every platform, as before.
- A plan is one meta-data value; the plugin sets no limit on its size, and a
  value that the agent refuses fails the root with the agent's message.
- A root whose pipeline is a selection (`QDB_PIPELINE_FILTER`, or a build
  message that selects), one with `SNAPSHOT_DISABLE=1`, and one on a branch its
  repository does not have (a release build on a tag) run their plain pipeline.
  A chain node never does.
- What a latest successful dependency downloads is decided when its job runs,
  as before.
- No repository may have a branch under `snapshot/`; nothing checks that.

## Resolve and download artifacts outside the plugin

A release script, deployment tool, or local utility can use `resolve_artifacts`
to find the same artifacts the plugin would download, without running Buildkite
hooks or duplicating the resolution logic. For example, resolve the C API
artifacts of the latest successful build of a given project, branch, platform
and step, then pass their object-store locations to your own downloader.

`lib/__init__.py` exposes the resolver, downloader, and their configuration types.
Install the dependencies from `lib/requirements.txt`.

The following example uses S3 with the standard AWS credential chain. Replace
`s3://my-artifacts/buildkite` with your artifact bucket/prefix; no Buildkite
environment variables or SSM configuration are required:

```python
from vendor.qdb_artifacts.lib import ObjectAuth, StoreConfig, resolve_artifacts

bucket, resolved, objects = resolve_artifacts(
    cfg=StoreConfig(backend="s3", destination="s3://my-artifacts/buildkite"),
    auth=ObjectAuth(),
    project_id="quasardb",
    variant="linux-amd64-release",
    step="build",
    git_ref="refs/heads/master",
    patterns=["*-c-api.tar.zst"],
    require_strict_ref_match=True,
)

print(f"Resolved key={resolved.key} latest file={resolved.latest}")
for artifact in objects:
    print(f"s3://{bucket}/{artifact.key}")
```

One call takes one step. `git_ref` takes the latest successful build of that
step on the ref, through the ref's latest file; `key=` in its place takes the
step at that key. `require_strict_ref_match=True` ensures this script only
selects artifacts from the requested ref. Omit it or set it to `False` to
allow the plugin's usual master/main fallback. The same `patterns`, optional
`exclude_patterns` rules are supported.

The result is `(bucket, resolved_step, objects)`. `resolved_step` is a
`ResolvedStep` with the `project`, the `folder` (platform and step id), the
`key`, its `location` in the bucket, its `manifest`, and the `latest` file it
was found through, None for a key given. Each object is a `ResolvedArtifact`
dataclass with `key`, `rel` (relative path), `size` (bytes), and
`entry_rules` fields. The `entry_rules` list contains all matching
`(entry_filter, strip_prefix)` pairs in configuration order for callers that
also want to extract selected archive entries. Project, platform, step, ref or
key, and patterns are explicit inputs rather than Buildkite-specific defaults.

### Download and extract artifacts

Use `download_artifacts` to resolve and download one step's artifacts in a
single call, without plugin configuration or a separate call to `resolve_artifacts`:

```python
from vendor.qdb_artifacts.lib import ObjectAuth, StoreConfig, download_artifacts

bucket, resolved, objects = download_artifacts(
    cfg=StoreConfig(backend="s3", destination="s3://my-artifacts/buildkite"),
    auth=ObjectAuth(),
    project_id="quasardb",
    variant="linux-amd64-release",
    step="build",
    git_ref="refs/heads/master",
    patterns=["*-c-api.tar.zst!bin/*", "*-c-api.tar.zst!lib/*"],
    require_strict_ref_match=True,
    output_dir="artifacts",
    extract=True,
)
```

This downloads each matching archive once, extracts the selected `bin/` and `lib/`
entries into `artifacts/`, and strips those prefixes. Set `extract=False` (the
default) to save the archives themselves, preserving their relative paths.
`parallel` and `concurrency` control transfers just as they do in the plugin.

The function returns the same `(bucket, resolved_step, objects)` tuple as the
resolver, after all transfers complete. It creates the output directory if needed
but does not clean it; the plugin's `clean` option remains handled by its wrapper.

To reuse the plugin's environment/SSM backend configuration, including R2,
import `load_store_config` and `resolve_object_auth` from `vendor.qdb_artifacts.lib`, and use
`load_store_config(ssm)` and
`resolve_object_auth(ssm, cfg, permission="object-read-only")` to obtain `cfg` and
`auth` instead of constructing them directly.

### Read the manifest of a step

`read_manifest` returns the `manifest.json` of a step that `resolve_artifacts`
or `download_artifacts` resolved, as a dict with the members the upload and
the promote wrote: `files`, `uploaded` and `promoted` (see
[The bucket](#the-bucket)).

```python
from vendor.qdb_artifacts.lib import read_manifest

manifest = read_manifest(cfg, auth, bucket, resolved)
```

It returns `None` when the step's folder has no manifest.

### Errors

A failure the library can explain raises `ArtifactsError`, also exported by
`vendor.qdb_artifacts.lib`, with the facts in its message: a step that cannot
be found names every latest file it tried, or the manifest that is missing at
its key; an empty selection names the patterns, the excludes and the step's
folder; a configuration error names the variable and the SSM parameter. Catch
`ArtifactsError` to handle such a failure.

Other failures raise the exception of boto3, of zstandard or of the standard
library: a request the store still refuses after its retries, an archive that
does not decompress, a tar member that would be written outside the output
directory. A Ctrl-C during the transfers of `download_artifacts` ends the
process at once with status 130, without running the caller's `finally` blocks
or exit handlers.

### Upgrading a vendored copy

A program that vendors an earlier copy of the library changes this:

- `resolve_artifacts` and `download_artifacts` take one `step` of a platform
  (`variant`), and a `git_ref` or a `key` where they took a `build_id`; they
  give a `ResolvedStep` where they gave a `ResolvedBuild`.
- `read_manifest` gives the members `files`, `uploaded` and `promoted`.
- A failure the library can explain raises `ArtifactsError` where it ended
  the process before (`SystemExit` with status 1). Catch it, or let it end the
  process.

The import path (`vendor.qdb_artifacts.lib` in the examples above) is
unchanged, and so are `ObjectAuth`, `ResolvedArtifact`, `StoreConfig`,
`aws_clients`, `download_artifacts`, `load_store_config`, `read_manifest`,
`resolve_artifacts` and `resolve_object_auth`. The library never imports
`lib/snapshot/`, and needs no PyYAML.

## Backend configuration

Config is resolved in order: **environment variable → SSM parameter → default**.

| Setting              | Env var                          | SSM parameter                                                           | Default      |
| -------------------- | -------------------------------- | ----------------------------------------------------------------------- | ------------ |
| Backend              | `ARTIFACTS_BACKEND`              | `/services/buildkite/config/artifacts/object-store/backend`             | `s3`         |
| Destination          | `ARTIFACTS_DESTINATION`          | `/services/buildkite/config/artifacts/object-store/destination`         | _(required)_ |
| Endpoint URL         | `ARTIFACTS_ENDPOINT_URL`         | `/services/buildkite/config/artifacts/object-store/endpoint-url`        | _(none)_     |
| R2 Account ID        | `ARTIFACTS_R2_ACCOUNT_ID`        | `/services/buildkite/config/artifacts/object-store/r2/account-id`       | _(R2 only)_  |
| R2 Access Key ID     | `ARTIFACTS_R2_ACCESS_KEY_ID`     | `/services/buildkite/config/artifacts/object-store/r2/access-key-id`    | _(R2 only)_  |
| R2 Secret Access Key | `ARTIFACTS_R2_SECRET_ACCESS_KEY` | `/services/buildkite/credentials/artifacts/r2/secret-access-key`        | _(R2 only)_  |
| Artifacts domain     | `ARTIFACTS_DOMAIN`               | `/services/buildkite/config/artifacts/object-store/r2/artifacts-domain` | _(optional)_ |

For **AWS S3**, the agent's IAM role is used — no explicit credentials needed.

For **Cloudflare R2**, set `ARTIFACTS_BACKEND=r2` and provide the R2 credentials above.

## Debug mode

Set `debug: true` in the plugin config to enable `set -x` tracing in all hooks:

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      step: build
      upload:
        variant: "linux-amd64-release"
        files: "artifacts/**/*.tar.zst"
      debug: true
```
