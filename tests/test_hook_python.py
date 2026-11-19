"""Which Python the hooks use: post-checkout builds the step's venv, the other hooks run it.

A step that carries plan or resume has .venv-snapshot, any other .venv.
"""

import os
import platform
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the PATH below is a POSIX layout")


def is_python(name):
    return name.startswith("python")


def path_without_python(tmp_path):
    """A PATH that offers every tool of the host's PATH except Python."""
    bin_dir = tmp_path / "no-python-bin"
    bin_dir.mkdir()
    for directory in map(Path, os.environ.get("PATH", "").split(os.pathsep)):
        if not directory.is_dir():
            continue
        for tool in directory.iterdir():
            # os.path.isfile, unlike Path.is_file, takes an unreadable entry for no file.
            usable = os.path.isfile(tool) and os.access(tool, os.X_OK)
            if usable and not is_python(tool.name) and not (bin_dir / tool.name).exists():
                (bin_dir / tool.name).symlink_to(tool)
    return str(bin_dir)


def test_post_checkout_built_the_venv_with_the_agent_python_and_its_dependencies(plugin_venv):
    version = subprocess.run(
        [
            str(plugin_venv),
            "-c",
            "import boto3, zstandard, platform; print(platform.python_version())",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    assert version.stdout.strip() == platform.python_version()


def test_post_checkout_replaces_an_existing_venv_with_one_made_by_the_agent_python(
    job, hook, plugin_copy, fake_python
):
    venv = plugin_copy / ".venv"
    stale = venv / "left-by-an-earlier-job"
    stale.parent.mkdir()
    stale.write_bytes(b"stale")
    job = job.with_env(
        QDB_CICD_AGENT_PYTHON3=str(fake_python.path), FAKE_PYTHON_CALLS=str(fake_python.calls_dir)
    )

    outcome = hook("post-checkout", job, root=plugin_copy)

    assert outcome.code == 0, outcome.output
    assert not stale.exists()
    make_venv, install = [call.argv for call in fake_python.calls()]
    assert (make_venv[0], make_venv[1:3], make_venv[-1]) == (
        str(fake_python.path),
        ["-m", "venv"],
        str(venv),
    )
    assert (install[0], install[1:4], install[-2:]) == (
        str(venv / "bin" / "python"),
        ["-m", "pip", "install"],
        ["-r", str(plugin_copy / "lib" / "requirements.txt")],
    )


def pip_install_of_post_checkout(job, hook, plugin_copy, fake_python, options):
    """The arguments post-checkout gave pip, for a step with the plugin options ``options``."""
    job = job.with_env(
        QDB_CICD_AGENT_PYTHON3=str(fake_python.path), FAKE_PYTHON_CALLS=str(fake_python.calls_dir)
    ).with_options(**options)
    outcome = hook("post-checkout", job, root=plugin_copy)
    assert outcome.code == 0, outcome.output
    _, install = [call.argv for call in fake_python.calls()]
    return install


@pytest.mark.parametrize(
    "options",
    [{"PLAN": ".buildkite/snapshot-pipeline.yml"}, {"RESUME": ".buildkite/snapshot-pipeline.yml"}],
    ids=["plan", "resume"],
)
def test_post_checkout_of_a_plan_or_resume_step_also_installs_what_reading_documents_needs(
    job, hook, plugin_copy, fake_python, options
):
    install = pip_install_of_post_checkout(job, hook, plugin_copy, fake_python, options)

    assert install[-4:] == [
        "-r",
        str(plugin_copy / "lib" / "requirements.txt"),
        "-r",
        str(plugin_copy / "lib" / "snapshot" / "requirements.txt"),
    ]


def test_the_steps_that_read_documents_keep_a_venv_that_no_other_job_replaces(
    job, hook, plugin_copy, fake_python
):
    # Every job of one plugin commit on a host shares the plugin's checkout, and
    # the post-checkout of each makes its venv again.
    job = job.with_env(
        QDB_CICD_AGENT_PYTHON3=str(fake_python.path), FAKE_PYTHON_CALLS=str(fake_python.calls_dir)
    )
    plan = job.with_options(PLAN=".buildkite/snapshot-pipeline.yml")
    upload = job.with_options(UPLOAD_FILES="out/*", UPLOAD_VARIANT="linux")

    assert hook("post-checkout", plan, root=plugin_copy).code == 0
    made_for_plan = Path(fake_python.calls()[0].argv[-1])
    (made_for_plan / "in-use").write_text("")
    assert hook("post-checkout", upload, root=plugin_copy).code == 0
    assert hook("post-command", plan, root=plugin_copy).code == 0

    assert made_for_plan == plugin_copy / ".venv-snapshot"
    assert (made_for_plan / "in-use").exists()
    assert fake_python.calls()[-1].argv[:3] == [
        str(made_for_plan / "bin" / "python"),
        str(plugin_copy / "artifacts.py"),
        "post-command",
    ]


@pytest.mark.parametrize(
    "options",
    [{"LEAVE": "true"}, {"UPLOAD_FILES": "out/*", "UPLOAD_VARIANT": "linux"}],
    ids=["leave", "upload"],
)
def test_post_checkout_of_any_other_step_installs_only_the_library(
    job, hook, plugin_copy, fake_python, options
):
    install = pip_install_of_post_checkout(job, hook, plugin_copy, fake_python, options)

    assert str(plugin_copy / "lib" / "snapshot" / "requirements.txt") not in install
    assert install[-2:] == ["-r", str(plugin_copy / "lib" / "requirements.txt")]


def test_an_ordinary_job_runs_in_a_venv_without_pyyaml(job, store, hook, plugin_venv):
    probe = subprocess.run(
        [str(plugin_venv), "-c", "import yaml"], capture_output=True, text=True, check=False
    )
    store.put_build("engine", "refs/heads/master", "linux", "b-1", {"a.bin": b"a"})
    job.write("out.bin", b"o")
    job = job.with_plugin(
        {
            "download": {
                "projects": [
                    {
                        "project_id": "engine",
                        "build_id": "b-1",
                        "variant": "linux",
                        "git_ref": "refs/heads/master",
                        "files": ["*.bin"],
                        "output-dir": "in",
                    }
                ]
            },
            "upload": {"variant": "linux", "git_ref": "refs/heads/master", "files": "out.bin"},
            "promote": {"variant": "linux", "git_ref": "refs/heads/master"},
        }
    )

    downloaded = hook("pre-command", job)
    uploaded = hook("post-command", job)

    assert "No module named 'yaml'" in probe.stderr
    assert (downloaded.code, uploaded.code) == (0, 0), downloaded.output + uploaded.output
    assert job.files("in") == ["a.bin"]
    latest = "widget-build/refs/heads/master/variants/linux/LATEST_SUCCESSFUL"
    assert store.get(latest) == job.env["BUILDKITE_BUILD_ID"].encode()


def test_the_leave_step_loads_its_modules_in_a_venv_without_pyyaml(plugin_venv):
    probe = (
        "import sys; from lib.snapshot import leave; "
        "print(sorted(m for m in sys.modules if m.split('.')[0] == 'yaml'))"
    )

    loaded = subprocess.run(
        [str(plugin_venv), "-c", probe],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert (loaded.returncode, loaded.stdout.strip()) == (0, "[]"), loaded.stderr


def test_post_checkout_without_any_python_fails_before_touching_the_venv(
    job, hook, plugin_venv, tmp_path
):
    venv_config = plugin_venv.parent.parent / "pyvenv.cfg"
    before = venv_config.stat().st_mtime_ns
    job = job.with_env(PATH=path_without_python(tmp_path), QDB_CICD_AGENT_PYTHON3=None)

    outcome = hook("post-checkout", job)

    assert outcome.code == 1
    assert "python3" in outcome.stdout
    assert venv_config.stat().st_mtime_ns == before


def test_the_other_hooks_need_only_the_venv(job, store, hook, tmp_path):
    store.put_build("engine", "refs/heads/master", "linux", "b-1", {"a.bin": b"a"})
    job.write("out.bin", b"o")
    job = job.with_env(PATH=path_without_python(tmp_path), QDB_CICD_AGENT_PYTHON3=None).with_plugin(
        {
            "download": {
                "projects": [
                    {
                        "project_id": "engine",
                        "build_id": "b-1",
                        "variant": "linux",
                        "git_ref": "refs/heads/master",
                        "files": ["*.bin"],
                        "output-dir": "in",
                    }
                ]
            },
            "upload": {
                "variant": "linux",
                "git_ref": "refs/heads/master",
                "files": "out.bin",
                "annotate": False,
            },
        }
    )

    downloaded = hook("pre-command", job)
    uploaded = hook("post-command", job)

    assert downloaded.code == 0, downloaded.output
    assert uploaded.code == 0, uploaded.output
    assert job.files("in") == ["a.bin"]
