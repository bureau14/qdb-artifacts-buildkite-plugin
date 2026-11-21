"""The plugin's options as a job sees them: BUILDKITE_PLUGIN_QDB_ARTIFACTS_*.

The agent flattens the step's plugin configuration into variables: a list as
KEY_0, KEY_1, ..., a map as KEY_<NAME>, a boolean as "true" or "false". This
module reads them back, checks them, and fills in what the job implies: its
project (the pipeline's), its branch, its build and its key. What it returns
are the arguments of upload(), promote() and download(), and the gate step a
step is.

The key of the job's build is the option ``key``, which the plan step writes
at the top of every configuration of the plugin. A build without a plan step
has none, and each job computes the key of its own build, with the build's id
in it (``own_key``); its first upload creates that key's ``key.json``.
"""

from __future__ import annotations

import functools
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .download import Entry
from .errors import ArtifactsError
from .forms import branch_ref, tag_ref
from .key import KeyJson, key_of
from .layout import Folder, is_step_id, not_a_step_id
from .manifest import Build, Uploaded

PREFIX = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_"
PLUGIN = "bureau14/qdb-artifacts"


def read_bool(val, default=False):
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


def _get_env_map(prefix, key):
    """Read a Buildkite plugin map env value of lower-case names: the agent hands
    ``key: {name: value}`` as KEY_<NAME>, its name upper-cased."""
    head = f"{prefix}{key}_"
    return {
        name[len(head) :].lower(): value
        for name, value in os.environ.items()
        if name.startswith(head) and value
    }


def parse_upload_config_from_env():
    """Parse the Buildkite plugin's upload schema from environment variables."""
    prefix = f"{PREFIX}UPLOAD_"
    return {
        "variant": os.environ.get(f"{prefix}VARIANT"),
        "base_dir": os.environ.get(f"{prefix}BASE_DIR"),
        "parallel": os.environ.get(f"{prefix}PARALLEL"),
        "concurrency": os.environ.get(f"{prefix}CONCURRENCY"),
        "annotate": read_bool(os.environ.get(f"{prefix}ANNOTATE"), default=True),
        "allow_empty": read_bool(os.environ.get(f"{prefix}ALLOW_EMPTY")),
        "files": _get_env_array(prefix, "FILES"),
        "exclude": _get_env_array(prefix, "EXCLUDE"),
    }


def parse_download_projects_from_env():
    """Parse the Buildkite plugin's download.projects[] schema from environment variables."""
    projects = []
    i = 0
    while True:
        prefix = f"{PREFIX}DOWNLOAD_PROJECTS_{i}_"
        variant = os.environ.get(f"{prefix}VARIANT")
        if not variant:
            break

        p = {
            "variant": variant,
            "git_ref": os.environ.get(f"{prefix}GIT_REF"),
            "require_strict_ref_match": read_bool(
                os.environ.get(f"{prefix}REQUIRE_STRICT_REF_MATCH")
            ),
            "project_id": os.environ.get(f"{prefix}PROJECT_ID"),
            "step": _get_env_array(prefix, "STEP"),
            "keys": _get_env_map(prefix, "KEYS"),
            "output_dir": os.environ.get(f"{prefix}OUTPUT_DIR", "."),
            "extract": read_bool(os.environ.get(f"{prefix}EXTRACT")),
            "files": _get_env_array(prefix, "FILES"),
            "exclude": _get_env_array(prefix, "EXCLUDE"),
        }

        projects.append(p)
        i += 1

    return projects


def parse_download_config_from_env():
    """Parse the Buildkite plugin's download schema from environment variables."""
    prefix = f"{PREFIX}DOWNLOAD_"
    return {
        "clean": read_bool(os.environ.get(f"{prefix}CLEAN")),
        "parallel": int(os.environ.get(f"{prefix}PARALLEL", "4")),
        "concurrency": int(os.environ.get(f"{prefix}CONCURRENCY", "32")),
        "projects": parse_download_projects_from_env(),
    }


# ---------------------------------------------------------------------------
# The job: its pipeline, ref, build and key
# ---------------------------------------------------------------------------


def job_pipeline():
    pipeline = os.environ.get("BUILDKITE_PIPELINE_SLUG")
    if not pipeline:
        raise ArtifactsError("BUILDKITE_PIPELINE_SLUG is not set")
    return pipeline


