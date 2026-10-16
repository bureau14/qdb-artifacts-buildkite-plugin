# qdb-artifacts-buildkite-plugin

A [Buildkite plugin](https://buildkite.com/docs/plugins) for uploading and downloading build artifacts to S3-compatible object stores (AWS S3 and Cloudflare R2).

## What it does

- **Upload** — after a build step's command completes, glob-matches local files and uploads them to `s3://<bucket>/<prefix>/<project_id>/<ref>/variants/<variant>/builds/<build_id>/…`, preserving directory structure, with optional exclusions, and generating a manifest file. This includes failed builds, so diagnostic artifacts remain available.
- **Download** — before a step's command runs, fetches artifacts produced by another step (cross-step sharing), with optional extraction and entry-level filtering. Can resolve latest successful builds and fallback to main/master branch artifacts.
- **Promote** — updates a pointer file after a successful build step, allowing downstream test steps to fetch the `LATEST_SUCCESSFUL` artifacts without knowing the specific build ID.

Uploaded artifacts are stored with `Content-Disposition: attachment` so browsers won't try to display them, instead prompting to download the file.

Artifacts are scoped by project ID, git ref, build ID, and variant, so each pipeline run is isolated and test steps can address a specific build step's output.

## Requirements

- **Python 3** must be available on the host agent (`python3` in `PATH`).
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

### `promote` object keys

| Key          | Type   | Required | Description                                                                                   |
| ------------ | ------ | -------- | --------------------------------------------------------------------------------------------- |
| `project_id` | string |          | Unique project identifier of the artifacts to mark as latest. Defaults to `BUILDKITE_PIPELINE_NAME`.        |
| `variant`    | string | ✓        | Artifact variant to mark as latest successful build.                                          |
| `git_ref`    | string | ✓        | Git ref to promote (e.g. `refs/heads/main`).                                                  |

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
| `post-checkout` | After repo checkout     | Creates/updates a Python venv in `<plugin-dir>/.venv` with dependencies from `lib/requirements.txt`. Uses `flock` to prevent races when multiple jobs run on the same agent. |
| `pre-command`   | Before the step command | If `download` is configured, downloads (and optionally extracts) artifacts from the specified step.                                                                          |
| `post-command`  | After the step command  | If `upload` is configured, uploads matched files to S3/R2, including after a failed command. Promotion runs only after a successful command.                                  |

### Failed builds

If the build command exits with a non-zero status, `post-command` still uploads configured artifacts, making logs and other diagnostic outputs available. It never promotes those artifacts, so `LATEST_SUCCESSFUL` can only reference successful builds.

## Resolve artifacts outside the plugin

A release script, deployment tool, or local utility can use `resolve_artifacts`
to find the same artifacts the plugin would download, without running Buildkite
hooks or duplicating the resolution logic. For example, resolve the latest
successful C API artifacts for a given project, ref, and variant, then pass their
object-store locations to your own downloader.

With this repository checked out as a submodule at `vendor/qdb_artifacts`,
`lib/__init__.py` exposes the resolver and its configuration types. Install the
dependencies from `vendor/qdb_artifacts/lib/requirements.txt` and keep the consuming
repository root on Python's import path.

The following example uses S3 with the normal AWS credential chain. Replace
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
master/main fallback. The same `patterns`, optional `exclude_patterns`rules
are supported outside the plugin.

The result is `(bucket, resolved_build, objects)`. `resolved_build` identifies the
selected ref and build ID and whether fallback was applied. Each object is a
`ResolvedArtifact` dataclass with
`key`, `rel` (relative path), `size` (bytes), and `entry_rules` fields. The
`entry_rules` list contains all matching `(entry_filter, strip_prefix)` pairs in
configuration order for callers that also want to extract selected archive entries.
Project, build, ref, variant, and patterns are explicit inputs rather than
Buildkite-specific defaults.

To reuse the plugin's environment/SSM backend configuration, including R2,
import `load_store_config` and `resolve_object_auth` from `vendor.qdb_artifacts.lib`, and use
`load_store_config(ssm)` and
`resolve_object_auth(ssm, cfg, permission="object-read-only")` to obtain `cfg` and
`auth` instead of constructing them directly.

Resolution reads object-store metadata and pointer/manifest files but does not
download artifact payloads, extract archives, clean directories, or create local
files. It logs resolution details to stderr.

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
