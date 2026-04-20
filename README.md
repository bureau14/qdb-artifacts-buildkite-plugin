# qdb-artifacts-buildkite-plugin

A [Buildkite plugin](https://buildkite.com/docs/plugins) for uploading and downloading build artifacts to S3-compatible object stores (AWS S3 and Cloudflare R2).

## What it does

- **Upload** — after a build step's command completes successfully, glob-matches local files and uploads them to `s3://<bucket>/<prefix>/<project_id>/<ref>/variants/<step_key>/builds/<build_id>/…`, preserving directory structure, and generating a manifest file.
- **Download** — before a step's command runs, fetches artifacts produced by another step (cross-step sharing), with optional streaming in-memory extraction and entry-level filtering. Can resolve latest successful builds and fallback to main/master branch artifacts.
- **Promote** — updates a pointer file after a successful build step, allowing downstream test steps to fetch the `LATEST_SUCCESSFUL` artifacts without knowing the specific build ID.

Artifacts are scoped by project ID, git ref, build ID, and step key, so each pipeline run is isolated and test steps can address a specific build step's output.

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
            project_id: quasardb
            files: "artifacts/**/*.tar.zst"
```

### Download (test step, cross-step, with extraction and entry filtering)

```yaml
steps:
  - label: ":test_tube: Test"
    command: ./run-tests.sh
    plugins:
      - bureau14/qdb-artifacts#v1.0.0:
          download:
            project_id: quasardb
            step: build-linux-amd64-release
            output-dir: artifacts
            extract: true
            clean: true
            files:
              - "*-c-api.tar.zst!lib/*"
              - "*-server.tar.zst!bin/*"
              - "*-tests.tar.zst!bin/*"
```

### Download without extraction

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      download:
        project_id: quasardb
        step: build-linux-amd64-release
        output-dir: dist
        files:
          - "*.tar.zst"
```

### Upload with tuned parallelism

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      upload:
        project_id: quasardb
        files: "dist/**/*.tar.zst"
        parallel: 8
        concurrency: 16
```

## Configuration reference

### Top-level keys

| Key          | Type    | Description                                                        |
| ------------ | ------- | ------------------------------------------------------------------ |
| `upload`     | object  | Upload configuration block. Mutually exclusive with `download` and `promote`. |
| `download`   | object  | Download configuration block. Mutually exclusive with `upload` and `promote`. |
| `promote`    | object  | Updates the LATEST_SUCCESSFUL pointer. Mutually exclusive.         |
| `debug`      | boolean | Enable bash `set -x` debug tracing in all hooks. Default: `false`. |

### `upload` object keys

| Key           | Type    | Required | Description                                                             |
| ------------- | ------- | -------- | ----------------------------------------------------------------------- |
| `project_id`  | string  |          | Unique project identifier for namespacing artifacts (e.g. `quasardb`). Defaults to `BUILDKITE_PIPELINE_NAME`. |
| `files`       | string  | ✓        | Glob pattern for files to upload (e.g. `artifacts/**/*.tar.zst`).       |
| `parallel`    | integer |          | Files uploaded simultaneously. Default: `4`.                            |
| `concurrency` | integer |          | Multipart threads per upload. Default: `32`.                            |

### `promote` object keys

| Key          | Type   | Required | Description                                                                                   |
| ------------ | ------ | -------- | --------------------------------------------------------------------------------------------- |
| `project_id` | string |          | Unique project identifier of the artifacts to mark as latest. Defaults to `BUILDKITE_PIPELINE_NAME`.        |
| `step`       | string | ✓        | Artifact path to mark as latest successful build of the current step.                         |

### `download` object keys

| Key           | Type             | Required | Description                                                                                     |
| ------------- | ---------------- | -------- | ----------------------------------------------------------------------------------------------- |
| `project_id`  | string           |          | Unique project identifier of the artifacts to download. Defaults to `BUILDKITE_PIPELINE_NAME`.                  |
| `build_id`    | string           |          | Build identifier to download from. Defaults to `BUILDKITE_BUILD_ID` if `project_id` matches current pipeline, else `LATEST_SUCCESSFUL`. |
| `step`        | string           | ✓        | Key of the build step to download artifacts from (cross-step).                                  |
| `ref`         | string           |          | Override the git ref to download from (e.g. `refs/heads/main`). Defaults to the current ref.    |
| `files`       | array of strings | ✓        | Archive glob patterns, optionally with entry filters (see [Entry filtering](#entry-filtering)). |
| `output-dir`  | string           |          | Destination directory. Default: `.` (current working directory).                                |
| `extract`     | boolean          |          | Stream-extract archives on download (no intermediate file on disk). Default: `false`.           |
| `clean`       | boolean          |          | Remove `output-dir` before downloading. Useful for retried jobs. Default: `false`.              |
| `parallel`    | integer          |          | Files downloaded simultaneously. Default: `4`.                                                  |
| `concurrency` | integer          |          | Multipart threads per download. Default: `32`.                                                  |

### Entry filtering

File patterns in `files` support an optional `!entry_filter` suffix:

```
*-server.tar.zst!bin/*
```

This matches archives by glob (`*-server.tar.zst`), extracts only entries matching `bin/*`, and strips the `bin/` prefix from output paths — so `bin/qdb` lands at `<output-dir>/qdb`.

Supported archive formats for extraction: `.tar.gz`, `.tar.zst` / `.tar.zstd`, `.zip`.

## How it works

| Hook            | Trigger                 | Action                                                                                                                                                                       |
| --------------- | ----------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `post-checkout` | After repo checkout     | Creates/updates a Python venv in `<plugin-dir>/.venv` with dependencies from `lib/requirements.txt`. Uses `flock` to prevent races when multiple jobs run on the same agent. |
| `pre-command`   | Before the step command | If `download` is configured, downloads (and optionally extracts) artifacts from the specified step.                                                                          |
| `post-command`  | After the step command  | If `upload` is configured **and** the command succeeded, uploads matched files to S3/R2.                                                                                     |

### Upload skipped on failure

If the build command exits with a non-zero status, `post-command` skips the upload and prints a warning. This prevents broken builds from polluting the artifact namespace that downstream test steps consume.

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

For **AWS S3**, the agent's IAM role is used — no explicit credentials needed.

For **Cloudflare R2**, set `ARTIFACTS_BACKEND=r2` and provide the R2 credentials above.

## Debug mode

Set `debug: true` in the plugin config to enable `set -x` tracing in all hooks:

```yaml
plugins:
  - bureau14/qdb-artifacts#v1.0.0:
      upload:
        files: "artifacts/**/*.tar.zst"
      debug: true
```

