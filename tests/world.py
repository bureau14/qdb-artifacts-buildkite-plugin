"""The job's world: what a Buildkite job of the plugin runs in.

- An S3 and SSM server (moto, over real HTTP on 127.0.0.1), emptied for every
  test, and the bucket as an administrator sees it.
- A ``buildkite-agent`` first on ``PATH`` that keeps every build's meta-data,
  step outcomes, uploads and annotations as files, as Buildkite would.
- The plugin as an agent checks it out: a copy of its hooks, library and
  command line, committed, whose ``.venv`` and ``.venv-snapshot`` the real
  ``post-checkout`` makes once per session.
- Jobs: a checkout that is a git repository, and the environment an agent
  gives a job, with the step's plugin options flattened as the agent does.
  Hooks run as the agent runs them: bash, that environment, that checkout.

Every name is invented. Nothing reaches a network but pip in post-checkout.
"""

from __future__ import annotations

import dataclasses
import itertools
import os
import shutil
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path

import boto3
import pytest
from botocore.config import Config
from moto.server import DomainDispatcherApplication, create_backend_app
from werkzeug.serving import WSGIRequestHandler, make_server

PLUGIN_ROOT = Path(__file__).resolve().parents[1]

BUCKET = "example-artifacts"
REGION = "us-east-1"
ORGANISATION = "example"
R2 = {
    "ARTIFACTS_R2_ACCOUNT_ID": "0123456789abcdef0123456789abcdef",
    "ARTIFACTS_R2_ACCESS_KEY_ID": "test-r2-access-key",
    "ARTIFACTS_R2_SECRET_ACCESS_KEY": "test-r2-secret-access-key",
}
OPTION_PREFIX = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_"
GENERATED = ".buildkite/snapshot-pipeline.yml"
# A hook of a test takes about a second; one that retries a store it cannot reach is stopped.
HOOK_TIMEOUT = 60
POST_CHECKOUT_TIMEOUT = 900

# Git as on an agent: no configuration of the host, a fixed author.
GIT_IDENTITY = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_IDENTITY}
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {done.stderr}")
    return done.stdout.strip()


def committed(directory: Path) -> str:
    """Make ``directory`` a repository of one commit of what it holds; that commit."""
    git(directory, "init", "-q")
    git(directory, "add", "-A")
    git(directory, "commit", "-q", "--allow-empty", "-m", "checkout")
    return git(directory, "rev-parse", "HEAD")


def paired(*dimensions):
    """The values of several dimensions zipped, each repeated until the longest ends:
    every value of every dimension once, without the cross product."""
    width = max(len(d) for d in dimensions)
    return [tuple(d[i % len(d)] for d in dimensions) for i in range(width)]


# ---------------------------------------------------------------------------
# The object store
# ---------------------------------------------------------------------------


class QuietHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


@pytest.fixture(scope="session")
def store_endpoint():
    app = DomainDispatcherApplication(create_backend_app)
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def admin_client(service: str, endpoint: str):
    return boto3.client(
        service,
        endpoint_url=endpoint,
        region_name=REGION,
        aws_access_key_id="test-admin",
        aws_secret_access_key="test-admin-secret",
        # moto gives multipart objects a whole-object checksum boto3 would refuse.
        config=Config(response_checksum_validation="when_required"),
    )


@dataclasses.dataclass(frozen=True)
class Store:
    """The bucket, as an administrator sees it."""

    endpoint: str
    s3: object

    def put(self, key: str, body: bytes) -> None:
        self.s3.put_object(Bucket=BUCKET, Key=key, Body=body)

    def get(self, key: str) -> bytes | None:
        try:
            return self.s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            return None

    def stored(self, prefix: str = "") -> set[str]:
        """The keys of every object under ``prefix``."""
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET, Prefix=prefix)
        return {o["Key"] for page in pages for o in page.get("Contents", [])}


@pytest.fixture
def store(store_endpoint):
    reset = urllib.request.Request(f"{store_endpoint}/moto-api/reset", method="POST")
    urllib.request.urlopen(reset).read()
    s3 = admin_client("s3", store_endpoint)
    s3.create_bucket(Bucket=BUCKET)
    return Store(store_endpoint, s3)


# ---------------------------------------------------------------------------
# buildkite-agent
# ---------------------------------------------------------------------------

