"""Which Python the hooks use: post-checkout builds .venv, the other hooks run it."""

import os
import platform
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="the PATH below is a POSIX layout")


def path_with_only(tmp_path, *tools):
    """A PATH that offers exactly the named tools and no Python."""
    bin_dir = tmp_path / "only-bin"
    bin_dir.mkdir()
    for tool in tools:
        (bin_dir / tool).symlink_to(shutil.which(tool))
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


def test_post_checkout_without_any_python_fails_before_touching_the_venv(
    job, hook, plugin_venv, tmp_path
):
    venv_config = plugin_venv.parent.parent / "pyvenv.cfg"
    before = venv_config.stat().st_mtime_ns
    job = job.with_env(PATH=path_with_only(tmp_path, "dirname"), QDB_CICD_AGENT_PYTHON3=None)

    outcome = hook("post-checkout", job)

    assert outcome.code == 1
    assert "requires python3" in outcome.stdout
    assert venv_config.stat().st_mtime_ns == before


def test_the_other_hooks_need_only_the_venv(job, store, hook, tmp_path):
    store.put_build("engine", "refs/heads/master", "linux", "b-1", {"a.bin": b"a"})
    job.write("out.bin", b"o")
    job = job.with_env(
        PATH=path_with_only(tmp_path, "dirname"), QDB_CICD_AGENT_PYTHON3=None
    ).with_plugin(
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
