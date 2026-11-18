"""The plugin's options as a job sees them: BUILDKITE_PLUGIN_QDB_ARTIFACTS_*.

The agent flattens the step's plugin configuration into variables: a list as
KEY_0, KEY_1, ..., a map as KEY_<NAME>, a boolean as "true" or "false". This
module reads them back, and fills in what the job implies when an option is
left out: its pipeline, its build and its ref.
"""

import os

from .errors import ArtifactsError


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
        raise ArtifactsError("BUILDKITE_BUILD_ID is not set and no build_id override was given")
    return build_id