def job_ref():
    """The ref of the job's build, in the form of an entry's git_ref, if known."""
    tag = os.environ.get("BUILDKITE_TAG")
    if tag:
        return tag if tag.startswith("refs/") else tag_ref(tag)
    branch = os.environ.get("BUILDKITE_BRANCH")
    if branch:
        return branch if branch.startswith("refs/") else branch_ref(branch)
    return None


def checkout_commit(cwd="."):
    """`git rev-parse HEAD` of a checkout: by default the job's, the directory the hook
    runs in."""
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=cwd, text=True).strip()


def job_build():
    """The build the job runs in, as the agent names it: its id, number and URL."""
    return Build(
        id=os.environ["BUILDKITE_BUILD_ID"],
        number=int(os.environ["BUILDKITE_BUILD_NUMBER"]),
        url=os.environ["BUILDKITE_BUILD_URL"],
    )


def job_branch():
    """The branch the job's build runs under, as Buildkite names it."""
    return os.environ.get("BUILDKITE_BRANCH", "")


# Set by the trigger step that starts a build for a dependency. A variable of the build, not an
# option of the plugin, so that a pipeline step which acts on its branch's name reads the same
# answer as the promotes and the plan step.
BRANCH_BUILD = "SNAPSHOT_BRANCH_BUILD"


def is_branch_build(value):
    """Whether a build whose ``SNAPSHOT_BRANCH_BUILD`` is ``value`` (None when unset) speaks
    for its branch, and so writes its branch's latest files. Unset, as in a build a person or
    a push starts, it does."""
    return read_bool(value, default=True)


@dataclass(frozen=True)
class OwnKey:
    """The key of the job's build; ``stands_for`` is the key.json to create when the job
    computed it, None when the plan step wrote it."""

    key: str
    stands_for: KeyJson | None


def own_key():
    """The plan step's ``key``; without one, the key of this build's own: its project, the
    commit of its checkout, no dependency, and its build id."""
    planned = os.environ.get(f"{PREFIX}KEY")
    if planned:
        return OwnKey(planned, None)
    k = KeyJson(job_pipeline(), checkout_commit(), (), os.environ["BUILDKITE_BUILD_ID"])
    return OwnKey(key_of(k), k)


# ---------------------------------------------------------------------------
# The step id of a step that uploads or promotes
# ---------------------------------------------------------------------------


def upload_requested():
    """Whether the step names files to upload: an upload block without files
    uploads nothing."""
    return bool(_get_env_array(f"{PREFIX}UPLOAD_", "FILES"))


def promote_requested():
    """Whether the step names a platform to promote."""
    return bool(os.environ.get(f"{PREFIX}PROMOTE_VARIANT"))


def download_requested():
    """Whether the step names a download entry; the agent reads entries up to the first
    without a variant."""
    return bool(os.environ.get(f"{PREFIX}DOWNLOAD_PROJECTS_0_VARIANT"))


def checked_step_id(value, platform, act):
    """``value`` as the step id of a step that ``act`` (uploads or promotes) ``platform``. A
    missing or malformed one fails the job."""
    if not value:
        raise ArtifactsError(
            f"This step {act} {platform}, and its configuration of {PLUGIN} names no step id. "
            "Its template writes step_id in place of key, with the .buildkite/tools of that "
            "change (merge master); a pipeline without pipeline.py writes step: <id> beside "
            "upload."
        )
    if not is_step_id(value):
        raise ArtifactsError(not_a_step_id(value))
    return value


def upload_step_id(platform):
    """The step id a step uploads ``platform`` as: the top-level option ``step``."""
    return checked_step_id(os.environ.get(f"{PREFIX}STEP"), platform, "uploads")


def promote_step_id(platform):
    """The step id a step promotes of ``platform``: the ``step`` of its promote block, which
    names what another step of the pipeline uploaded, else the top-level option ``step``."""
    value = os.environ.get(f"{PREFIX}PROMOTE_STEP") or os.environ.get(f"{PREFIX}STEP")
    return checked_step_id(value, platform, "promotes")


def check_step_id():
    """The step ids of a step that uploads or promotes, checked before its command."""
    if upload_requested():
        upload_step_id(os.environ.get(f"{PREFIX}UPLOAD_VARIANT"))
    if promote_requested():
        promote_step_id(os.environ.get(f"{PREFIX}PROMOTE_VARIANT"))


# ---------------------------------------------------------------------------
# The arguments of upload, promote and download
# ---------------------------------------------------------------------------


