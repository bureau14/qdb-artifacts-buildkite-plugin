"""The plugin's options as a job sees them: BUILDKITE_PLUGIN_QDB_ARTIFACTS_*.

The agent flattens the step's plugin configuration into variables: a list as
KEY_0, KEY_1, ..., a map as KEY_<NAME>, a boolean as "true" or "false". This
module reads them back, checks them, and fills in what the job implies when an
option is left out: its pipeline, its build and its ref. What it returns are the
arguments of upload(), promote() and download(), and the gate step a step is.

A step with a snapshot key also records facts of its job: the build, read from
the agent's variables, and the commit of its checkout, which only git can tell
when the build was started for a branch rather than a commit.
"""

from __future__ import annotations

import functools
import os
import re
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import forms, pointer
from .errors import ArtifactsError
from .manifest import Download, Provenance, Source


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


def snapshot_key(variable, option):
    """The snapshot key of an option that only the plan step writes, or None when
    the step has none."""
    value = os.environ.get(f"BUILDKITE_PLUGIN_QDB_ARTIFACTS_{variable}")
    if not value:
        return None
    if not forms.is_key(value):
        raise ArtifactsError(
            f"{option} {value!r} is not a snapshot key ('v1-' and 32 hex digits); "
            "the plan step writes this option, never a person"
        )
    return value


def checkout_commit():
    """`git rev-parse HEAD` of the job's checkout, the directory the hook runs in."""
    try:
        done = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as error:
        raise ArtifactsError(f"git rev-parse HEAD cannot run in {Path.cwd()}: {error}") from None
    commit = done.stdout.strip()
    if done.returncode != 0 or not forms.is_object_id(commit):
        told = done.stderr.strip() or commit
        raise ArtifactsError(f"git rev-parse HEAD in {Path.cwd()} names no commit: {told!r}")
    return commit


def job_variable(name, option):
    """A variable the agent sets for every job, which a step with option needs."""
    value = os.environ.get(name)
    if not value:
        raise ArtifactsError(f"{name} is not set, and a step with {option} needs it")
    return value


def job_build_number(option):
    value = job_variable("BUILDKITE_BUILD_NUMBER", option)
    if not re.fullmatch(r"[0-9]+", value):
        raise ArtifactsError(f"BUILDKITE_BUILD_NUMBER {value!r} is not a build number")
    return int(value)


def job_build(option):
    """The build the job runs in, as the agent names it: its id, number and URL. Whoever
    writes it into an object checks the form of each."""
    return pointer.Build(
        id=job_variable("BUILDKITE_BUILD_ID", option),
        number=job_build_number(option),
        url=job_variable("BUILDKITE_BUILD_URL", option),
    )


def refuse_another_build(build_id, option, key_option):
    """A step with a snapshot key records the build it runs in, by the id, number
    and URL the agent gives, so a build_id option may name only that build."""
    own = job_variable("BUILDKITE_BUILD_ID", key_option)
    if build_id != own:
        raise ArtifactsError(
            f"{option} {build_id!r} is not this job's build {own!r}, "
            f"and a step with {key_option} records the build it runs in"
        )


def snapshot_pointer(project_id, key, variant, git_ref):
    """The pointer promote creates in a build with a snapshot key, checked before
    anything is written. It names the job's own build: refuse_another_build holds
    the build_id option to it."""
    return pointer.checked(
        pointer.Pointer(
            project=project_id,
            key=key,
            variant=variant,
            git_ref=git_ref,
            commit=checkout_commit(),
            build=job_build("promote.snapshot"),
            written_at=forms.iso_utc(time.time()),
        )
    )


def own_downloads():
    """The builds this job's own download options name by their build_id. An option
    without one, or with LATEST_SUCCESSFUL, names no build: the job resolves it
    when it runs."""
    pipeline_name = os.environ.get("BUILDKITE_PIPELINE_SLUG")
    return tuple(
        Download(
            project_id=p["project_id"] or pipeline_name,
            variant=p["variant"],
            git_ref=p["git_ref"],
            build_id=p["build_id"],
        )
        for p in parse_download_projects_from_env()
        if p["build_id"] and p["build_id"] != "LATEST_SUCCESSFUL"
    )


def without_user(repository):
    """The repository as a manifest may name it. Every reader of the bucket reads the
    manifest, and the user part of a URL may hold a token or a password, so a URL
    loses it. The scp form (git@host:org/repo) has no place for a password."""
    url = urllib.parse.urlsplit(repository)
    if not url.scheme or "@" not in url.netloc:
        return repository
    return urllib.parse.urlunsplit(url._replace(netloc=url.netloc.rpartition("@")[2]))


def upload_provenance(key, build_id):
    """Where the files of an upload in a build with a snapshot key were made."""
    option = "upload.snapshot"
    return Provenance(
        source=Source(
            repository=without_user(job_variable("BUILDKITE_REPO", option)),
            commit=checkout_commit(),
            pipeline=job_variable("BUILDKITE_PIPELINE_SLUG", option),
            build_number=job_build_number(option),
            build_id=build_id,
            job_id=job_variable("BUILDKITE_JOB_ID", option),
            snapshot=key,
        ),
        downloads=own_downloads(),
    )


