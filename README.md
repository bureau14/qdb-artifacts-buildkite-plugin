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

| Key          | Type    | Description                                                                                                                                                                                                                                                                           |
| ------------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `step`       | string  | The step id of a step that uploads or promotes. Required for those steps. See [Usage](#usage).                                                                                                                                                                                        |
| `key`        | string  | Written by the plan step, never by a person: the key of the build. Without it, each job computes a key of its own build. See [The bucket](#the-bucket).                                                                                                                               |
| `upload`     | object  | Upload configuration block.                                                                                                                                                                                                                                                           |
| `download`   | object  | Download configuration block.                                                                                                                                                                                                                                                         |
| `promote`    | object  | Makes an upload of its platform official, of this step's id or of the one its `step` names, and moves the branch's latest file.                                                                                                                                                       |
| `debug`      | boolean | Enable bash `set -x` debug tracing in all hooks. Default: `false`.                                                                                                                                                                                                                    |
| `plan`       | string  | Written by the runner (`qdb_pipeline.run`), never by a person: the plan step, the first step of a build of a `pipeline.py` that hands its pipeline to the runner, and the path at which its command puts the generated pipeline. See [Snapshot dependencies](#snapshot-dependencies). |
| `plan_again` | string  | Written by the plan step, never by a person: its second run, once the builds it started for dependencies have ended, and the path of the generated pipeline. See [Snapshot dependencies](#snapshot-dependencies).                                                                     |
| `last_step`  | boolean | Written by the runner (`qdb_pipeline.run`), never by a person: the last step of a build with a plan step, which runs after every other step and records which steps did not pass. See [Snapshot dependencies](#snapshot-dependencies).                                                |

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
| `files`                    | array of strings           |          | Archive glob patterns, optionally with entry filters (see [Entry filtering](#entry-filtering)). Default: `*.tar.zst`.                                                                                                  |
| `exclude`                  | array of strings           |          | Archive glob patterns to skip after `files` patterns have matched.                                                                                                                                                     |
| `output-dir`               | string                     |          | Destination directory. Default: `.` (current working directory).                                                                                                                                                       |
| `extract`                  | boolean                    |          | Stream-extract archives on download (no intermediate file on disk). Default: `false`.                                                                                                                                  |

### Entry filtering

File patterns in `files` support an optional `!entry_filter` suffix:

```
*-server.tar.zst!bin/*
```

This matches archives by glob (`*-server.tar.zst`), extracts only entries matching `bin/*`, and strips the `bin/` prefix from output paths -- so `bin/qdb` lands at `<output-dir>/qdb`.

An entry filter without a glob character names one entry, and strips the entry's directory: `*-server.tar.zst!README` extracts `README` as `<output-dir>/README`, and `*-server.tar.zst!docs/README` extracts `docs/README` as `<output-dir>/README`.

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

| Hook            | Action                                                                                                                                                                               |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `post-checkout` | Makes the step's virtual environment with the Python that `QDB_CICD_AGENT_PLUGIN_PYTHON` names, and fails when that names nothing that runs.                                         |
| `pre-command`   | Fails a step that uploads or promotes without a step id, then downloads the files of the steps each entry takes. Every entry is resolved before a file is fetched.                   |
| `post-command`  | Uploads the matched files, also after a failed command, so that the files of a failed build can be read. Promotes only after a command that passed and an upload that was not empty. |

The plan step, its second run and the last step of a build (options `plan`, `plan_again` and `last_step`, see [Snapshot dependencies](#snapshot-dependencies)) run their own command in `post-command` and nothing else. They install `lib/planning/requirements.txt` besides `lib/requirements.txt`, into a virtual environment of their own.

### The bucket

```text
<prefix>/<project>/<key>/key.json                           what the key stands for
<prefix>/<project>/<key>/<platform>/<step id>/<files>       what one step made for one platform
<prefix>/<project>/<key>/<platform>/<step id>/manifest.json the files of that upload, and who promoted them
<prefix>/<project>/<key>/failures/<build id>.json           a build of this key failed, and why
<prefix>/<project>/refs/heads/<branch>/<platform>/<step id> the branch's latest file: its text is a key
```

- **The key** says what a build was built from: the project's commit, a dash, and 32 hex digits of the SHA-256 of `key.json`, the canonical JSON of `{"commit", "deps": [[<project>, <key>], ...], "project"}`. `deps` holds the key of every build it downloads from another project. It holds no branch or tag, so builds of the same commits share a key under any name. Reading `key.json` down from any key gives every commit of its tree. A build without a plan step computes a key of its own, with `"build": "<build id>"` in it.
- **The manifest** names a step's files and the build that uploaded them (`files`, `uploaded`), and, once a promote made them official, the build that promoted them (`promoted`). The first promote wins and nothing changes a promoted manifest: an upload into a promoted folder writes nothing. A download takes the files the manifest names.
- **The branch's latest file** names the key of the latest build of a branch that made a step for a platform. A promote writes it; the last writer wins.

### What a build shows of its downloads

For each step a download takes, the job's log names the key, its commit and the build that promoted the step, and the branch and latest file it read when it read one. The job annotates the build with the same facts.

```text
[artifacts] Download entry 1 (quasardb, linux-amd64-release), step build:
[artifacts]   branch   = main, latest file quasardb/refs/heads/main/linux-amd64-release/build
[artifacts]   key      = <commit>-<32 hex>
[artifacts]   commit   = <commit>
[artifacts]   promoted = build 1234 https://buildkite.com/<organization>/quasardb/builds/1234
```

## Snapshot dependencies

A build that downloads the artifacts of another pipeline gets them from a build of exactly the commits it planned, and every job of the build and of the builds it starts downloads them at the same keys. What is already built at those keys is not built again. A repository configures nothing beyond what its step templates say: which project each step downloads, and the step id of each step that uploads.

A **project** is a Buildkite pipeline, named by its slug; a **platform** is what `variant` names. The **plan step** is the first step of a build whose `pipeline.py` hands its pipeline to the runner (`qdb_pipeline.run`, qdb-cicd-tools); the **last step** runs after every other step. The **root build** is one a person, a push or a schedule started; its plan step makes the **plan**: the commit and key of its project and, recursively, of every snapshot dependency. A build the plan step starts for a dependency reads that plan.

### Two kinds of dependency

A download entry of another project, in a committed `.yml` or `.yaml` file under `.buildkite/`, declares a dependency.

A **snapshot** dependency is planned, and built when its key has no build yet:

```yaml
download:
  projects:
    - project_id: "quasardb"
      variant: "linux-amd64-release"
      git_ref: "{git_ref}"
      files: ["*-c-api.tar.zst"]
```

Before any job runs, the plan step writes the planned key of each step the entry takes into it, as `keys`, in place of `step`, `git_ref` and `require_strict_ref_match`. An entry without `step` takes every step id the dependency's templates promote. A `git_ref` that is literal text, such as `refs/heads/3.14.x`, is a pin: the project is planned at that ref in the whole plan.

A **latest successful** dependency says `latest: true`. It is not planned and never built: the plan step reads the key of each step it takes from the latest files of its `git_ref`, then of master and main (in a build at a tag, of its `git_ref` alone), once, and every job takes those keys. A download that finds no latest file fails the plan step. A project with a latest successful dependency cannot itself be a snapshot dependency of another.

### Where a dependency is taken from

The root build is taken at the commit its checkout holds. A dependency is taken from its pin; else:

- **in a build on a branch**, from the first of these its repository has: that branch, the release line of the root build's commit (a branch named `master`, `main` or `<digits>.<digits>.x`), `master`, `main`;
- **in a build at a tag** (started with the tag's name in Buildkite's branch field), from the same tag and nothing else.

Every build runs under the name of its git ref, so a build started for a dependency taken from `refs/heads/master` runs as `master`.

### Who writes a branch's latest files

The root build does. A build started for a dependency does when the build a person would start on its branch at its commit would take every project below it at the commit the plan takes it at: from that branch, from its release line, `master` or `main` where a repository lacks the branch, or from a pin its own templates or those below it write. The plan step hands the answer to the started build as `SNAPSHOT_BRANCH_BUILD`.

A plan step that finds steps already made at its key also points its branch's latest files at that key, when git names the build's commit as its branch's newest: so a branch cut at built commits gets its latest files at the first build on it, and an older build that runs again does not move them back.

### What the plan step requires of a repository

- Its `.buildkite/pipeline.py` ends with the runner, and its configuration in Buildkite runs `.buildkite/pipeline.py | buildkite-agent pipeline upload`:

  ```python
  from qdb_pipeline import run

  if __name__ == "__main__":
      run(generate_pipeline)
  ```

- Every template of a step that uploads writes `step_id: <id>` in place of its key.
- Every download of another project that its pipeline generates is in a committed template, of the same kind. A snapshot download writes the build's own ref (`"{git_ref}"`) or a pin; a latest successful download names a `git_ref`.
- One step promotes each step id of a platform.

The plan step fails a build whose generated pipeline breaks one of these, before anything is built, and names the step and the reason. It also fails, and names the way out, when a dependency cannot be part of a snapshot: its repository lacks the ref, its `pipeline.py` does not end with the runner, or it holds a latest successful dependency.

### How a build runs

1. The runner saves the generated pipeline as an artifact of the build and prints, in its place, the plan step and the last step. Both are in one concurrency group per pipeline and commit.
2. The plan step's command downloads that pipeline; then the plugin plans. A build whose every step is made at its key builds nothing. Else the dependencies that lack a step are started, each as a build of its own pipeline, and the plan step runs a second time when they have ended. Else the pipeline is uploaded, with the key of every step a download takes and the build's own key written into it; a step whose folders are all made is replaced by one that names the build that made them.
3. The jobs download, upload and promote at those keys.
4. The last step records the steps that did not pass.

The plan step's page shows the tree of the build twice. A picture, the build's artifact `snapshot-plan.svg`, has what is built first on the left and the build itself on the right, with an arrow from a project to each project that takes it. A table has a row for each project under the project that takes it, with its commit and, when it is taken from another branch or tag than the build's, that name; a project that several take is written out once. Below the table the page names each project whose build does not become the latest successful build of its branch (see "Who writes a branch's latest files"), and each download of the pipeline's own project at another branch: the key leaves such a download out, and its job reads that branch's latest build when it runs. The key of every project is behind the line "Snapshot keys".

### Variables

| Variable                | Set by                              | Meaning                                                                                             |
| ----------------------- | ----------------------------------- | --------------------------------------------------------------------------------------------------- |
| `SNAPSHOT_REBUILD`      | a person                            | `1`: the root build builds again, under a key of its own. `all`: so does every project of its plan. |
| `SNAPSHOT_PLAN`         | the plan step, on a build it starts | The root build's id, whose meta-data holds the plan.                                                |
| `SNAPSHOT_NEEDS`        | the same                            | The platforms and step ids the dependent downloads.                                                 |
| `SNAPSHOT_BRANCH_BUILD` | the same                            | `false`: the build writes no latest file of its branch.                                             |

The plan step of a root build asks Buildkite's API (scope `read_pipelines`) for the repository and configuration of every pipeline of its plan, with the token in `ARTIFACTS_BUILDKITE_TOKEN`, else in the SSM parameter `/services/buildkite/credentials/artifacts/buildkite-api-token`.

### When a build fails

A build that failed says why on its page. Its own steps that did not pass are named; a dependency that failed is followed down its failure records to the builds that failed, and the page ends with how to retry. A build that failed before it built because of its pipeline is no job to retry; one whose plan step could not download the pipeline is.

A failed step is not built twice for nothing: a build that needs a key whose build failed under the same plan fails at once, and so does a build that waited while another build of the same key failed. A root build started after that failure builds the key again.

### Limits

- The plan step, its second run and the last step name no agents, so they run on the default queue.
- A build of only part of its pipeline (picked by `QDB_PIPELINE_FILTER`, or by its build message) runs without the plan step; what its downloads of another project take is decided when each job runs.
- A branch of one name is taken to have left the same release line in every repository that has it. Where it did not, a build started for a dependency may write latest files that its branch's own build would not, until that build runs.

## Resolve and download artifacts outside the plugin

A release script, deployment tool, or local utility can use `resolve_artifacts`
to find the same artifacts the plugin would download, without running Buildkite
hooks or duplicating the resolution logic. For example, resolve the C API
artifacts of the latest successful build of a given project, branch, platform
and step, then pass their object-store locations to your own downloader.

`lib/__init__.py` exposes the resolver, downloader, and their configuration types.
Install the dependencies from `lib/requirements.txt`; the library never imports
`lib/planning/`, and needs no PyYAML.

The following example uses S3 with the standard AWS credential chain. Replace
`s3://my-artifacts/buildkite` with your artifact bucket/prefix; no Buildkite
environment variables or SSM configuration are required:

```python
from vendor.qdb_artifacts.lib import Branch, StoreConfig, resolve_artifacts

bucket, resolved, objects = resolve_artifacts(
    cfg=StoreConfig(backend="s3", destination="s3://my-artifacts/buildkite"),
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
from vendor.qdb_artifacts.lib import Key, StoreConfig, download_artifacts

bucket, resolved, objects = download_artifacts(
    cfg=StoreConfig(backend="s3", destination="s3://my-artifacts/buildkite"),
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
import `ssm_client` and `load_store_config` from `vendor.qdb_artifacts.lib`,
and use `load_store_config(ssm_client())` to obtain `cfg` instead of
constructing it directly. For R2 the configuration holds the access key.

### Read the manifest of a step

`read_manifest` returns the `manifest.json` of a step that `resolve_artifacts`
or `download_artifacts` resolved, as a dict with the members the upload and
the promote wrote: `files`, `uploaded` and `promoted` (see
[The bucket](#the-bucket)).

```python
from vendor.qdb_artifacts.lib import read_manifest

manifest = read_manifest(resolved)
```

### Errors

A failure the library can explain raises `ArtifactsError`, also exported by
`vendor.qdb_artifacts.lib`, with the facts in its message: a step that cannot
be found names every latest file it tried, or the manifest that is missing at
its key; a step that gives no file names the patterns, the excludes and the
step's folder; a configuration error names the variable and the SSM parameter;
a member of an archive that would be written outside the output directory
names the archive and the member, and is not tried again. Catch
`ArtifactsError` to handle such a failure.

Other failures raise the exception of boto3, of zstandard or of the standard
library: a request the store still refuses after its retries, an archive that
does not decompress. A Ctrl-C during the transfers of `download_artifacts` ends
the process at once with status 130, without running the caller's `finally`
blocks or exit handlers.

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
