"""Which Python the hooks use: post-checkout builds .venv, the other hooks run it."""

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
