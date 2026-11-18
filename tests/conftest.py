"""Harness of the characterisation suite.

The suite drives the plugin only through the surfaces its users have: the hooks,
run the way a Buildkite agent runs them; the command line the hooks call; and the
names exported by lib/__init__.py. Everything else here is the world around those
surfaces: an S3 and SSM server (moto, over real HTTP), a fake buildkite-agent on
PATH, the plugin's own .venv, and the job environment an agent sets.

When the entry point of the command line moves, only run_cli changes. When the
library stops ending the process on failure, only expect_library_failure changes.
"""

import contextlib
import dataclasses
import gzip
import http.server
import importlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import boto3
import pytest
import zstandard
from botocore.config import Config
from moto.server import DomainDispatcherApplication, create_backend_app
from werkzeug.serving import WSGIRequestHandler, make_server

# ---------------------------------------------------------------------------
# Fixed facts of the world the plugin runs in. All names are invented.
# ---------------------------------------------------------------------------

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_OPTION_PREFIX = "BUILDKITE_PLUGIN_QDB_ARTIFACTS_"
REUSE_VENV_VARIABLE = "QDB_ARTIFACTS_TESTS_REUSE_VENV"

BUCKET = "example-artifacts"
ARTIFACTS_DOMAIN = "artifacts.example.test"
AWS_REGION = "us-east-1"
AWS_ACCESS_KEY_ID = "test-aws-access-key"
ADMIN_ACCESS_KEY_ID = "test-harness-admin"
R2_ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
R2_ACCESS_KEY_ID = "test-r2-access-key"
R2_SECRET_ACCESS_KEY = "test-r2-secret-access-key"

PIPELINE_SLUG = "widget-build"
BUILD_ID = "01900000-0000-7000-8000-000000000001"
BRANCH = "master"
JOB_LABEL = ":hammer: Build widget"

HOOK_TIMEOUT = 300
POST_CHECKOUT_TIMEOUT = 900


def find_bash():
    git_bash = Path(r"C:\Program Files\Git\bin\bash.exe")
    return str(git_bash) if git_bash.is_file() else shutil.which("bash")


BASH = find_bash()


# ---------------------------------------------------------------------------
# The object store: moto in server mode, every request recorded.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StoreRequest:
    """What the suite needs to know about one HTTP request the store received."""

    method: str
    path: str
    query: dict
    access_key_id: str
    region: str
    service: str
    content_type: str
    content_disposition: str
    target: str
    body: dict


def parse_credential_scope(authorization):
    # "AWS4-HMAC-SHA256 Credential=<key>/<date>/<region>/<service>/aws4_request, ..."
    marker = "Credential="
    if marker not in authorization:
        return "", "", ""
    scope = authorization.split(marker, 1)[1].split(",", 1)[0].split("/")
    return scope[0], scope[2], scope[3]


def read_json_body(environ):
    """Read a JSON API body and put it back so the server can still read it."""
    length = int(environ.get("CONTENT_LENGTH") or 0)
    raw = environ["wsgi.input"].read(length)
    environ["wsgi.input"] = io.BytesIO(raw)
    return json.loads(raw or b"{}")


def describe_request(environ):
    access_key_id, region, service = parse_credential_scope(environ.get("HTTP_AUTHORIZATION", ""))
    target = environ.get("HTTP_X_AMZ_TARGET", "")
    query = urllib.parse.parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
    return StoreRequest(
        method=environ["REQUEST_METHOD"],
        path=environ.get("PATH_INFO", ""),
        query={k: v[0] for k, v in query.items()},
        access_key_id=access_key_id,
        region=region,
        service=service,
        content_type=environ.get("CONTENT_TYPE", ""),
        content_disposition=environ.get("HTTP_CONTENT_DISPOSITION", ""),
        target=target,
        body=read_json_body(environ) if target else {},
    )


def recording(app, requests):
    def record_and_forward(environ, start_response):
        requests.append(describe_request(environ))
        return app(environ, start_response)

    return record_and_forward


class QuietRequestHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def start_store_server(requests):
    app = recording(DomainDispatcherApplication(create_backend_app), requests)
    server = make_server("127.0.0.1", 0, app, threaded=True, request_handler=QuietRequestHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def reset_store_server(endpoint):
    request = urllib.request.Request(f"{endpoint}/moto-api/reset", method="POST")
    with urllib.request.urlopen(request) as response:
        response.read()


def admin_client(service, endpoint, region=AWS_REGION):
    return boto3.client(
        service,
        endpoint_url=endpoint,
        region_name=region,
        aws_access_key_id=ADMIN_ACCESS_KEY_ID,
        aws_secret_access_key="test-harness-admin-secret",
        # moto gives multipart objects a wrong whole-object checksum.
        config=Config(response_checksum_validation="when_required"),
    )


def build_prefix(project, ref, variant, build_id):
    return f"{project}/{ref}/variants/{variant}/builds/{build_id}"


def pointer_key(project, ref, variant):
    return f"{project}/{ref}/variants/{variant}/LATEST_SUCCESSFUL"


@dataclasses.dataclass(frozen=True)
class Store:
    """The bucket the plugin talks to, seen through an administrator's client."""

    endpoint: str
    bucket: str
    requests: list
    s3: object

    def put(self, key, body=b""):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body)

    def get(self, key):
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def head(self, key):
        return self.s3.head_object(Bucket=self.bucket, Key=key)

    def stored_keys(self, prefix=""):
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
        return sorted(o["Key"] for page in pages for o in page.get("Contents", []))

    def put_build(self, project, ref, variant, build_id, files, manifest=True):
        """Store a build the way an upload leaves it: the files, then manifest.json."""
        prefix = build_prefix(project, ref, variant, build_id)
        for rel, body in files.items():
            self.put(f"{prefix}/{rel}", body)
        if manifest:
            self.put(f"{prefix}/manifest.json", json.dumps({"file_count": len(files)}).encode())
        return prefix

    def put_pointer(self, project, ref, variant, body):
        self.put(
            pointer_key(project, ref, variant), body.encode() if isinstance(body, str) else body
        )

    def put_parameter(self, name, value, secure=False, region=AWS_REGION):
        admin_client("ssm", self.endpoint, region).put_parameter(
            Name=name, Value=value, Type="SecureString" if secure else "String"
        )

    def plugin_requests(self, service=None):
        """Requests made by the plugin, not by the harness, optionally of one service."""
        return [
            r
            for r in self.requests
            if r.access_key_id != ADMIN_ACCESS_KEY_ID and service in (None, r.service)
        ]


@pytest.fixture(scope="session")
def store_server():
    requests = []
    server = start_store_server(requests)
    yield f"http://127.0.0.1:{server.server_port}", requests
    server.shutdown()


@pytest.fixture
def store(store_server):
    endpoint, requests = store_server
    reset_store_server(endpoint)
    s3 = admin_client("s3", endpoint)
    s3.create_bucket(Bucket=BUCKET)
    requests.clear()
    return Store(endpoint=endpoint, bucket=BUCKET, requests=requests, s3=s3)


