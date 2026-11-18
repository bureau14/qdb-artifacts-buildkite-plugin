"""The command line the hooks call. Its commands:

    upload [--report-empty]   exits 3 instead of 0 when allow_empty skipped
                              an empty upload, so the hook leaves the pointer
    download
    promote --variant V --git-ref R [--project-id P] [--build-id B]

The step's options come from the environment, as the agent sets them. An
ArtifactsError of the library ends the command with "[artifacts] ERROR:
<message>" and exit status 1.
"""

import os
import sys
from pathlib import Path

from .download import download
from .errors import ArtifactsError
from .options import (
    parse_download_config_from_env,
    parse_upload_config_from_env,
    resolve_default_download_build_id,
)
from .promote import promote
from .upload import upload


def run_command():
    args = sys.argv[1:]
    cmd = args.pop(0)
    cmd_project_id = None
    cmd_build_id = None
    variant = None
    git_ref = None

    if cmd == "upload":
        # The hook uses exit code 3 to distinguish a skipped upload from a completed
        # upload and preserve LATEST_SUCCESSFUL. Direct uploads still exit successfully.
        report_empty = args == ["--report-empty"]
        if args and not report_empty:
            raise ArtifactsError(f"unknown upload arguments: {args}")
        upload_config = parse_upload_config_from_env()

        pipeline_name = os.environ.get("BUILDKITE_PIPELINE_SLUG")
        upload_parallel = int(upload_config.get("parallel") or "4")
        upload_concurrency = int(upload_config.get("concurrency") or "32")

        if not upload_config.get("git_ref"):
            raise ArtifactsError(f"missing required git_ref in upload config: {upload_config}")
        if not upload_config.get("variant"):
            raise ArtifactsError(f"missing required variant in upload config: {upload_config}")
        if not upload_config.get("files"):
            upload_config["files"] = ["*.tar.zst"]

        project_id = upload_config.get("project_id") or pipeline_name
        if not project_id:
            raise ArtifactsError(
                "missing required project_id and BUILDKITE_PIPELINE_SLUG is not set"
            )
        upload_config["resolved_project_id"] = project_id

        build_id = upload_config.get("build_id") or os.environ.get("BUILDKITE_BUILD_ID")
        if not build_id:
            raise ArtifactsError("BUILDKITE_BUILD_ID is not set and no build_id override was given")
        upload_config["resolved_build_id"] = build_id

        uploaded = upload(
            upload_config["resolved_project_id"],
            upload_config["resolved_build_id"],
            upload_config["git_ref"],
            upload_config["variant"],
            upload_config["files"],
            upload_parallel,
            upload_concurrency,
            upload_config["base_dir"],
            upload_config["annotate"],
            upload_config["exclude"],
            allow_empty=upload_config["allow_empty"],
        )
        if report_empty and not uploaded:
            sys.exit(3)

    elif cmd == "promote":
        # this is a separate variant because sometimes we want to
        i = 0
        while i < len(args):
            if args[i] == "--variant":
                variant = args[i + 1]
                i += 2
            elif args[i] == "--project-id":
                cmd_project_id = args[i + 1]
                i += 2
            elif args[i] == "--build-id":
                cmd_build_id = args[i + 1]
                i += 2
            elif args[i] == "--git-ref":
                git_ref = args[i + 1]
                i += 2
            else:
                raise ArtifactsError(f"unknown argument: {args[i]}")

        if not variant:
            raise ArtifactsError("missing required --variant argument")
        if not git_ref:
            raise ArtifactsError("missing required --git-ref argument")

        project_id = cmd_project_id or os.environ.get("BUILDKITE_PIPELINE_SLUG")
        if not project_id:
            raise ArtifactsError(
                "missing required --project-id argument and BUILDKITE_PIPELINE_SLUG is not set"
            )

        build_id = cmd_build_id or os.environ.get("BUILDKITE_BUILD_ID")
        if not build_id:
            raise ArtifactsError(
                "BUILDKITE_BUILD_ID is not set and no --build-id override was given"
            )

        promote(project_id, build_id, git_ref, variant)

    elif cmd == "download":
        download_config = parse_download_config_from_env()
        projects_config = download_config["projects"]
        if not projects_config:
            raise ArtifactsError("no download projects configured")

        pipeline_name = os.environ.get("BUILDKITE_PIPELINE_SLUG")
        dirs_to_clean = set()

        for p in projects_config:
            if not p.get("git_ref"):
                raise ArtifactsError(f"missing required git_ref in download project config: {p}")
            if not p.get("variant"):
                raise ArtifactsError(f"missing required variant in download project config: {p}")
            if not p.get("files"):
                p["files"] = ["*.tar.zst"]

            project_id = p.get("project_id") or pipeline_name
            if not project_id:
                raise ArtifactsError(
                    "missing required project_id and BUILDKITE_PIPELINE_SLUG is not set"
                )
            p["resolved_project_id"] = project_id

            p["resolved_build_id"] = resolve_default_download_build_id(p, pipeline_name)

            out_dir = Path(p["output_dir"]).resolve()
            if download_config["clean"]:
                dirs_to_clean.add(out_dir)

        download(
            projects_config,
            dirs_to_clean,
            download_config["parallel"],
            download_config["concurrency"],
        )

    else:
        raise ArtifactsError(f"unknown command: {cmd}")


def main():
    try:
        run_command()
    except ArtifactsError as error:
        print(f"[artifacts] ERROR: {error}", file=sys.stderr)
        return 1
    return 0