def upload_requested():
    """Whether the step names files to upload: an upload block without files
    uploads nothing."""
    return bool(_get_env_array("BUILDKITE_PLUGIN_QDB_ARTIFACTS_UPLOAD_", "FILES"))


def upload_arguments():
    """The upload block, checked and completed with the job's pipeline and build.

    The provenance of a step with a key is a function, which upload calls once it
    has files: an upload that is allowed to be empty asks git nothing.
    """
    upload_config = parse_upload_config_from_env()

    pipeline_name = os.environ.get("BUILDKITE_PIPELINE_SLUG")
    upload_parallel = int(upload_config.get("parallel") or "4")
    upload_concurrency = int(upload_config.get("concurrency") or "32")

    if not upload_config.get("git_ref"):
        raise ArtifactsError(f"missing required git_ref in upload config: {upload_config}")
    if not upload_config.get("variant"):
        raise ArtifactsError(f"missing required variant in upload config: {upload_config}")

    project_id = upload_config.get("project_id") or pipeline_name
    if not project_id:
        raise ArtifactsError("missing required project_id and BUILDKITE_PIPELINE_SLUG is not set")

    build_id = upload_config.get("build_id") or os.environ.get("BUILDKITE_BUILD_ID")
    if not build_id:
        raise ArtifactsError("BUILDKITE_BUILD_ID is not set and no build_id override was given")

    key = snapshot_key("UPLOAD_SNAPSHOT", "upload.snapshot")
    if key is not None:
        refuse_another_build(build_id, "upload.build_id", "upload.snapshot")

    return {
        "project_id": project_id,
        "build_id": build_id,
        "git_ref": upload_config["git_ref"],
        "variant": upload_config["variant"],
        "patterns": upload_config["files"],
        "parallel": upload_parallel,
        "concurrency": upload_concurrency,
        "base_dir": upload_config["base_dir"],
        "annotate": upload_config["annotate"],
        "exclude_patterns": upload_config["exclude"],
        "allow_empty": upload_config["allow_empty"],
        "read_provenance": (
            None if key is None else functools.partial(upload_provenance, key, build_id)
        ),
    }


def promote_requested():
    """Whether the step names a variant to promote."""
    return bool(os.environ.get("BUILDKITE_PLUGIN_QDB_ARTIFACTS_PROMOTE_VARIANT"))


def promote_arguments():
    """The promote block, checked and completed with the job's pipeline and build."""
    prefix = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_PROMOTE_"
    git_ref = os.environ.get(f"{prefix}GIT_REF")
    if not git_ref:
        raise ArtifactsError("missing required --git-ref argument")

    project_id = os.environ.get(f"{prefix}PROJECT_ID") or os.environ.get("BUILDKITE_PIPELINE_SLUG")
    if not project_id:
        raise ArtifactsError(
            "missing required --project-id argument and BUILDKITE_PIPELINE_SLUG is not set"
        )

    build_id = os.environ.get(f"{prefix}BUILD_ID") or os.environ.get("BUILDKITE_BUILD_ID")
    if not build_id:
        raise ArtifactsError("BUILDKITE_BUILD_ID is not set and no --build-id override was given")

    variant = os.environ.get(f"{prefix}VARIANT")
    key = snapshot_key("PROMOTE_SNAPSHOT", "promote.snapshot")
    if key is not None:
        refuse_another_build(build_id, "promote.build_id", "promote.snapshot")

    return {
        "project_id": project_id,
        "build_id": build_id,
        "git_ref": git_ref,
        "variant": variant,
        "snapshot_pointer": (
            None if key is None else snapshot_pointer(project_id, key, variant, git_ref)
        ),
    }


def command_exit_status():
    """The exit status of the step's command as the agent reports it; 0 when unset."""
    return int(os.environ.get("BUILDKITE_COMMAND_EXIT_STATUS") or "0")


@dataclass(frozen=True)
class GateStep:
    """A gate step of a build with snapshot dependencies: its kind and the pipeline it plans.

    ``kind`` is ``plan``, ``resume`` or ``leave``; ``pipeline`` the path of the
    generated pipeline that ``plan`` and ``resume`` name, None for ``leave``.
    """

    kind: str
    pipeline: str | None


GATE_OPTIONS = ("plan", "resume", "leave")


def gate_step():
    """The gate option of the step, or None for a step that is no gate step.

    The runner prints the steps of plan and leave, the plan step the step of
    resume; a person writes none. A step carries one of them: two are an
    error, and so is a leave that is not true.
    """
    prefix = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_"
    given = {k: os.environ.get(prefix + k.upper()) for k in GATE_OPTIONS}
    carried = {k: v for k, v in given.items() if v}
    if not carried:
        return None
    if len(carried) > 1:
        raise ArtifactsError(f"a step carries one gate option, this one {' and '.join(carried)}")
    ((kind, value),) = carried.items()
    if kind == "leave":
        if value != "true":
            raise ArtifactsError(f"leave is true on the step that leaves the gate, not {value!r}")
        return GateStep(kind, None)
    return GateStep(kind, value)


def download_arguments():
    """The download block, checked and completed with the job's defaults."""
    download_config = parse_download_config_from_env()
    projects_config = download_config["projects"]

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

    return {
        "projects_config": projects_config,
        "dirs_to_clean": dirs_to_clean,
        "parallel": download_config["parallel"],
        "concurrency": download_config["concurrency"],
    }