class DenyingSsmHandler(http.server.BaseHTTPRequestHandler):
    """An SSM endpoint that refuses every call, as an IAM policy without the grant would."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = json.dumps({"__type": "AccessDeniedException", "message": "denied by test"})
        self.send_response(400)
        self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *args):
        pass


@pytest.fixture
def denying_ssm_endpoint():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), DenyingSsmHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


# ---------------------------------------------------------------------------
# Archives as builds publish them. Members are {name: data} for plain files, or
# a list of entries {"name", "data", "mode", "type", "target"} for anything else.
# ---------------------------------------------------------------------------

TAR_TYPES = {
    "file": tarfile.REGTYPE,
    "dir": tarfile.DIRTYPE,
    "symlink": tarfile.SYMTYPE,
    "hardlink": tarfile.LNKTYPE,
    "fifo": tarfile.FIFOTYPE,
}


def archive_entries(members):
    if isinstance(members, dict):
        return [{"name": name, "data": data} for name, data in members.items()]
    return members


def tar_member(entry):
    info = tarfile.TarInfo(entry["name"])
    info.type = TAR_TYPES[entry.get("type", "file")]
    info.mode = entry.get("mode", 0o755 if info.isdir() else 0o644)
    info.linkname = entry.get("target", "")
    data = entry.get("data", b"") if info.isreg() else b""
    info.size = len(data)
    return info, io.BytesIO(data) if info.isreg() else None


def tar_bytes(members):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        for entry in archive_entries(members):
            tar.addfile(*tar_member(entry))
    return raw.getvalue()


def zip_bytes(members):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        for entry in archive_entries(members):
            archive.writestr(entry["name"], entry.get("data", b""))
    return raw.getvalue()


def zstd_tar_bytes(members):
    return zstandard.ZstdCompressor().compress(tar_bytes(members))


def gzip_tar_bytes(members):
    return gzip.compress(tar_bytes(members))


ARCHIVE_WRITERS = {
    ".tar.zst": zstd_tar_bytes,
    ".tar.zstd": zstd_tar_bytes,
    ".tar.gz": gzip_tar_bytes,
    ".zip": zip_bytes,
    ".jar": zip_bytes,
}


def make_archive(suffix, members):
    return ARCHIVE_WRITERS[suffix](members)


@pytest.fixture
def archive():
    return make_archive


# ---------------------------------------------------------------------------
# The fake buildkite-agent: first on PATH, records every call.
# ---------------------------------------------------------------------------

FAKE_AGENT_SCRIPT = """#!/bin/sh
# Records each call in its own numbered directory: argv NUL-separated, then stdin.
set -eu
calls="$FAKE_BUILDKITE_AGENT_CALLS"
n=0
while [ -e "$calls/$n" ]; do n=$((n + 1)); done
mkdir "$calls/$n"
printf '%s\\0' "$@" > "$calls/$n/argv"
cat > "$calls/$n/stdin"
exit "${FAKE_BUILDKITE_AGENT_EXIT_STATUS:-0}"
"""


@dataclasses.dataclass(frozen=True)
class AgentCall:
    argv: list
    stdin: str


@dataclasses.dataclass(frozen=True)
class FakeAgent:
    bin_dir: Path
    calls_dir: Path

    def calls(self):
        numbered = sorted(self.calls_dir.iterdir(), key=lambda d: int(d.name))
        return [
            AgentCall(
                argv=(d / "argv").read_bytes().decode().split("\0")[:-1],
                stdin=(d / "stdin").read_bytes().decode(),
            )
            for d in numbered
        ]


def install_fake_agent(root):
    bin_dir, calls_dir = root / "bin", root / "calls"
    bin_dir.mkdir(parents=True)
    calls_dir.mkdir()
    script = bin_dir / "buildkite-agent"
    script.write_bytes(FAKE_AGENT_SCRIPT.encode())
    script.chmod(0o755)
    return FakeAgent(bin_dir=bin_dir, calls_dir=calls_dir)


@pytest.fixture
def agent(tmp_path):
    return install_fake_agent(tmp_path / "agent")


# ---------------------------------------------------------------------------
# The job environment, built from nothing so the host's variables cannot leak in.
# ---------------------------------------------------------------------------


def host_variables(path_dirs, home):
    passed_through = ("SYSTEMROOT", "COMSPEC", "PATHEXT", "WINDIR", "TMPDIR", "TEMP", "TMP")
    return {
        **{k: os.environ[k] for k in passed_through if k in os.environ},
        "PATH": os.pathsep.join([*map(str, path_dirs), os.environ.get("PATH", "")]),
        "HOME": str(home),
        "QDB_CICD_AGENT_PYTHON3": sys.executable,
        # Keeps the plugin tree free of bytecode whatever way Python is started.
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def aws_variables(endpoint, home):
    return {
        "AWS_ACCESS_KEY_ID": AWS_ACCESS_KEY_ID,
        "AWS_SECRET_ACCESS_KEY": "test-aws-secret-access-key",
        "AWS_DEFAULT_REGION": AWS_REGION,
        "AWS_ENDPOINT_URL_SSM": endpoint,
        "AWS_CONFIG_FILE": str(home / "absent-aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(home / "absent-aws-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
    }


def store_variables(endpoint, backend):
    common = {
        "ARTIFACTS_BACKEND": backend,
        "ARTIFACTS_DESTINATION": f"s3://{BUCKET}",
        "ARTIFACTS_ENDPOINT_URL": endpoint,
        "ARTIFACTS_DOMAIN": ARTIFACTS_DOMAIN,
    }
    if backend != "r2":
        return common
    return {
        **common,
        "ARTIFACTS_R2_ACCOUNT_ID": R2_ACCOUNT_ID,
        "ARTIFACTS_R2_ACCESS_KEY_ID": R2_ACCESS_KEY_ID,
        "ARTIFACTS_R2_SECRET_ACCESS_KEY": R2_SECRET_ACCESS_KEY,
    }


def buildkite_variables(workspace):
    return {
        "BUILDKITE": "true",
        "BUILDKITE_BUILD_ID": BUILD_ID,
        "BUILDKITE_PIPELINE_SLUG": PIPELINE_SLUG,
        "BUILDKITE_BRANCH": BRANCH,
        "BUILDKITE_LABEL": JOB_LABEL,
        "BUILDKITE_BUILD_CHECKOUT_PATH": str(workspace),
    }


def plugin_env_key(path):
    return PLUGIN_OPTION_PREFIX + "_".join(
        str(p).upper().replace("-", "_").replace(" ", "_") for p in path
    )


def flatten_plugin_config(config, path=()):
    """Plugin configuration as the agent hands it over: lists as KEY_<i>, maps as
    KEY_<NAME>, scalars as KEY, booleans as true/false."""
    if isinstance(config, dict):
        return {
            k: v
            for name, value in config.items()
            for k, v in flatten_plugin_config(value, (*path, name)).items()
        }
    if isinstance(config, list):
        return {
            k: v
            for i, value in enumerate(config)
            for k, v in flatten_plugin_config(value, (*path, i)).items()
        }
    if isinstance(config, bool):
        return {plugin_env_key(path): "true" if config else "false"}
    return {plugin_env_key(path): str(config)}


@dataclasses.dataclass(frozen=True)
class Job:
    """A Buildkite job: its checkout directory and the environment of its hooks."""

    workspace: Path
    env: dict

    def with_env(self, **variables):
        """Set variables; None removes one."""
        merged = {**self.env, **variables}
        return dataclasses.replace(self, env={k: v for k, v in merged.items() if v is not None})

    def with_plugin(self, config):
        """Configure the plugin as a pipeline step would."""
        flat = flatten_plugin_config(config)
        return self.with_env(**flat, BUILDKITE_PLUGIN_CONFIGURATION=json.dumps(config))

    def with_options(self, **options):
        """Set raw plugin variables, for forms the agent's flattening never produces."""
        return self.with_env(**{PLUGIN_OPTION_PREFIX + k: v for k, v in options.items()})

    def write(self, rel, data=b""):
        path = self.workspace / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def files(self, under="."):
        root = self.workspace / under
        return sorted(p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())


