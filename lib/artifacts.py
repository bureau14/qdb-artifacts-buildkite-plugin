#!/usr/bin/env python3
from __future__ import annotations

"""Artifact upload/download for Buildkite CI pipelines (AWS S3 + Cloudflare R2).

Replaces Buildkite's native artifact handling with backend choice (S3/R2), streaming
in-memory extraction, entry-level filtering, and two-level parallel transfers.

Usage
-----
    # upload (build step)
    python3 .buildkite/tools/artifacts.py upload 'artifacts/**/*.tar.zst'

    # download + extract (test step, cross-step, entry-filtered)
    python3 .buildkite/tools/artifacts.py download \\
        --variant build-linux-amd64-release --extract --clean --output-dir artifacts \\
        '*-c-api.tar.zst!lib/*' '*-server.tar.zst!bin/*' '*-tests.tar.zst!bin/*'
"""

import os
import sys
from pathlib import Path

from .download import download
from .errors import die
from .promote import promote
from .upload import upload


def _get_env_bool(val, default=False):
    if val is None:
        return default
    return str(val).strip().lower() in ("true", "1", "on", "yes")


def _get_env_array(prefix, key):
    """Read a Buildkite plugin array env value.

    Buildkite exposes multi-item arrays as KEY_0, KEY_1, ... and single-item
    arrays as KEY without the _0 suffix.
    """
    values = []
    j = 0
    while True:
        val = os.environ.get(f"{prefix}{key}_{j}")
        if not val:
            if j == 0:
                single_val = os.environ.get(f"{prefix}{key}")
                if single_val:
                    values.append(single_val)
            break
        values.append(val)
        j += 1
    return values


def parse_upload_config_from_env():
    """Parse the Buildkite plugin's upload schema from environment variables."""
    prefix = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_"
    return {
        "variant": os.environ.get(f"{prefix}VARIANT"),
        "git_ref": os.environ.get(f"{prefix}GIT_REF"),
        "project_id": os.environ.get(f"{prefix}PROJECT_ID"),
        "build_id": os.environ.get(f"{prefix}BUILD_ID"),
        "base_dir": os.environ.get(f"{prefix}BASE_DIR"),
        "parallel": os.environ.get(f"{prefix}PARALLEL"),
        "concurrency": os.environ.get(f"{prefix}CONCURRENCY"),
        "annotate": _get_env_bool(os.environ.get(f"{prefix}ANNOTATE"), default=True),
        "allow_empty": _get_env_bool(os.environ.get(f"{prefix}ALLOW_EMPTY")),
        "files": _get_env_array(prefix, "FILES"),
        "exclude": _get_env_array(prefix, "EXCLUDE"),
    }


def parse_download_projects_from_env():
    """Parse the Buildkite plugin's download.projects[] schema from environment variables."""
    projects = []
    i = 0
    while True:
        prefix = f"BUILDKITE_PLUGIN_QDB_ARTIFACTS_DOWNLOAD_PROJECTS_{i}_"
        variant = os.environ.get(f"{prefix}VARIANT")
        if not variant:
            break

        p = {
            "variant": variant,
            "git_ref": os.environ.get(f"{prefix}GIT_REF"),
            "require_strict_ref_match": _get_env_bool(
                os.environ.get(f"{prefix}REQUIRE_STRICT_REF_MATCH")
            ),
            "project_id": os.environ.get(f"{prefix}PROJECT_ID"),
            "build_id": os.environ.get(f"{prefix}BUILD_ID"),
            "output_dir": os.environ.get(f"{prefix}OUTPUT_DIR", "."),
            "extract": _get_env_bool(os.environ.get(f"{prefix}EXTRACT")),
            "files": _get_env_array(prefix, "FILES"),
            "exclude": _get_env_array(prefix, "EXCLUDE"),
        }

        projects.append(p)
        i += 1

    return projects


def parse_download_config_from_env():
    """Parse the Buildkite plugin's download schema from environment variables."""
    prefix = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_DOWNLOAD_"
    return {
        "clean": _get_env_bool(os.environ.get(f"{prefix}CLEAN")),
        "parallel": int(os.environ.get(f"{prefix}PARALLEL", "4")),
        "concurrency": int(os.environ.get(f"{prefix}CONCURRENCY", "32")),
        "projects": parse_download_projects_from_env(),
    }