AGENT_SCRIPT = '''#!{python}
"""buildkite-agent of the tests: every build's meta-data, step outcomes,
uploads and annotations, as files under $FAKE_BUILDKITE_STATE."""
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_BUILDKITE_STATE"])


def option(args, name):
    return args[args.index(name) + 1]


def build_and_rest(args):
    if "--build" in args:
        i = args.index("--build")
        return args[i + 1], args[:i] + args[i + 2:]
    return os.environ["BUILDKITE_BUILD_ID"], args


def numbered(directory):
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{{len(list(directory.iterdir())):04d}}"


def main(args):
    build, args = build_and_rest(args)
    here = STATE / build
    stdin = (lambda: sys.stdin.buffer.read().decode("utf-8"))
    if args[:2] == ["meta-data", "exists"]:
        return 0 if (here / "meta-data" / args[2]).exists() else 100
    if args[:2] == ["meta-data", "get"]:
        found = here / "meta-data" / args[2]
        if not found.exists():
            return 1
        sys.stdout.buffer.write(found.read_bytes() + b"\\n")
        return 0
    if args[:2] == ["meta-data", "set"]:
        (here / "meta-data").mkdir(parents=True, exist_ok=True)
        (here / "meta-data" / args[2]).write_text(stdin(), encoding="utf-8")
        return 0
    if args[:3] == ["step", "get", "outcome"]:
        found = here / "outcomes" / option(args, "--step")
        sys.stdout.write(found.read_text() if found.exists() else "")
        return 0
    if args[:2] == ["pipeline", "upload"]:
        numbered(here / "uploads").write_text(stdin(), encoding="utf-8")
        return 0
    if args[:1] == ["annotate"]:
        body = args[1] if not args[1].startswith("--") else stdin()
        numbered(here / "annotations" / option(args, "--context")).write_text(body)
        return 0
    return 1


sys.exit(main(sys.argv[1:]))
'''


@dataclasses.dataclass(frozen=True)
class Agent:
    """What the agent keeps of every build, read and written by a test as Buildkite would."""

    state: Path
    bin: Path

    def files(self, build: str, kind: str) -> list[str]:
        """The texts the build keeps of ``kind`` (``uploads``, ``annotations``, ...), in order."""
        found = self.state / build / kind
        return [p.read_text() for p in sorted(found.rglob("*")) if p.is_file()]

    def set(self, build: str, kind: str, name: str, value: str) -> None:
        found = self.state / build / kind
        found.mkdir(parents=True, exist_ok=True)
        (found / name).write_text(value)


@pytest.fixture
def agent(tmp_path):
    bin_dir = tmp_path / "agent-bin"
    bin_dir.mkdir()
    script = bin_dir / "buildkite-agent"
    script.write_text(AGENT_SCRIPT.format(python=sys.executable))
    script.chmod(0o755)
    return Agent(tmp_path / "agent-state", bin_dir)


# ---------------------------------------------------------------------------
# Running hooks
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    @property
    def output(self) -> str:
        return f"exit {self.code}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"


def start_hook(root: Path, name: str, cwd: Path, env: dict) -> subprocess.Popen:
    return subprocess.Popen(
        [shutil.which("bash"), str(root / "hooks" / name)],
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def finish_hook(process: subprocess.Popen, timeout: float) -> Outcome:
    """The hook's outcome; a hook still running after ``timeout`` seconds is killed."""
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        stderr += f"\n[killed after {timeout} s]"
    return Outcome(process.returncode, stdout, stderr)


def host_variables(home: Path, *path_dirs: Path) -> dict:
    return {
        "PATH": os.pathsep.join([*map(str, path_dirs), os.environ["PATH"]]),
        "HOME": str(home),
        "QDB_CICD_AGENT_PYTHON3": sys.executable,
        "PYTHONDONTWRITEBYTECODE": "1",
        **GIT_IDENTITY,
        **{k: os.environ[k] for k in ("TMPDIR",) if k in os.environ},
    }


def plugin_variables(options: dict, path=()) -> dict:
    """Plugin options as the agent hands them over: a list as KEY_<i>, a map as
    KEY_<NAME>, a boolean as true or false, every key upper case with - as _."""
    if isinstance(options, dict):
        return {
            k: v
            for name, value in options.items()
            for k, v in plugin_variables(value, (*path, name)).items()
        }
    if isinstance(options, list):
        return {
            k: v
            for i, value in enumerate(options)
            for k, v in plugin_variables(value, (*path, i)).items()
        }
    name = OPTION_PREFIX + "_".join(str(p).upper().replace("-", "_") for p in path)
    return {name: str(options).lower() if isinstance(options, bool) else str(options)}


# ---------------------------------------------------------------------------
# The plugin, as an agent checks it out and post-checkout prepares it
# ---------------------------------------------------------------------------

# The steps post-checkout prepares apart: a step that plans gets .venv-snapshot.
STEP_KINDS = {"plain": {}, "plan": {"plan": GENERATED}}


@dataclasses.dataclass(frozen=True)
class Plugin:
    root: Path
    prepared: dict  # step kind -> Outcome of its post-checkout


@pytest.fixture(scope="session")
def plugin(tmp_path_factory):
    """The plugin's checkout, committed, as the plan step reads the commit it runs; its two
    venvs made by post-checkout at once."""
    root = tmp_path_factory.mktemp("plugin")
    for part in ("hooks", "lib"):
        shutil.copytree(
            PLUGIN_ROOT / part, root / part, ignore=shutil.ignore_patterns("__pycache__")
        )
    shutil.copy2(PLUGIN_ROOT / "artifacts.py", root)
    committed(root)
    home = tmp_path_factory.mktemp("post-checkout-home")
    started = {
        kind: start_hook(
            root, "post-checkout", home, {**host_variables(home), **plugin_variables(o)}
        )
        for kind, o in STEP_KINDS.items()
    }
    prepared = {kind: finish_hook(p, POST_CHECKOUT_TIMEOUT) for kind, p in started.items()}
    return Plugin(root, prepared)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def build_id(number: int) -> str:
    return f"01900000-0000-7000-8000-{number:012d}"


def build_url(pipeline: str, number: int) -> str:
    return f"https://buildkite.example.test/{ORGANISATION}/{pipeline}/builds/{number}"


@dataclasses.dataclass(frozen=True)
class Job:
    """A job of a step: its plugin checkout, its own checkout and its commit, its environment."""

    plugin: Path
    workspace: Path
    commit: str
    env: dict

    def committed(self, files: dict) -> Job:
        """The job after ``files`` were committed to its checkout."""
        self.write(files)
        return dataclasses.replace(self, commit=committed(self.workspace))

    def with_env(self, **variables) -> Job:
        """Set variables; None removes one."""
        merged = {**self.env, **variables}
        return dataclasses.replace(self, env={k: v for k, v in merged.items() if v is not None})

    def with_plugin(self, options: dict) -> Job:
        """The job of a step with these plugin options, and no others."""
        kept = {k: v for k, v in self.env.items() if not k.startswith(OPTION_PREFIX)}
        return dataclasses.replace(self, env={**kept, **plugin_variables(options)})

    def write(self, files: dict) -> None:
        for rel, data in files.items():
            path = self.workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data.encode() if isinstance(data, str) else data)

    def files(self, under: str) -> dict:
        root = self.workspace / under
        return {
            p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()
        }

    def run(self, hook: str) -> Outcome:
        return finish_hook(start_hook(self.plugin, hook, self.workspace, self.env), HOOK_TIMEOUT)