@pytest.fixture
def backend():
    """The store backend a job is configured for; tests parametrize it to cover both."""
    return "s3"


@pytest.fixture
def job(tmp_path, store, agent, backend, plugin_venv):
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **host_variables([agent.bin_dir], home),
        **aws_variables(store.endpoint, home),
        **store_variables(store.endpoint, backend),
        **buildkite_variables(workspace),
        "FAKE_BUILDKITE_AGENT_CALLS": str(agent.calls_dir),
    }
    return Job(workspace=workspace, env=env)


# ---------------------------------------------------------------------------
# The plugin's .venv, built by the real post-checkout hook once per session.
# ---------------------------------------------------------------------------


def venv_python():
    if os.name == "nt":
        return PLUGIN_ROOT / ".venv" / "Scripts" / "python.exe"
    return PLUGIN_ROOT / ".venv" / "bin" / "python"


def venv_version():
    cfg = PLUGIN_ROOT / ".venv" / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    entries = dict(line.split("=", 1) for line in cfg.read_text().splitlines() if "=" in line)
    return {k.strip(): v.strip() for k, v in entries.items()}.get("version")


@dataclasses.dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    @property
    def output(self):
        return f"exit {self.code}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"


def decode_output(raw):
    return raw.decode("utf-8", errors="replace")


def stop_process_group(process):
    # A hook's Python child outlives a killed bash; end the whole group.
    if os.name == "nt":
        process.kill()
    else:
        os.killpg(process.pid, 9)
    return process.communicate()