def _current_buildkite_git_ref():
    """Return the current Buildkite git ref in artifact namespace format, if known."""
    tag = os.environ.get("BUILDKITE_TAG")
    if tag:
        return tag if tag.startswith("refs/") else f"refs/tags/{tag}"

    branch = os.environ.get("BUILDKITE_BRANCH")
    if branch:
        return branch if branch.startswith("refs/") else f"refs/heads/{branch}"

    return None


def resolve_default_download_build_id(project_config, pipeline_name):
    """Resolve the implicit download build_id for a project config.

    Same-pipeline downloads default to the current build only when the requested
    git_ref is the current Buildkite ref. If the same pipeline asks for another
    ref, use LATEST_SUCCESSFUL so branch/main fallbacks point at promoted builds
    instead of looking for the current build ID under a different ref.
    """
    build_id = project_config.get("build_id")
    if build_id:
        return build_id

    project_id = project_config.get("project_id")
    is_current_project = not project_id or project_id == pipeline_name
    if not is_current_project:
        return "LATEST_SUCCESSFUL"

    current_git_ref = _current_buildkite_git_ref()
    requested_git_ref = project_config.get("git_ref")
    if current_git_ref and requested_git_ref and requested_git_ref != current_git_ref:
        return "LATEST_SUCCESSFUL"

    build_id = os.environ.get("BUILDKITE_BUILD_ID")
    if not build_id:
        die("BUILDKITE_BUILD_ID is not set and no build_id override was given")
    return build_id


def main():
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
            die(f"unknown upload arguments: {args}")
        upload_config = parse_upload_config_from_env()

        pipeline_name = os.environ.get("BUILDKITE_PIPELINE_SLUG")
        upload_parallel = int(upload_config.get("parallel") or "4")
        upload_concurrency = int(upload_config.get("concurrency") or "32")

        if not upload_config.get("git_ref"):
            die(f"missing required git_ref in upload config: {upload_config}")
        if not upload_config.get("variant"):
            die(f"missing required variant in upload config: {upload_config}")
        if not upload_config.get("files"):
            upload_config["files"] = ["*.tar.zst"]

        project_id = upload_config.get("project_id") or pipeline_name
        if not project_id:
            die("missing required project_id and BUILDKITE_PIPELINE_SLUG is not set")
        upload_config["resolved_project_id"] = project_id

        build_id = upload_config.get("build_id") or os.environ.get("BUILDKITE_BUILD_ID")
        if not build_id:
            die("BUILDKITE_BUILD_ID is not set and no build_id override was given")
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
                die(f"unknown argument: {args[i]}")

        if not variant:
            die("missing required --variant argument")
        if not git_ref:
            die("missing required --git-ref argument")

        project_id = cmd_project_id or os.environ.get("BUILDKITE_PIPELINE_SLUG")
        if not project_id:
            die("missing required --project-id argument and BUILDKITE_PIPELINE_SLUG is not set")

        build_id = cmd_build_id or os.environ.get("BUILDKITE_BUILD_ID")
        if not build_id:
            die("BUILDKITE_BUILD_ID is not set and no --build-id override was given")

        promote(project_id, build_id, git_ref, variant)

    elif cmd == "download":
        download_config = parse_download_config_from_env()
        projects_config = download_config["projects"]
        if not projects_config:
            die("no download projects configured")

        pipeline_name = os.environ.get("BUILDKITE_PIPELINE_SLUG")
        dirs_to_clean = set()

        for p in projects_config:
            if not p.get("git_ref"):
                die(f"missing required git_ref in download project config: {p}")
            if not p.get("variant"):
                die(f"missing required variant in download project config: {p}")
            if not p.get("files"):
                p["files"] = ["*.tar.zst"]

            project_id = p.get("project_id") or pipeline_name
            if not project_id:
                die("missing required project_id and BUILDKITE_PIPELINE_SLUG is not set")
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
        die(f"unknown command: {cmd}")
