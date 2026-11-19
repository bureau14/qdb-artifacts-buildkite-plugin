"""The world of the gate steps: the plugin as an agent checks it out for them, and the
stand-ins of ``standin.py`` around the suite's store.

Gate steps run on the Linux agents of the default queue only, so these tests
run where bash and git are POSIX ones.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from standin import AUTHOR, GENERATED, Agent, Buildkite, BuildkiteApi, Hosting

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
POST_CHECKOUT_TIMEOUT = 900

collect_ignore_glob = ["test_*.py"] if os.name == "nt" else []


def commit_as_checked_out(root: Path) -> None:
    """Make ``root`` a git checkout of one commit, as the agent checks out a plugin."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **AUTHOR}
    for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "-m", "plugin"]):
        subprocess.run(["git", *args], cwd=root, env=env, check=True, capture_output=True)


def build_gate_plugin(root: Path, home: Path) -> None:
    """Hooks and library of the worktree in a checkout of their own, its .venv-snapshot
    made by the real post-checkout of a step that carries ``plan``."""
    for part in ("hooks", "lib"):
        shutil.copytree(
            PLUGIN_ROOT / part, root / part, ignore=shutil.ignore_patterns("__pycache__")
        )
    shutil.copy2(PLUGIN_ROOT / "artifacts.py", root / "artifacts.py")
    commit_as_checked_out(root)
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(home),
        "QDB_CICD_AGENT_PYTHON3": sys.executable,
        "PYTHONDONTWRITEBYTECODE": "1",
        "BUILDKITE_PLUGIN_QDB_ARTIFACTS_PLAN": GENERATED,
    }
    done = subprocess.run(
        [shutil.which("bash"), str(root / "hooks" / "post-checkout")],
        cwd=home,
        env=env,
        capture_output=True,
        text=True,
        timeout=POST_CHECKOUT_TIMEOUT,
        check=False,
    )
    if done.returncode != 0:
        pytest.fail(f"post-checkout of a plan step failed\n{done.stdout}\n{done.stderr}")


@pytest.fixture(scope="session")
def gate_plugin(tmp_path_factory, request):
    """The plugin of the gate steps, made once per session, also when xdist's workers share it."""
    import fcntl

    shared = tmp_path_factory.getbasetemp()
    if hasattr(request.config, "workerinput"):
        shared = shared.parent
    root = shared / "gate-plugin"
    with open(shared / "gate-plugin.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not (shared / "gate-plugin.built").exists():
            home = shared / "gate-plugin-home"
            home.mkdir()
            build_gate_plugin(root, home)
            (shared / "gate-plugin.built").write_text("")
    return root


@pytest.fixture
def buildkite(tmp_path, job, hook, gate_plugin, plugin_venv):
    """Buildkite, GitHub and their agents around the suite's store, for one test."""
    api = BuildkiteApi(tmp_path / "api")
    world = Buildkite(
        root=tmp_path / "buildkite",
        template=job,
        hook=hook,
        gate_plugin=gate_plugin,
        plugin_root=PLUGIN_ROOT,
        hosting=Hosting(tmp_path / "hosting"),
        api=api,
        agent=Agent(tmp_path / "builds-agent"),
    )
    yield world
    api.close()
