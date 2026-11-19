# qdb-artifacts-buildkite-plugin

A [Buildkite plugin](https://buildkite.com/docs/plugins) for uploading and downloading build artifacts to S3-compatible object stores (AWS S3 and Cloudflare R2).

## What it does

- **Upload** — after a build step's command completes, glob-matches local files and uploads them to `s3://<bucket>/<prefix>/<project_id>/<ref>/variants/<variant>/builds/<build_id>/…`, preserving directory structure, with optional exclusions, and generating a manifest file. This includes failed builds, so diagnostic artifacts remain available.
- **Download** — before a step's command runs, fetches artifacts produced by another step (cross-step sharing), with optional extraction and entry-level filtering. Can resolve latest successful builds and fallback to main/master branch artifacts.
- **Promote** — updates a pointer file after a successful build step, allowing downstream test steps to fetch the `LATEST_SUCCESSFUL` artifacts without knowing the specific build ID.

Uploaded artifacts are stored with `Content-Disposition: attachment` so browsers won't try to display them, instead prompting to download the file.

Artifacts are scoped by project ID, git ref, build ID, and variant, so each pipeline run is isolated and test steps can address a specific build step's output.

## Requirements

- **Python 3.13** on the host agent, named by the agent in `QDB_CICD_AGENT_PLUGIN_PYTHON`, which every agent image sets for its plugins. The plugin runs that Python and no other, none from `PATH`, and does not check its version; the plugin's pipeline tests on 3.13.
- The agent must have IAM permissions (S3) or SSM-stored R2 credentials configured. See [Backend configuration](#backend-configuration).

## Usage

### Upload (build step)

```yaml
steps:
  - label: ":hammer: Build"
    key: build-linux-amd64-release
    command: make release
    plugins:
      - bureau14/qdb-artifacts#v1.0.0:
          upload:
            variant: "linux-amd64-release"
            git_ref: "refs/heads/main"
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
          upload:
            variant: "linux-amd64-release-python"
            git_ref: "refs/heads/main"
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
      upload:
        variant: "linux-amd64-release"
        git_ref: "refs/heads/main"
        files: "dist/**/*.tar.zst"
        base_dir: "dist"
        exclude:
          - "*-debug.tar.zst"
          - "*-tests.tar.zst"
```

### Allow an empty upload

Set `upload.allow_empty: true` to skip uploading without an error when no files
match `files` or all matches are excluded. Defaults to `false`, so empty uploads
fail as before. Skipping requires no object-store configuration or credentials,
creates no manifest or upload annotation, and skips promotion when `promote` is
configured in the same step, preserving the previous `LATEST_SUCCESSFUL` pointer.

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      upload:
        variant: "linux-amd64-release"
        git_ref: "refs/heads/main"
        files: "artifacts/**/*.tar.zst"
        allow_empty: true
```

### Download (test step, cross-step, with extraction and entry filtering)

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
                git_ref: "refs/heads/main"
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

Download defaults used by the plugin:

- If `project_id` is omitted, it defaults to the current pipeline (`BUILDKITE_PIPELINE_SLUG`).
- If `build_id` is omitted and `project_id` is set to current or omitted, it defaults to `BUILDKITE_BUILD_ID`.
- If `build_id` is omitted and `project_id` is set to current or omitted, but `git_ref` is different from the current Buildkite ref, it defaults to `LATEST_SUCCESSFUL`.
- If `build_id` is omitted and `project_id` points to another pipeline, it defaults to `LATEST_SUCCESSFUL`.
- By default, downloads fall back to `refs/heads/master` and then `refs/heads/main` when the requested `git_ref` has no artifacts. Set `require_strict_ref_match: true` on a project to disable this fallback.

### Download without extraction

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      download:
        projects:
          - variant: "linux-amd64-release"
            git_ref: "refs/heads/main"
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
      upload:
        variant: "linux-amd64-release"
        git_ref: "refs/heads/main"
        project_id: quasardb
        files: "dist/**/*.tar.zst"
        parallel: 8
        concurrency: 16
```

### Upload without Buildkite annotation

Set `annotate: false` to skip creating the Buildkite job annotation after upload.

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      upload:
        variant: "linux-amd64-release"
        git_ref: "refs/heads/main"
        files: "dist/**/*.tar.zst"
        annotate: false
```

### Upload and promote in one step

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      upload:
        variant: "linux-amd64-release"
        git_ref: "refs/heads/main"
        files: "dist/**/*.tar.zst"
      promote:
        variant: "linux-amd64-release"
        git_ref: "refs/heads/main"
```

## Configuration reference

### Top-level keys

| Key          | Type    | Description                                                        |
| ------------ | ------- | ------------------------------------------------------------------ |
| `upload`     | object  | Upload configuration block.                                        |
| `download`   | object  | Download configuration block.                                      |
| `promote`    | object  | Updates the LATEST_SUCCESSFUL pointer.                             |
| `debug`      | boolean | Enable bash `set -x` debug tracing in all hooks. Default: `false`. |
| `plan`       | string  | Written by the runner (`qdb_pipeline.run`), never by a person: the gate step that enters a build with snapshot dependencies, and the path of the pipeline its command generated. See [Snapshot dependencies](#snapshot-dependencies). |
| `resume`     | string  | Written by the plan step, never by a person: the gate step that goes on once the triggered dependency builds have ended, and the path of the generated pipeline. See [Snapshot dependencies](#snapshot-dependencies). |
| `leave`      | boolean | Written by the runner (`qdb_pipeline.run`), never by a person: the gate step that runs after every other step and records which variants did not pass. See [Snapshot dependencies](#snapshot-dependencies). |

### `upload` object keys

| Key           | Type    | Required | Description                                                             |
| ------------- | ------- | -------- | ----------------------------------------------------------------------- |
| `variant`     | string  | ✓        | Name of the artifact variant (e.g. `linux-amd64-release`).              |
| `git_ref`     | string  | ✓        | Git ref to upload for (e.g. `refs/heads/main`).                         |
| `project_id`  | string  |          | Unique project identifier for namespacing artifacts (e.g. `quasardb`). Defaults to `BUILDKITE_PIPELINE_NAME`. |
| `files`       | string  | ✓        | Glob pattern for files to upload (e.g. `artifacts/**/*.tar.zst`).       |
| `exclude`     | array of strings |          | Glob patterns to skip after `files` has matched. Matched against the uploaded artifact path after `base_dir` is stripped. |
| `base_dir`    | string  |          | Optional base directory to strip from uploaded object keys (e.g. `dist`). |
| `parallel`    | integer |          | Files uploaded simultaneously. Default: `4`.                            |
| `concurrency` | integer |          | Multipart threads per upload. Default: `32`.                            |
| `annotate`    | boolean |          | Create a Buildkite job annotation listing uploaded artifacts. Default: `true`. |
| `allow_empty` | boolean |          | Skip upload without error when no files match or all matches are excluded. Default: `false`. |
| `snapshot`    | string  |          | Written by the plan step, never by a person: the key of the snapshot the build makes. See [Snapshots](#snapshots). |

### `promote` object keys

| Key          | Type   | Required | Description                                                                                   |
| ------------ | ------ | -------- | --------------------------------------------------------------------------------------------- |
| `project_id` | string |          | Unique project identifier of the artifacts to mark as latest. Defaults to `BUILDKITE_PIPELINE_NAME`.        |
| `variant`    | string | ✓        | Artifact variant to mark as latest successful build.                                          |
| `git_ref`    | string | ✓        | Git ref to promote (e.g. `refs/heads/main`).                                                  |
| `snapshot`   | string |          | Written by the plan step, never by a person: the key of the snapshot the build makes. See [Snapshots](#snapshots). |

### `download` object keys

| Key          | Type  | Required | Description                                            |
| ------------ | ----- | -------- | ------------------------------------------------------ |
| `projects`   | array | ✓        | List of projects to download artifacts from.           |
| `clean`      | boolean |        | Remove `output-dir` of all configured projects before downloading. Useful for retried jobs. Default: `false`. |
| `parallel`    | integer          |          | Files downloaded simultaneously. Default: `4`.                                                  |
| `concurrency` | integer          |          | Multipart threads per download. Default: `32`.                                                  |

#### `projects` item keys

| Key           | Type             | Required | Description                                                                                     |
| ------------- | ---------------- | -------- | ----------------------------------------------------------------------------------------------- |
| `project_id`  | string           |          | Unique project identifier of the artifacts to download. Defaults to `BUILDKITE_PIPELINE_NAME`.                  |
| `build_id`    | string           |          | Build identifier to download from. Defaults to `BUILDKITE_BUILD_ID` if `project_id` matches current pipeline and `git_ref` matches the current Buildkite ref, else `LATEST_SUCCESSFUL`. |
| `variant`     | string           | ✓        | Variant of the artifacts to download.                                                           |
| `git_ref`     | string           | ✓        | Git ref to download from (e.g. `refs/heads/main`).                                              |
| `require_strict_ref_match` | boolean | | Disable branch fallback. Default: `false`. |
| `files`       | array of strings | ✓        | Archive glob patterns, optionally with entry filters (see [Entry filtering](#entry-filtering)). |
| `exclude`     | array of strings |          | Archive glob patterns to skip after `files` patterns have matched.                              |
| `output-dir`  | string           |          | Destination directory. Default: `.` (current working directory).                                |
| `extract`     | boolean          |          | Stream-extract archives on download (no intermediate file on disk). Default: `false`.           |

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

| Hook            | Trigger                 | Action                                                                                                                                                                       |
| --------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `post-checkout` | After repo checkout     | Prints the path and version of the Python that `QDB_CICD_AGENT_PLUGIN_PYTHON` names, fails when that is not set or names nothing that runs, and makes `<plugin-dir>/.venv` anew with it, with the dependencies from `lib/requirements.txt`. |
| `pre-command`   | Before the step command | If `download` is configured, runs `artifacts.py pre-command`, which downloads (and optionally extracts) artifacts from the specified step.                                    |
| `post-command`  | After the step command  | If `upload` files or a `promote` variant are configured, runs `artifacts.py post-command`. It uploads matched files to S3/R2, including after a failed command, then promotes only after a successful command and an upload that was not empty. |

`pre-command` and `post-command` only find Python and start `artifacts.py` with the name of their phase; `lib/cli.py` decides what the phase runs.

On a gate step of a build with snapshot dependencies (option `plan`, `resume` or `leave`), `post-command` runs the gate command of the step, also after a failed command, and nothing else. `post-checkout` installs `lib/snapshot/requirements.txt` (PyYAML) besides `lib/requirements.txt` on the steps that carry `plan` or `resume`, and on no other step: no other job needs it, on any platform. Those steps make their venv in `<plugin-dir>/.venv-snapshot`: every job of one plugin commit on a host shares the plugin's checkout and makes its venv again, and no other job's venv then replaces theirs.

### Failed builds

If the build command exits with a non-zero status, `post-command` still uploads configured artifacts, making logs and other diagnostic outputs available. It never promotes those artifacts, so `LATEST_SUCCESSFUL` can only reference successful builds.

### Snapshots

A **snapshot** of a project is one commit of it together with one snapshot of
every project it depends on. Its **key** is a hash of exactly that, so it is a
function of git commits alone. The **pointer** says which build holds one
variant of a snapshot: the object
`_snapshot/v1/pointers/<project>/<key>/<variant>.json` of the bucket, beside
the projects and never inside a build.

The options `promote.snapshot` and `upload.snapshot` hold a key. The plan step
of a build with snapshot dependencies writes them into the pipeline it uploads;
a person never writes them. In a step whose `promote` block has a key and whose
command passed, promote moves `LATEST_SUCCESSFUL` and then creates the
pointer of the variant, once: the first build of the key to promote the variant
holds it, and a later build logs which build that is and leaves the pointer as
it is. Nothing changes or removes a pointer.

An upload whose block has a key adds two fields to `manifest.json`, after the
five that every upload writes:

- `source`: where the files were made,
  `{schema, repository, commit, pipeline, build_number, build_id, job_id, snapshot}`;
  `commit` is `git rev-parse HEAD` of the job's checkout.
- `downloads`: the builds the job downloaded by those of its own download
  options that name a build, `[{project_id, variant, git_ref, build_id}]`.

Without a key, upload writes the files and the manifest of five fields, and
promote writes only `LATEST_SUCCESSFUL`.

## Snapshot dependencies

A build that downloads the artifacts of another pipeline gets them from the
build of exactly the commits its root planned, and every job of a chain of
builds downloads the same build of them. A repository configures nothing for
this beyond what its step templates already say: which project each step
downloads.

### Words

- A **project** is a Buildkite pipeline, named by its slug. A **variant** is
  the files of a project for one platform, such as `linux-amd64-release`.
- A **snapshot** of a project is one commit of it together with one snapshot
  of every project it depends on. Its **key** is a hash of exactly that, so it
  is a function of git commits alone.
- The **pointer** says which build holds one variant of a snapshot. The step
  that promotes the variant writes it, once, and nothing changes it.
- A **root** is a build without `SNAPSHOT_PLAN` in its environment: one
  started by a person, a push or a schedule. Its plan step makes the **plan**:
  the commit, the key and the label of every project of its snapshot tree.
  A **chain node** is a build that a trigger step started for the plan, with
  `SNAPSHOT_PLAN` in its environment; a **chain** is a root and its chain
  nodes.
- The **label** of a node is the branch name its build runs under, uploads
  under and moves `LATEST_SUCCESSFUL` of: its own branch when the build is the
  one its own pipeline would make for that branch, else `snapshot/<root
  branch>`, a name no branch has.

### Two types of dependency

A download entry of another project in a committed template under
`.buildkite/` declares a dependency. Whether it names a build gives its type.

A **snapshot** dependency names none:

```yaml
download:
  projects:
    - project_id: "quasardb"
      variant: "linux-amd64-release"
      git_ref: "{git_ref}"
      files: ["*-c-api.tar.zst"]
```

Before any job runs, the plan step writes the build of the planned snapshot
into the entry: `build_id`, its `git_ref` and `require_strict_ref_match: true`.
A `git_ref` that is literal text, such as `refs/heads/3.14.x`, is a pin: the
project is planned at that ref for the whole chain.

A **latest successful** dependency names its build:

```yaml
download:
  projects:
    - project_id: "quasardb"
      build_id: "LATEST_SUCCESSFUL"
      variant: "linux-amd64-release"
      git_ref: "{git_ref}"
      files: ["*-c-api.tar.zst"]
```

It is not planned, not triggered and part of no key: the plan step leaves the
entry as generated, and the job downloads the newest good build of the ref,
as before. A snapshot tree never contains one. A project whose templates hold
a latest successful dependency has no snapshot: as a root it is built every
time and its snapshot dependencies are planned all the same, and no project
can take it as a snapshot dependency.

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
- Every download of another project that its generated pipeline makes is in a
  committed template, with the same type. The plan step fails a build whose
  pipeline breaks this rule, and names the step and the project.
- A project that others take as a snapshot dependency holds no latest
  successful dependency itself.

A dependency that cannot give a snapshot fails the plan of the root before
anything is triggered. The message names who needs it, which condition failed
and the way out, which always includes this one: the consumer takes it as a
latest successful dependency. A template of a dependency that does not parse
fails the plan too, and the message names the project and the file.

### How a build runs

1. The pipeline's first step prints the gate in place of the pipeline: an
   enter step, which carries `plan`, and a leave step, which carries `leave`.
   Both are in one concurrency group per pipeline and commit, so one build of a
   commit passes the gate at a time.
2. Enter: its command generates the pipeline, then the plugin plans. A root
   makes the plan and keeps it as meta-data of its own build; a chain node
   reads its root's. A build whose every variant asked for has a pointer at its
   key is reused and builds nothing. Else the dependencies that lack a variant
   are triggered, with a resume step that carries `resume`. Else the pipeline
   is uploaded with one build of every snapshot dependency written into it.
3. Resume: once the triggered builds have ended, it uploads the pipeline, or
   fails and names the dependency that failed.
4. The jobs run as ever. A step that promotes writes the pointer of its
   variant after its command passed.
5. Leave runs after every other step and records the variants whose step did
   not pass.

A key some of whose variants exist builds only what is missing: a step all of
whose variants exist is replaced by one that names the build that holds them.

### Variables a person can set

| Variable               | Effect                                                                                    |
| ---------------------- | ----------------------------------------------------------------------------------------- |
| `SNAPSHOT_REBUILD=1`   | The root builds again, under a key no earlier build has; its dependencies are reused.     |
| `SNAPSHOT_REBUILD=all` | Every node of the tree builds again. Any other value of `SNAPSHOT_REBUILD` fails the plan. |
| `SNAPSHOT_DISABLE=1`   | The root runs its plain pipeline, without the gate (the runner reads it).                 |

The plan step of a root asks Buildkite's API (`read_pipelines`) for the
repository and the configuration of every pipeline of its tree, with the token
in `ARTIFACTS_BUILDKITE_TOKEN`, else in the SSM parameter
`/services/buildkite/credentials/artifacts/buildkite-api-token`, and tries a
call again on no answer, 429 and 5xx, five times in all. A chain node asks
nothing.

### What is stored, and where

| What           | Where                                                                                | Written                                       |
| -------------- | ------------------------------------------------------------------------------------ | --------------------------------------------- |
| pointer        | `_snapshot/v1/pointers/<project>/<key>/<variant>.json` in the bucket                 | once, by the step that promotes the variant   |
| failure record | `_snapshot/v1/failures/<project>/<key>/<build uuid>.json` in the bucket              | once, by a gate step of a build that has a key |
| plan           | meta-data `snapshot-plan` of the root build                                          | once, by the root's enter step                |
| marks          | meta-data `snapshot-mode` and `snapshot-promotes`                                    | by the gate steps of each build               |
| arrival        | meta-data `snapshot-arrived`                                                         | by the runner, when it prints the gate        |

A trigger step hands the build it starts `SNAPSHOT_PLAN`, the root build's
uuid, and `SNAPSHOT_NEEDS`, the variants its consumer downloads. The plan goes
away with the root build; a pointer and a failure record stay.

### What a failed build shows

A build that failed for its snapshot says why on its build page. A failure of
its own names the steps that did not pass; a dependency that failed is
followed down its failure records to the builds that failed. The annotation
ends: "To retry: retry the failed job in `<pipeline>` `<number>`. When it
passes, start a new build of `<root pipeline>`. Everything that passed is
reused."

If a snapshot fails, it fails everywhere: a variant whose step hard failed
fails, without compiling it again, every build of that key in a chain whose
root build started before the failure was recorded, also one that waited at
the gate meanwhile and one that reaches the dependency later. Only a root
build started after the failure builds the key again. Within one chain a key
is built at most once.

### Limits

- The gate steps run on the Linux agents of the default queue; the steps that
  upload, download and promote run on every platform, as before.
- A plan is one meta-data value, which the agent takes up to 102400 bytes; a
  larger plan fails the root with the agent's message.
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
hooks or duplicating the resolution logic. For example, resolve the latest
successful C API artifacts for a given project, ref, and variant, then pass their
object-store locations to your own downloader.

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
    build_id="LATEST_SUCCESSFUL",
    git_ref="refs/heads/master",
    variant="linux-amd64-release",
    patterns=["*-c-api.tar.zst"],
    require_strict_ref_match=True,
)

print(f"Resolved ref={resolved.ref} build={resolved.build_id}")
for artifact in objects:
    print(f"s3://{bucket}/{artifact.key}")
```

`require_strict_ref_match=True` ensures this script only selects artifacts from
the requested ref. Omit it or set it to `False` to allow the plugin's usual
master/main fallback. The same `patterns`, optional `exclude_patterns` rules
are supported.

The result is `(bucket, resolved_build, objects)`. `resolved_build` identifies the
selected ref and build ID and whether fallback was applied. Each object is a
`ResolvedArtifact` dataclass with
`key`, `rel` (relative path), `size` (bytes), and `entry_rules` fields. The
`entry_rules` list contains all matching `(entry_filter, strip_prefix)` pairs in
configuration order for callers that also want to extract selected archive entries.
Project, build, ref, variant, and patterns are explicit inputs rather than
Buildkite-specific defaults.

### Download and extract artifacts

Use `download_artifacts` to resolve and download one project's artifacts in a
single call, without plugin configuration or a separate call to `resolve_artifacts`:

```python
from vendor.qdb_artifacts.lib import ObjectAuth, StoreConfig, download_artifacts

bucket, resolved, objects = download_artifacts(
    cfg=StoreConfig(backend="s3", destination="s3://my-artifacts/buildkite"),
    auth=ObjectAuth(),
    project_id="quasardb",
    build_id="LATEST_SUCCESSFUL",
    git_ref="refs/heads/master",
    variant="linux-amd64-release",
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

The function returns the same `(bucket, resolved_build, objects)` tuple as the
resolver, after all transfers complete. It creates the output directory if needed
but does not clean it; the plugin's `clean` option remains handled by its wrapper.

To reuse the plugin's environment/SSM backend configuration, including R2,
import `load_store_config` and `resolve_object_auth` from `vendor.qdb_artifacts.lib`, and use
`load_store_config(ssm)` and
`resolve_object_auth(ssm, cfg, permission="object-read-only")` to obtain `cfg` and
`auth` instead of constructing them directly.

### Read the manifest of a build

`read_manifest` returns the `manifest.json` of a build that `resolve_artifacts`
or `download_artifacts` resolved, as a dict with the fields the upload wrote:
`file_count`, `files`, `uploaded_at`, `total_size_bytes` and `base_dir`.

```python
from vendor.qdb_artifacts.lib import read_manifest

manifest = read_manifest(cfg, auth, bucket, resolved)
```

It returns `None` when the build has no manifest that reads as a JSON object,
for example when its upload failed before the end.

The manifest of a build uploaded with a snapshot key also has the fields
`source` and `downloads` (see [Snapshots](#snapshots)).

### Errors

A failure the library can explain raises `ArtifactsError`, also exported by
`vendor.qdb_artifacts.lib`, with the facts in its message: an unresolvable build
names every ref and pointer it tried, an empty selection names the patterns and
what the build holds, a configuration error names the variable and the SSM
parameter. Catch `ArtifactsError` to handle such a failure.

Other failures raise the exception of boto3, of zstandard or of the standard
library: a request the store still refuses after its retries, an archive that
does not decompress, a tar member that would be written outside the output
directory. A Ctrl-C during the transfers of `download_artifacts` ends the
process at once with status 130, without running the caller's `finally` blocks
or exit handlers.

### Upgrading a vendored copy

A program that vendors an earlier copy of the library changes this:

- A failure the library can explain raises `ArtifactsError` where it ended
  the process before (`SystemExit` with status 1). Catch it, or let it end the
  process.
- `read_manifest` is new.

The import path (`vendor.qdb_artifacts.lib` in the examples above) and the nine
names exported before are unchanged: `ObjectAuth`, `ResolvedArtifact`,
`ResolvedBuild`, `StoreConfig`, `aws_clients`, `download_artifacts`,
`load_store_config`, `resolve_artifacts` and `resolve_object_auth`. The
library never imports `lib/snapshot/`, and needs no PyYAML.

## Backend configuration

Config is resolved in order: **environment variable → SSM parameter → default**.

| Setting              | Env var                          | SSM parameter                                                        | Default      |
| -------------------- | -------------------------------- | -------------------------------------------------------------------- | ------------ |
| Backend              | `ARTIFACTS_BACKEND`              | `/services/buildkite/config/artifacts/object-store/backend`          | `s3`         |
| Destination          | `ARTIFACTS_DESTINATION`          | `/services/buildkite/config/artifacts/object-store/destination`      | _(required)_ |
| Endpoint URL         | `ARTIFACTS_ENDPOINT_URL`         | `/services/buildkite/config/artifacts/object-store/endpoint-url`     | _(none)_     |
| R2 Account ID        | `ARTIFACTS_R2_ACCOUNT_ID`        | `/services/buildkite/config/artifacts/object-store/r2/account-id`    | _(R2 only)_  |
| R2 Access Key ID     | `ARTIFACTS_R2_ACCESS_KEY_ID`     | `/services/buildkite/config/artifacts/object-store/r2/access-key-id` | _(R2 only)_  |
| R2 Secret Access Key | `ARTIFACTS_R2_SECRET_ACCESS_KEY` | `/services/buildkite/credentials/artifacts/r2/secret-access-key`     | _(R2 only)_  |
| Artifacts domain     | `ARTIFACTS_DOMAIN`             | `/services/buildkite/config/artifacts/object-store/r2/artifacts-domain`| _(optional)_ |

For **AWS S3**, the agent's IAM role is used — no explicit credentials needed.

For **Cloudflare R2**, set `ARTIFACTS_BACKEND=r2` and provide the R2 credentials above.

## Debug mode

Set `debug: true` in the plugin config to enable `set -x` tracing in all hooks:

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      upload:
        variant: "linux-amd64-release"
        git_ref: "refs/heads/main"
        files: "artifacts/**/*.tar.zst"
      debug: true
```