def upload_arguments():
    """The upload block, checked and completed with the job's project, build and key.

    The key is a function, which upload calls once it has files: an upload
    that allow_empty skips asks git nothing.
    """
    upload_config = parse_upload_config_from_env()
    variant = upload_config["variant"]
    if not variant:
        raise ArtifactsError(f"missing required variant in upload config: {upload_config}")
    return {
        "project": job_pipeline(),
        "key_of_build": own_key,
        "folder": Folder(variant, upload_step_id(variant)),
        "uploaded": Uploaded(job_branch(), job_build()),
        "patterns": upload_config["files"],
        "parallel": int(upload_config.get("parallel") or "4"),
        "concurrency": int(upload_config.get("concurrency") or "32"),
        "base_dir": upload_config["base_dir"],
        "annotate": upload_config["annotate"],
        "exclude_patterns": upload_config["exclude"],
        "allow_empty": upload_config["allow_empty"],
    }


def promote_arguments():
    """The promote block, completed with the job's project, branch, build and key, and whether
    the build speaks for its branch."""
    variant = os.environ.get(f"{PREFIX}PROMOTE_VARIANT")
    return {
        "project": job_pipeline(),
        "key": own_key().key,
        "folder": Folder(variant, promote_step_id(variant)),
        "branch": job_branch(),
        "build": job_build(),
        "branch_build": is_branch_build(os.environ.get(BRANCH_BUILD)),
    }


def command_exit_status():
    """The exit status of the step's command as the agent reports it; 0 when unset."""
    return int(os.environ.get("BUILDKITE_COMMAND_EXIT_STATUS") or "0")


def is_own_build(p, pipeline, ref):
    """Whether a download entry takes from the job's own build: of the pipeline's project,
    and at no git_ref, the build's own, or any when the build's ref is unknown."""
    project = p["project_id"] or pipeline
    return project == pipeline and (not p["git_ref"] or ref is None or p["git_ref"] == ref)


def no_git_ref(entry, project, template):
    """Why a download ``entry`` of another ``project`` that names no git_ref is refused, by a job
    and by the plan step alike: no build of it can be found. ``template`` says where the entry
    is written."""
    return (
        f"{entry} downloads from {project} and names no git_ref, so no build of {project} can "
        f'be found. {template} writes git_ref in it: "{{git_ref}}" for the build\'s own ref, '
        "or the ref to take."
    )


def download_entry(n, p, pipeline, ref, own):
    """The ``n``-th download entry, checked; ``own()`` gives the build's key."""
    project = p["project_id"] or pipeline
    own_build = is_own_build(p, pipeline, ref)
    if own_build and not p["step"]:
        raise ArtifactsError(
            f"Download entry {n} ({project}, {p['variant']}) names no step and downloads from "
            "this build. Write in it the steps it takes, as step: build or step: [api, rest]; "
            "on a branch that has taken the step-id change (merge master), the runner writes "
            f"them when another step uploads {p['variant']}."
        )
    if not own_build and not p["keys"] and not p["git_ref"]:
        entry = f"Download entry {n} ({project}, {p['variant']})"
        raise ArtifactsError(no_git_ref(entry, project, "Its template"))
    return Entry(
        project=project,
        variant=p["variant"],
        files=tuple(p["files"] or ["*.tar.zst"]),
        exclude=tuple(p["exclude"]),
        output_dir=p["output_dir"],
        extract=p["extract"],
        keys=p["keys"],
        steps=tuple(p["step"]),
        git_ref=p["git_ref"],
        strict=p["require_strict_ref_match"],
        own_key=own().key if own_build else None,
    )


def download_arguments():
    """The download block, checked and completed with the job's defaults."""
    download_config = parse_download_config_from_env()
    pipeline, ref = job_pipeline(), job_ref()
    own = functools.cache(own_key)
    entries = [
        download_entry(n, p, pipeline, ref, own)
        for n, p in enumerate(download_config["projects"], start=1)
    ]
    dirs_to_clean = {Path(e.output_dir).resolve() for e in entries if download_config["clean"]}
    return {
        "entries": entries,
        "dirs_to_clean": dirs_to_clean,
        "parallel": download_config["parallel"],
        "concurrency": download_config["concurrency"],
    }


# ---------------------------------------------------------------------------
# Gate steps
# ---------------------------------------------------------------------------


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
    resume; a person writes none, and each carries one of them.
    """
    for kind in GATE_OPTIONS:
        value = os.environ.get(PREFIX + kind.upper())
        if value:
            return GateStep(kind, None if kind == "leave" else value)
    return None
