# qdb-artifacts-buildkite-plugin

A [Buildkite plugin](https://buildkite.com/docs/plugins) for uploading and downloading build artifacts to S3-compatible object stores (AWS S3 and Cloudflare R2).

## What it does

- **Upload** -- after a build step's command completes, glob-matches local files and uploads them to `s3://<bucket>/<prefix>/<project>/<key>/<platform>/<step id>/...`, preserving directory structure, with optional exclusions, then writes the folder's `manifest.json`. This includes failed builds, so diagnostic artifacts remain available.
- **Download** -- before a step's command runs, fetches the files of steps of a build (cross-step sharing), with optional extraction and entry-level filtering. It takes a step at a key it is given, or the latest successful build of a branch, with a fallback to the master and main branches, and says in the job's log and on the build page which build each step came from.
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
- `keys`, which the plan step writes into an entry of another project, gives the key of each step the entry takes.
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

| Key        | Type    | Description                                                                                                                                                                                                                                                                |
| ---------- | ------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `step`     | string  | The step id of a step that uploads or promotes. Required for those steps. See [Usage](#usage).                                                                                                                                                                             |
| `key`      | string  | Written by the plan step, never by a person: the key of the build. Without it, each job computes a key of its own build. See [The bucket](#the-bucket).                                                                                                                    |
| `upload`   | object  | Upload configuration block.                                                                                                                                                                                                                                                |
| `download` | object  | Download configuration block.                                                                                                                                                                                                                                              |
| `promote`  | object  | Makes an upload of its platform official, of this step's id or of the one its `step` names, and moves the branch's latest file.                                                                                                                                            |
| `debug`    | boolean | Enable bash `set -x` debug tracing in all hooks. Default: `false`.                                                                                                                                                                                                         |
| `plan`     | string  | Written by the runner (`qdb_pipeline.run`), never by a person: the plan step, the first step of a build of a `pipeline.py` that hands its pipeline to the runner, and the path of the pipeline its command generated. See [Snapshot dependencies](#snapshot-dependencies). |
| `resume`   | string  | Written by the plan step, never by a person: its second run, once the builds it started for dependencies have ended, and the path of the generated pipeline. See [Snapshot dependencies](#snapshot-dependencies).                                                          |
| `leave`    | boolean | Written by the runner (`qdb_pipeline.run`), never by a person: the last step of a build with a plan step, which runs after every other step and records which steps did not pass. See [Snapshot dependencies](#snapshot-dependencies).                                     |

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

| Key       | Type   | Required | Description                                                                                                                                                                                              |
| --------- | ------ | -------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `variant` | string | yes      | The platform whose upload to make official: this step's, or that of the step id `step` names.                                                                                                            |
| `step`    | string |          | The step id to promote, when it is not this step's own: what another step of the same pipeline uploaded for the platform. See [Promote what another step uploaded](#promote-what-another-step-uploaded). |

### `download` object keys

| Key           | Type    | Required | Description                                                                                                   |
| ------------- | ------- | -------- | ------------------------------------------------------------------------------------------------------------- |
| `projects`    | array   | yes      | List of entries to download, each of one project and platform.                                                |
| `clean`       | boolean |          | Remove `output-dir` of all configured projects before downloading. Useful for retried jobs. Default: `false`. |
| `parallel`    | integer |          | Files downloaded simultaneously. Default: `4`.                                                                |
| `concurrency` | integer |          | Multipart threads per download. Default: `32`.                                                                |

#### `projects` item keys

| Key                        | Type                       | Required | Description                                                                                                                                                                                                            |
| -------------------------- | -------------------------- | -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `project_id`               | string                     |          | Unique project identifier of the artifacts to download. Defaults to `BUILDKITE_PIPELINE_SLUG`.                                                                                                                         |
| `variant`                  | string                     | yes      | The platform to download.                                                                                                                                                                                              |
| `step`                     | string or array of strings |          | The step ids to download. Required from the build's own key; without it, an entry of another project takes every step.                                                                                                 |
| `keys`                     | object                     |          | Written by the plan step, never by a person: the key of each step the entry takes.                                                                                                                                     |
| `latest`                   | boolean                    |          | The entry takes the latest successful build of another project, which is not planned: the plan step reads its keys.                                                                                                    |
| `git_ref`                  | string                     |          | The ref of the download (e.g. `refs/heads/main`): of a planned dependency the build's own ref or a pin; else the branch whose latest successful build to take. Required in an entry of another project without `keys`. |
| `require_strict_ref_match` | boolean                    |          | Take the latest successful build of `git_ref` alone, never of master or main. Default: `false`.                                                                                                                        |
| `files`                    | array of strings           | yes      | Archive glob patterns, optionally with entry filters (see [Entry filtering](#entry-filtering)).                                                                                                                        |
| `exclude`                  | array of strings           |          | Archive glob patterns to skip after `files` patterns have matched.                                                                                                                                                     |
| `output-dir`               | string                     |          | Destination directory. Default: `.` (current working directory).                                                                                                                                                       |
| `extract`                  | boolean                    |          | Stream-extract archives on download (no intermediate file on disk). Default: `false`.                                                                                                                                  |

### Entry filtering

File patterns in `files` support an optional `!entry_filter` suffix:

```
*-server.tar.zst!bin/*
```

This matches archives by glob (`*-server.tar.zst`), extracts only entries matching `bin/*`, and strips the `bin/` prefix from output paths -- so `bin/qdb` lands at `<output-dir>/qdb`.

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

| Hook            | Trigger                 | Action                                                                                                                                                                                                                                                                                                     |
| --------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `post-checkout` | After repo checkout     | Prints the path and version of the Python that `QDB_CICD_AGENT_PLUGIN_PYTHON` names, fails when that is not set or names nothing that runs, and makes the step's venv anew with it: `<plugin-dir>/.venv` with the dependencies from `lib/requirements.txt`, or the venv of the plan step, described below. |
| `pre-command`   | Before the step command | If `download`, `upload` files or a `promote` platform are configured, runs `artifacts.py pre-command`, which fails a step that uploads or promotes without a step id, then downloads (and optionally extracts) the files of the steps each entry takes.                                                    |
| `post-command`  | After the step command  | If `upload` files or a `promote` platform are configured, runs `artifacts.py post-command`. It uploads matched files to S3/R2, including after a failed command, then promotes only after a successful command and an upload that was not empty.                                                           |

`pre-command` and `post-command` start `artifacts.py` with the Python of the venv that `post-checkout` made, and the name of their phase; `lib/cli.py` decides what the phase runs.

On the plan step, its second run and the last step of a build (option `plan`, `resume` or `leave`), `post-command` runs that step's command of the plugin, also after a failed command, and nothing else. `post-checkout` installs `lib/snapshot/requirements.txt` (PyYAML) besides `lib/requirements.txt` on the steps that carry `plan` or `resume`, and on no other step: no other job needs it, on any platform. Those steps make their venv in `<plugin-dir>/.venv-snapshot`: every job of one plugin commit on a host shares the plugin's checkout and makes its venv again, and no other job's venv then replaces theirs.

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
`deps` holds the key of the build taken of each snapshot dependency, and the
key read for each step a latest successful download takes, each pair once,
sorted. It holds no name of a branch or a tag, so builds of one commit that
take the same keys share a key under any name. A key of one build's own also
holds `"build": "<build id>"`, so that no other build has it. The plan step of
the first build of a chain computes the key of every build of the chain; a
build started for a dependency takes its key from the plan. Each build's plan
step writes its key as the option `key` into every configuration of the
plugin, and creates `key.json` once. A build without a plan step has no `key`
option: each of its jobs computes the key of its own build (its project, the
commit of its checkout, no dependency, its build id), and its first upload
creates that `key.json`.

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
under `refs/heads/<BUILDKITE_BRANCH>`, also when that name is a tag's; the
last writer wins. The plan step writes it too, for each step it finds made at
its key, when git names the build's commit as the newest of its branch, or as
its tag's: so a branch cut at, or a tag put on, built commits gets its latest
files at its first build. A build under a `snapshot/` name, which a plan step
starts for a dependency, writes none.

### What a build shows of its downloads

The job's log names, for each download entry and each step it takes, as soon
as it finds the step and before its files: the branch and the latest file it
read, when it read one, which a job does for an entry of its own project at
another branch and for an entry of another project in a build without a plan
step; the key; the commit, which is the key's first 40 hex digits; and the
build that promoted the step, by number and URL. An entry none of whose steps
gives a file fails after these lines. A step of the job's own build that a
later step promotes is still captured when it is read, and its line names the
build that uploaded it.

```text
[artifacts] Download entry 1 (quasardb, linux-amd64-release), step build:
[artifacts]   branch   = main, latest file quasardb/refs/heads/main/linux-amd64-release/build
[artifacts]   key      = <commit>-<32 hex>
[artifacts]   commit   = <commit>
[artifacts]   promoted = build 1234 https://buildkite.com/<organization>/quasardb/builds/1234
```

After its last entry the job annotates the build, under the context
`artifacts-downloads`, with the same facts, one line per entry and step; an
upload of the same job annotates under the context `artifacts`, so neither
replaces the other. A failed annotation is logged and fails nothing.

## Snapshot dependencies

A build that downloads the artifacts of another pipeline gets them from the
build of exactly the commits the first build of its chain planned, and every
job of a chain of builds downloads them at the same keys. A repository
configures nothing for this beyond what its step templates already say: which
project each step downloads, and the step id of each step that uploads.

### Words

- A **project** is a Buildkite pipeline, named by its slug. A **platform** is
  what the option `variant` names, such as `linux-amd64-release`; a **step
  id** names what one step uploads for one platform.
- The **key** of a build says what it was built from (see
  [The bucket](#the-bucket)). A step is **made** at a key when its manifest
  there says promoted; nothing changes a promoted manifest.
- The **plan step** is the first step of a build of a `pipeline.py` that
  hands its pipeline to the runner; its **second run** goes on once the builds
  it started have ended, and the **last step** runs after every other step.
  The name of a build is Buildkite's branch field, `BUILDKITE_BRANCH`.
- The **first build of a chain** is a build with a plan step that no plan
  step started: one a person, a push or a schedule started, without
  `SNAPSHOT_PLAN` in its environment. Its plan step makes the **plan**: the
  commit, the key, the step ids and the name a build runs under of every
  project of its snapshot tree, and the keys it read for its own latest
  successful downloads. A **build started for a dependency** is one a plan
  step started, with `SNAPSHOT_PLAN`, the first build's id, in its
  environment; a **chain** is a first build and the builds started for it.
- A build started for a dependency runs under the name of the ref the plan
  took it from, a branch's or a tag's, when it is the build that project's own
  pipeline would make under that name; else under `snapshot/<first build's
name>`, a name no branch has, under which it writes no latest file.

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

It is not planned and not triggered. The plan step of the first build reads
it once, before it plans: the key of each step it takes, through the latest
files of the ref, then of master and main, as a job would (see
[Download](#download-cross-project-with-extraction-and-entry-filtering)), or
of the ref alone in a build at a tag. It writes those keys into the entry as
`keys`, into the first build's `key.json` and into the plan, so that every
job of the first build and its second run take the same keys, and the first
build is reused while the latest files name the same keys. A download that
finds no latest file fails the plan step before anything runs, and names the
latest files, or for an entry without `step` the folders, it read. No project
can take a project that holds a latest successful dependency as a snapshot
dependency: such a project is planned only as the first build of a chain.

### Where each dependency is taken from

The plan step of a first build asks git once whether its name is a branch or
a tag of its repository, with the commit of each, an annotated tag peeled. A
name that is both is the branch.

- **On a branch**, a dependency without a pin is taken from the first of
  these that its repository has: the branch of that name, the release line
  of the first build's commit (a branch named `master`, `main` or
  `<digits>.<digits>.x`), `master`, `main`.
- **At a tag** (a build at a tag: start it with the tag's name in Buildkite's
  branch field, and its tag field empty), every dependency without a pin is
  taken from the same tag and from nothing else. A repository without the tag
  fails the plan step before anything runs, and the message names it. A
  latest successful download reads the latest files of its own `git_ref`
  alone, which is the tag's name when the template writes `{git_ref}`.
- **Under a name that is neither** (a Rebuild of a build of a deleted
  branch), the build is planned as on the branch it names; git names no
  commit for it, so its plan step writes no latest file of a step it finds
  made.

A pin wins over each of these. The first build itself is taken at the commit
its checkout holds. A build at a tag reuses what any build made of the same
commits, and its promotes write the latest files of the tag's name,
`refs/heads/<tag>`.

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
  is the build's own ref, `refs/heads/<BUILDKITE_BRANCH>`, or a pin written in
  a committed template; every upload and promote names a step id; and no key
  names the plugin another way (`Bureau14/qdb-artifacts`, say). The plan step
  fails a build whose pipeline breaks this rule, and names the step and why;
  a download at `refs/tags/<tag>` comes from Buildkite's tag field, and the
  message says to start the build with the tag's name in the branch field.
- Every step id its pipeline promotes is one its templates write as
  `step_id`, since the builds that depend on it read its steps from them; one
  step promotes each step id of a platform.
- A snapshot download without `step` takes a dependency whose templates
  promote step ids; of one that promotes none, it names the steps or goes.
- A project that others take as a snapshot dependency holds no latest
  successful dependency itself.

A build that breaks one of these fails before anything is built. A build
started for a dependency also writes a failure record of reason `rule` under
its key; a first build writes one only for what its plan step checks once the
plan has given it its key: the step ids its pipeline promotes and its
downloads without `step`.

A dependency that cannot give a snapshot fails the plan of the first build
before anything is triggered. The message names who needs it, which condition
failed and the way out, which includes this one: the dependent takes it as a
latest successful dependency. A dependency whose ref its repository does not
have fails the plan the same way. A template of a dependency that does not
parse fails the plan step with an error that names the project and the file.

A build started for a dependency that is asked for a step one of its platforms
does not make refuses it before anything is built, and its failure record
names the steps refused and what it makes on that platform. The build that
asked fails and names the template of its entry and the steps the entry takes:
the list to write in it, as `step: [build]`, or, on a platform for which the
dependency makes nothing, the download to remove. So does the first build.

### How a build runs

1. The runner prints, in place of the pipeline, the plan step, which carries
   `plan`, and the last step, which carries `leave`. Both are in one
   concurrency group per pipeline and commit, so the builds of one commit
   pass the plan step one after the other.
2. The plan step: its command generates the pipeline, then the plugin plans. A
   first build asks git where its name is, reads the keys of its latest
   successful downloads, makes the plan and keeps it as meta-data of its own
   build; a build started for a dependency reads the first build's. Then the
   build creates its `key.json`, from the plan alone. When git names its
   commit as the newest of its branch, or as its tag's, it points the latest
   files of its name at its key for every step made there (see
   [The bucket](#the-bucket)). A build whose pipeline promotes at least one
   step, and whose every step and platform asked for is made at its key, is
   reused and builds nothing. Else the dependencies that lack a step are
   triggered, with the plan step's second run, which carries `resume`. Else
   the pipeline is uploaded with the key of every step a download of another
   project takes, and its own key, written into it.
3. The second run: once the triggered builds have ended, it uploads the
   pipeline, or fails and names the dependency that failed.
4. The jobs download, upload and promote at those keys. A step that promotes
   makes its folder at the key after its command passed and its upload, if it
   names files, was not empty.
5. The last step runs after every other step and records the steps whose job
   did not pass.

A key some of whose steps are made builds only what is missing: a job all of
whose folders are made is replaced by one that names the build that made them.

The page of a plan step, of a first build or of a build started for a
dependency alike, lists the build's project and each project below it in the
plan, with its commit, ref, key and the name its build runs under, and each
download of the pipeline's own project at another branch, which its job reads
when it runs: the key leaves such a download out, so a reused build does not
take it again.

### Variables a person can set

| Variable               | Effect                                                                                                                                   |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `SNAPSHOT_REBUILD=1`   | The first build builds again, under a key of its own; its dependencies are reused.                                                       |
| `SNAPSHOT_REBUILD=all` | Every project of the tree builds again, each under a key of the first build's own. Any other value of `SNAPSHOT_REBUILD` fails the plan. |

A forced build serves its own chain: later builds of the same commits take the
earlier keys. Its promotes under a branch's own name move that branch's latest
files to the forced key, until the next build of that project at the branch's
newest commit points them back at the earlier one.

The plan step of a first build asks Buildkite's API (`read_pipelines`) for the
repository and the configuration of every pipeline of its tree, with the token
in `ARTIFACTS_BUILDKITE_TOKEN`, else in the SSM parameter
`/services/buildkite/credentials/artifacts/buildkite-api-token`, and tries a
call again on no answer, 429 and 5xx, five times in all. A build started for a
dependency asks nothing of Buildkite's API.

### What is stored, and where

| What                 | Where                                                      | Written                                                                                                                                              |
| -------------------- | ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| key.json             | `<project>/<key>/key.json` in the bucket                   | once, by the plan step; without one, by each job's first upload                                                                                      |
| manifest             | `<project>/<key>/<platform>/<step id>/manifest.json`       | by each upload; once promoted, by the first promote                                                                                                  |
| branch's latest file | `<project>/refs/heads/<name>/<platform>/<step id>`         | by each promote, and by the plan step for the steps it finds made at the newest commit of its branch or at its tag's; never under a `snapshot/` name |
| failure record       | `<project>/<key>/failures/<build uuid>.json` in the bucket | once, by the plan step, its second run or the last step of a build that has its plan                                                                 |
| plan                 | meta-data `snapshot-plan` of the first build               | by the first build's plan step; a retried plan step writes it again                                                                                  |
| state                | meta-data `snapshot-mode` and `snapshot-promotes`          | by the plan step and its second run, for the last step                                                                                               |
| arrival              | meta-data `snapshot-arrived`                               | by the runner, when it prints the plan step                                                                                                          |

A trigger step hands the build it starts `SNAPSHOT_PLAN`, the first build's
uuid, and `SNAPSHOT_NEEDS`, the platforms and step ids its dependent
downloads. The plan goes away with the first build; what lies under a key
stays.

### What a failed build shows

A build that failed for its snapshot says why on its build page. A failure of
its own names the steps that did not pass; a dependency that failed is
followed down its failure records to the builds that failed. The annotation
ends with how to retry. For one failed build: "To retry: retry the failed job
in `<pipeline>` `<number>`. When it passes, start a new build of `<first
build's pipeline>`. Everything that passed is reused." For several it names
each; with none it says to start a new build of the first build's pipeline
when the cause is fixed. A build that failed before it built, for its
generator, its rule or a step it does not make, is no job to retry: a retry
cures none of these.

If a snapshot fails, it fails everywhere: a step whose job hard failed fails,
without compiling it again, every build of that key in a chain whose first
build arrived at the plan step more than 5 seconds before the failure was
recorded, also one that waited at the plan step meanwhile and one that
reaches the dependency later. A first build that arrived later, or 5 seconds
or less before the failure, builds the key again. Within one chain a key is
built at most once.

### Limits

- The plan step, its second run and the last step name no agents, so they run
  on the default queue.
- A plan is one meta-data value; the plugin sets no limit on its size, and a
  value that the agent refuses fails the first build with the agent's message.
- A build of only part of its pipeline (picked by `QDB_PIPELINE_FILTER`, or by
  its build message) runs without the plan step.
- In a build without a plan step, what a download of another project takes is
  decided when its job runs.
- No repository may have a branch under `snapshot/`; nothing checks that.

## Resolve and download artifacts outside the plugin

A release script, deployment tool, or local utility can use `resolve_artifacts`
to find the same artifacts the plugin would download, without running Buildkite
hooks or duplicating the resolution logic. For example, resolve the C API
artifacts of the latest successful build of a given project, branch, platform
and step, then pass their object-store locations to your own downloader.

`lib/__init__.py` exposes the resolver, downloader, and their configuration types.
Install the dependencies from `lib/requirements.txt`; the library never imports
`lib/snapshot/`, and needs no PyYAML.

The following example uses S3 with the standard AWS credential chain. Replace
`s3://my-artifacts/buildkite` with your artifact bucket/prefix; no Buildkite
environment variables or SSM configuration are required:

```python
from vendor.qdb_artifacts.lib import Branch, ObjectAuth, StoreConfig, resolve_artifacts

bucket, resolved, objects = resolve_artifacts(
    cfg=StoreConfig(backend="s3", destination="s3://my-artifacts/buildkite"),
    auth=ObjectAuth(),
    project_id="quasardb",
    variant="linux-amd64-release",
    step="build",
    build=Branch("refs/heads/master", strict=True),
    patterns=["*-c-api.tar.zst"],
)

print(f"Resolved key={resolved.key} latest file={resolved.latest}")
for artifact in objects:
    print(f"s3://{bucket}/{artifact.key}")
```

One call takes one step, of the build `build` names, which the call must name:
`Branch(git_ref)` takes the latest successful build of that step on the ref,
through the ref's latest file, and `Key(key)` the step at that key.
`strict=True` ensures this script only selects artifacts from the requested
ref; without it a `Branch` falls back to the plugin's usual master and main.
The same `patterns`, optional `exclude_patterns` rules are supported. The call
logs, as a download does, the key it took, its commit and the build that
promoted the step.

The result is `(bucket, resolved_step, objects)`. `resolved_step` is a
`ResolvedStep` with the `project`, the `folder` (platform and step id), the
`key`, its `location` in the bucket, its `manifest`, and the `ref` and
`latest` file it was found through, both None for a key given. Each object is
a `ResolvedArtifact` dataclass with `key`, `rel` (relative path), `size`
(bytes), and `entry_rules` fields. The `entry_rules` list contains all
matching `(entry_filter, strip_prefix)` pairs in configuration order for
callers that also want to extract selected archive entries. Project,
platform, step, build, and patterns are explicit inputs rather than
Buildkite-specific defaults.

### Download and extract artifacts

Use `download_artifacts` to resolve and download one step's artifacts in a
single call, without plugin configuration or a separate call to `resolve_artifacts`:

```python
from vendor.qdb_artifacts.lib import Key, ObjectAuth, StoreConfig, download_artifacts

bucket, resolved, objects = download_artifacts(
    cfg=StoreConfig(backend="s3", destination="s3://my-artifacts/buildkite"),
    auth=ObjectAuth(),
    project_id="quasardb",
    variant="linux-amd64-release",
    step="build",
    build=Key("<commit>-<32 hex>"),
    patterns=["*-c-api.tar.zst!bin/*", "*-c-api.tar.zst!lib/*"],
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
its key; a step that gives no file names the patterns, the excludes and the
step's folder; a configuration error names the variable and the SSM parameter. Catch
`ArtifactsError` to handle such a failure.

Other failures raise the exception of boto3, of zstandard or of the standard
library: a request the store still refuses after its retries, an archive that
does not decompress, a tar member that would be written outside the output
directory. A Ctrl-C during the transfers of `download_artifacts` ends the
process at once with status 130, without running the caller's `finally` blocks
or exit handlers.

## Backend configuration

Config is resolved in order: **environment variable -> SSM parameter -> default**.

| Setting              | Env var                          | SSM parameter                                                           | Default      |
| -------------------- | -------------------------------- | ----------------------------------------------------------------------- | ------------ |
| Backend              | `ARTIFACTS_BACKEND`              | `/services/buildkite/config/artifacts/object-store/backend`             | `s3`         |
| Destination          | `ARTIFACTS_DESTINATION`          | `/services/buildkite/config/artifacts/object-store/destination`         | _(required)_ |
| Endpoint URL         | `ARTIFACTS_ENDPOINT_URL`         | `/services/buildkite/config/artifacts/object-store/endpoint-url`        | _(none)_     |
| R2 Account ID        | `ARTIFACTS_R2_ACCOUNT_ID`        | `/services/buildkite/config/artifacts/object-store/r2/account-id`       | _(R2 only)_  |
| R2 Access Key ID     | `ARTIFACTS_R2_ACCESS_KEY_ID`     | `/services/buildkite/config/artifacts/object-store/r2/access-key-id`    | _(R2 only)_  |
| R2 Secret Access Key | `ARTIFACTS_R2_SECRET_ACCESS_KEY` | `/services/buildkite/credentials/artifacts/r2/secret-access-key`        | _(R2 only)_  |
| Artifacts domain     | `ARTIFACTS_DOMAIN`               | `/services/buildkite/config/artifacts/object-store/r2/artifacts-domain` | _(optional)_ |

For **AWS S3**, the agent's IAM role is used -- no explicit credentials needed.

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