def run_process(argv, job, timeout=HOOK_TIMEOUT):
    process = subprocess.Popen(
        [str(a) for a in argv],
        cwd=job.workspace,
        env=job.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = stop_process_group(process)
        pytest.fail(f"{argv[:3]} did not finish in {timeout}s\n{decode_output(stderr)}")
    return Outcome(process.returncode, decode_output(stdout), decode_output(stderr))


def run_hook(name, job, timeout=HOOK_TIMEOUT):
    """Run a hook the way the agent does: bash, the job's environment, its checkout."""
    return run_process([BASH, PLUGIN_ROOT / "hooks" / name], job, timeout)


def run_cli(job, *args):
    """The command line the hooks call. The one place that knows the entry point."""
    return run_process([venv_python(), PLUGIN_ROOT / "lib" / "artifacts.py", *args], job)


def build_plugin_venv(home):
    job = Job(workspace=home, env=host_variables([], home))
    outcome = run_hook("post-checkout", job, timeout=POST_CHECKOUT_TIMEOUT)
    if outcome.code != 0:
        pytest.fail(f"post-checkout could not build the plugin's .venv\n{outcome.output}")


@pytest.fixture(scope="session")
def plugin_venv(tmp_path_factory):
    """The plugin's .venv for the interpreter under test. Set the reuse variable to 1
    to keep an existing .venv built for the same interpreter version."""
    reuse = os.environ.get(REUSE_VENV_VARIABLE) == "1"
    if not (reuse and venv_version() == platform.python_version()):
        build_plugin_venv(tmp_path_factory.mktemp("post-checkout-home"))
    assert venv_version() == platform.python_version(), "the .venv is not for this interpreter"
    return venv_python()


@pytest.fixture
def hook():
    return run_hook


@pytest.fixture
def cli():
    return run_cli


# ---------------------------------------------------------------------------
# The library, imported from the plugin root and from a nested namespace path.
# ---------------------------------------------------------------------------


def import_library_from_plugin_root():
    if str(PLUGIN_ROOT) not in sys.path:
        sys.path.insert(0, str(PLUGIN_ROOT))
    lib = importlib.import_module("lib")
    assert Path(lib.__file__).resolve().parent == PLUGIN_ROOT / "lib"
    return lib


@pytest.fixture(scope="session")
def library():
    return import_library_from_plugin_root()


@pytest.fixture
def library_environment(store, backend, tmp_path, monkeypatch):
    """The process environment of an in-process library caller, built from nothing."""
    owned = ("AWS_", "ARTIFACTS_", "BUILDKITE")
    for name in [k for k in os.environ if k.startswith(owned)]:
        monkeypatch.delenv(name)
    variables = {
        **aws_variables(store.endpoint, tmp_path),
        **store_variables(store.endpoint, backend),
    }
    for name, value in variables.items():
        monkeypatch.setenv(name, value)
    return variables


@dataclasses.dataclass
class LibraryFailure:
    text: str = ""


@pytest.fixture
def expect_library_failure(capsys):
    """How the library fails today: diagnostics on stderr, then SystemExit(1)."""

    @contextlib.contextmanager
    def expecting():
        failure = LibraryFailure()
        with pytest.raises(SystemExit) as raised:
            yield failure
        assert raised.value.code == 1
        failure.text = capsys.readouterr().err + str(raised.value)

    return expecting


def link_plugin_under_namespace(root):
    """Lay the plugin out the way the outside caller vendors it."""
    link = root / "thirdparty" / "qdb_artifacts"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(PLUGIN_ROOT, target_is_directory=True)
    except OSError:
        shutil.copytree(PLUGIN_ROOT, link, ignore=shutil.ignore_patterns(".venv", ".git"))
    return root


@pytest.fixture
def outside_caller(tmp_path, store, backend):
    """Run tests/outside_caller.py in a fresh interpreter with a clean sys.path."""
    namespace_root = link_plugin_under_namespace(tmp_path / "dags")
    home = tmp_path / "caller-home"
    home.mkdir()
    job = Job(
        workspace=tmp_path,
        env={
            **host_variables([], home),
            **aws_variables(store.endpoint, home),
            **store_variables(store.endpoint, backend),
        },
    )
    script = Path(__file__).resolve().parent / "outside_caller.py"

    def call(request):
        argv = [sys.executable, "-I", "-B", script, namespace_root, json.dumps(request)]
        return run_process(argv, job)

    return call