def store_variables(store: Store, backend: str, destination: str = f"s3://{BUCKET}") -> dict:
    """What a job, or a program outside Buildkite, sets to reach the store; the AWS
    configuration files of its home are absent. An s3 job signs with AWS keys; an r2 job
    holds the R2 key pair and no AWS credential, so that only the pair can sign its requests."""
    aws = {"AWS_ACCESS_KEY_ID": "test-aws-access-key", "AWS_SECRET_ACCESS_KEY": "test-aws-secret"}
    return {
        **(R2 if backend == "r2" else aws),
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ENDPOINT_URL_SSM": store.endpoint,
        "AWS_EC2_METADATA_DISABLED": "true",
        "ARTIFACTS_BACKEND": backend,
        "ARTIFACTS_DESTINATION": destination,
        "ARTIFACTS_ENDPOINT_URL": store.endpoint,
        "ARTIFACTS_DOMAIN": "artifacts.example.test",
    }


@pytest.fixture
def backend():
    """The store backend of every job; a test generates both by parametrizing it."""
    return "s3"


@pytest.fixture
def jobs(tmp_path, store, agent, plugin, backend):
    """Make the job of a step of a build: ``jobs(pipeline, number, branch, step)``.

    Its checkout is a git repository of one commit; ``destination`` names the
    bucket and an optional prefix.
    """
    count = itertools.count(1)
    home = tmp_path / "home"
    home.mkdir()

    def job(pipeline="widget", number=1, branch="master", step="build", destination=None) -> Job:
        workspace = tmp_path / f"job-{next(count)}"
        workspace.mkdir()
        commit = committed(workspace)
        env = {
            **host_variables(home, agent.bin),
            **store_variables(store, backend, destination or f"s3://{BUCKET}"),
            "BUILDKITE": "true",
            "BUILDKITE_ORGANIZATION_SLUG": ORGANISATION,
            "BUILDKITE_PIPELINE_SLUG": pipeline,
            "BUILDKITE_REPO": f"https://git.example.test/{ORGANISATION}/{pipeline}.git",
            "BUILDKITE_BUILD_ID": build_id(number),
            "BUILDKITE_BUILD_NUMBER": str(number),
            "BUILDKITE_BUILD_URL": build_url(pipeline, number),
            "BUILDKITE_BRANCH": branch,
            "BUILDKITE_COMMIT": "HEAD",
            "BUILDKITE_JOB_ID": f"01900000-0000-7000-9000-{number:012d}",
            "BUILDKITE_STEP_KEY": step,
            "BUILDKITE_LABEL": step,
            "BUILDKITE_COMMAND": "make",
            "BUILDKITE_COMMAND_EXIT_STATUS": "0",
            "FAKE_BUILDKITE_STATE": str(agent.state),
        }
        return Job(plugin.root, workspace, commit, env)

    return job
