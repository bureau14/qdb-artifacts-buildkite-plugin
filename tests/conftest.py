"""Harness of the characterisation suite.

The suite drives the plugin only through the surfaces its users have: the hooks,
run the way a Buildkite agent runs them; the command line the hooks call; and the
names exported by lib/__init__.py. Everything else here is the world around those
surfaces: an S3 and SSM server (moto, over real HTTP) that can also refuse or hold
chosen requests, a fake buildkite-agent on PATH, a clock that lets the job's
Python skip its sleeps, the plugin's own .venv, and the job environment an agent
sets.

When the entry point of the command line moves, only run_cli changes. When the
command line reports errors differently, only Outcome.error changes. When the
library raises a different error, only expect_library_failure and the failure
test of the outside caller change.
"""

import contextlib
import dataclasses
import datetime
import gzip
import http.server
import importlib
import io
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import tarfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http import HTTPStatus
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


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


# ---------------------------------------------------------------------------
# The object store: moto in server mode. Every request is recorded, and a test
# can make the store refuse or hold chosen requests of the plugin.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class StoreRequest:
    """What the suite needs to know about one HTTP request the store received."""

    method: str
    host: str
    path: str
    query: dict
    access_key_id: str
    region: str
    service: str
    content_type: str
    content_disposition: str
    if_none_match: str
    target: str
    body: dict
    arrived_at: datetime.datetime

    @property
    def key(self):
        """The object key of a path-style request; "" for the bucket itself."""
        parts = self.path.split("/", 2)
        return parts[2] if len(parts) == 3 else ""


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
        host=environ.get("HTTP_HOST", ""),
        path=environ.get("PATH_INFO", ""),
        query={k: v[0] for k, v in query.items()},
        access_key_id=access_key_id,
        region=region,
        service=service,
        content_type=environ.get("CONTENT_TYPE", ""),
        content_disposition=environ.get("HTTP_CONTENT_DISPOSITION", ""),
        if_none_match=environ.get("HTTP_IF_NONE_MATCH", ""),
        target=target,
        body=read_json_body(environ) if target else {},
        arrived_at=utc_now(),
    )


@dataclasses.dataclass(frozen=True)
class RequestPattern:
    """A plugin request by method, object key and some of its query parameters."""

    method: str
    key: str
    query: tuple = ()

    def matches(self, request):
        return (
            request.access_key_id != ADMIN_ACCESS_KEY_ID
            and (request.method, request.key) == (self.method, self.key)
            and all(request.query.get(name) == value for name, value in self.query)
        )


def drain_body(environ):
    # What the client sent must be read for its connection to stay usable.
    environ["wsgi.input"].read(int(environ.get("CONTENT_LENGTH") or 0))


def answer_with_error(start_response, status, code=""):
    """An S3 error response; without a code its body is empty, as for a HEAD."""
    body = b""
    if code:
        body = (
            "<?xml version='1.0' encoding='UTF-8'?>"
            f"<Error><Code>{code}</Code><Message>refused by test</Message></Error>"
        ).encode()
    start_response(
        f"{status} {HTTPStatus(status).phrase}",
        [("Content-Type", "application/xml"), ("Content-Length", str(len(body)))],
    )
    return [body]


@dataclasses.dataclass(frozen=True)
class Refusal:
    """Answers every matching plugin request with an S3 error; the store never sees it."""

    pattern: RequestPattern
    status: int
    code: str

    def claim(self, request):
        return self.pattern.matches(request)

    def answer(self, traffic, environ, start_response, forward):
        drain_body(environ)
        return answer_with_error(start_response, self.status, self.code)


@dataclasses.dataclass
class Hold:
    """Keeps the first matching plugin request waiting before the store answers it,
    until a request matching `until` has arrived ("until"), the test releases it
    ("released") or `timeout` seconds have passed ("timeout"); ended_by says which
    and released_at when. A hold still waiting when its test ends is "closed": its
    request is answered 503 and never reaches the store of the next test."""

    pattern: RequestPattern
    until: object = None
    timeout: float = HOOK_TIMEOUT
    arrived: threading.Event = dataclasses.field(default_factory=threading.Event)
    ended_by: str = ""
    released_at: object = None

    def claim(self, request):
        if self.arrived.is_set() or not self.pattern.matches(request):
            return False
        self.arrived.set()
        return True

    def awaited_request_arrived(self, requests):
        return self.until is not None and any(self.until.matches(r) for r in requests)

    def answer(self, traffic, environ, start_response, forward):
        if traffic.wait_out(self) == "closed":
            return answer_with_error(start_response, 503, "SlowDown")
        return forward(environ, start_response)


class StoreTraffic:
    """The requests the store received, and the rules it applies to the plugin's."""

    def __init__(self):
        self.requests = []
        self.rules = []
        self.changed = threading.Condition()

    def receive(self, request):
        """Record a request; return the rule that claims it, if one does."""
        with self.changed:
            self.requests.append(request)
            self.changed.notify_all()
            return next((rule for rule in self.rules if rule.claim(request)), None)

    def add_rule(self, rule):
        with self.changed:
            self.rules.append(rule)
        return rule

    def end_hold(self, hold, reason):
        with self.changed:
            hold.ended_by = hold.ended_by or reason
            self.changed.notify_all()

    def wait_out(self, hold):
        """Block until the hold ends; return why it ended."""
        with self.changed:
            over = self.changed.wait_for(
                lambda: hold.ended_by or hold.awaited_request_arrived(self.requests), hold.timeout
            )
            hold.ended_by = hold.ended_by or ("until" if over else "timeout")
            hold.released_at = utc_now()
            return hold.ended_by

    def clear(self):
        """Close every hold still waiting, then forget all requests and rules."""
        with self.changed:
            for rule in self.rules:
                if isinstance(rule, Hold):
                    rule.ended_by = rule.ended_by or "closed"
            self.changed.notify_all()
            self.requests.clear()
            self.rules.clear()


def intercepting(app, traffic):
    def record_and_answer(environ, start_response):
        rule = traffic.receive(describe_request(environ))
        if rule is None:
            return app(environ, start_response)
        return rule.answer(traffic, environ, start_response, app)

    return record_and_answer


class QuietRequestHandler(WSGIRequestHandler):
    def log(self, *args, **kwargs):
        pass


def start_store_server(traffic):
    app = intercepting(DomainDispatcherApplication(create_backend_app), traffic)
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
    traffic: StoreTraffic
    s3: object

    @property
    def requests(self):
        return self.traffic.requests

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

    def refuse(self, method, key, status, code="", query=()):
        """From now on answer the plugin's matching requests with an S3 error."""
        return self.traffic.add_rule(
            Refusal(RequestPattern(method, key, tuple(query)), status, code)
        )

    def hold(self, method, key, query=(), until=None, timeout=HOOK_TIMEOUT):
        """Keep the plugin's next matching request waiting; until is (method, key,
        query) of the request whose arrival ends the hold. See Hold."""
        awaited = RequestPattern(*until) if until else None
        return self.traffic.add_rule(
            Hold(RequestPattern(method, key, tuple(query)), awaited, timeout)
        )

    def release(self, hold):
        self.traffic.end_hold(hold, "released")


@pytest.fixture(scope="session")
def store_server():
    traffic = StoreTraffic()
    server = start_store_server(traffic)
    yield f"http://127.0.0.1:{server.server_port}", traffic
    server.shutdown()


@pytest.fixture
def store(store_server):
    endpoint, traffic = store_server
    reset_store_server(endpoint)
    s3 = admin_client("s3", endpoint)
    s3.create_bucket(Bucket=BUCKET)
    traffic.clear()
    yield Store(endpoint=endpoint, bucket=BUCKET, traffic=traffic, s3=s3)
    traffic.clear()


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
# Fake tools: a buildkite-agent first on PATH, and a Python for post-checkout.
# Each records every call in its own numbered directory: argv NUL-separated.
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

FAKE_PYTHON_SCRIPT = """#!/bin/sh
# Records each call in its own numbered directory: the path it ran as, then its
# arguments, NUL-separated. Asked for "-m venv <dir>", it lays out <dir>/bin/python
# as a copy of itself, so the venv's Python records its calls too.
set -eu
calls="$FAKE_PYTHON_CALLS"
n=0
while [ -e "$calls/$n" ]; do n=$((n + 1)); done
mkdir "$calls/$n"
printf '%s\\0' "$0" "$@" > "$calls/$n/argv"
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "venv" ]; then
  for venv in "$@"; do :; done
  mkdir -p "$venv/bin"
  cp "$0" "$venv/bin/python"
fi
"""


@dataclasses.dataclass(frozen=True)
class ToolCall:
    argv: list
    stdin: str


def read_calls(calls_dir):
    def read(path):
        return path.read_bytes().decode() if path.exists() else ""

    numbered = sorted(calls_dir.iterdir(), key=lambda d: int(d.name))
    return [
        ToolCall(argv=read(d / "argv").split("\0")[:-1], stdin=read(d / "stdin")) for d in numbered
    ]


@dataclasses.dataclass(frozen=True)
class FakeTool:
    path: Path
    calls_dir: Path

    @property
    def bin_dir(self):
        return self.path.parent

    def calls(self):
        return read_calls(self.calls_dir)


def install_fake_tool(root, name, script):
    bin_dir, calls_dir = root / "bin", root / "calls"
    bin_dir.mkdir(parents=True)
    calls_dir.mkdir()
    path = bin_dir / name
    path.write_bytes(script.encode())
    path.chmod(0o755)
    return FakeTool(path=path, calls_dir=calls_dir)


@pytest.fixture
def agent(tmp_path):
    return install_fake_tool(tmp_path / "agent", "buildkite-agent", FAKE_AGENT_SCRIPT)


@pytest.fixture
def fake_python(tmp_path):
    return install_fake_tool(tmp_path / "fake-python", "python3", FAKE_PYTHON_SCRIPT)


# ---------------------------------------------------------------------------
# A clock for the job's Python: time.sleep returns at once and records how long
# it was asked to wait, so retry waits cost nothing and can be checked.
# ---------------------------------------------------------------------------

INSTANT_SLEEP_MODULE = '''"""Imported by every Python of a job whose PYTHONPATH holds this directory."""
import os
import threading
import time

_RECORD = os.environ["QDB_ARTIFACTS_TESTS_SLEEPS"]
_LOCK = threading.Lock()


def _record_instead_of_sleeping(seconds):
    with _LOCK, open(_RECORD, "a") as record:
        record.write(f"{seconds}\\n")


time.sleep = _record_instead_of_sleeping
'''


@dataclasses.dataclass(frozen=True)
class InstantClock:
    """The variables that give a job the instant clock, and what its Python slept."""

    env: dict
    record: Path

    def sleeps(self):
        text = self.record.read_text() if self.record.exists() else ""
        return [float(seconds) for seconds in text.split()]


def install_instant_clock(root):
    # Python imports sitecustomize from PYTHONPATH at start-up, before any plugin
    # code, so a sleep the plugin binds at import is the instant one too. This
    # holds as long as the hooks start Python without -E, -I or -S.
    root.mkdir(parents=True)
    (root / "sitecustomize.py").write_text(INSTANT_SLEEP_MODULE)
    record = root / "sleeps"
    env = {"PYTHONPATH": str(root), "QDB_ARTIFACTS_TESTS_SLEEPS": str(record)}
    return InstantClock(env=env, record=record)


@pytest.fixture
def instant_clock(tmp_path):
    return install_instant_clock(tmp_path / "clock")


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
# Running the plugin's processes, and the plugin's .venv, built by the real
# post-checkout hook once per session.
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


ERROR_MARKER = "[artifacts] ERROR:"


@dataclasses.dataclass(frozen=True)
class Outcome:
    code: int
    stdout: str
    stderr: str

    @property
    def output(self):
        return f"exit {self.code}\n--- stdout\n{self.stdout}\n--- stderr\n{self.stderr}"

    @property
    def error(self):
        """The error the plugin reported, without the progress lines before it:
        today the last "[artifacts] ERROR:" line of stderr and all that follows.
        "" when it reported none, as when a traceback ended it."""
        _, marker, rest = self.stderr.rpartition(ERROR_MARKER)
        return marker + rest if marker else ""


def decode_output(raw):
    return raw.decode("utf-8", errors="replace")


def stop_process_group(process):
    # A hook's Python child outlives a killed bash; end the whole group.
    if os.name == "nt":
        process.kill()
    else:
        os.killpg(process.pid, signal.SIGKILL)
    return process.communicate()


def start_process(argv, job):
    return subprocess.Popen(
        [str(a) for a in argv],
        cwd=job.workspace,
        env=job.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name != "nt",
    )


def finish_process(process, argv, timeout):
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        stdout, stderr = stop_process_group(process)
        pytest.fail(f"{argv[:3]} did not finish in {timeout}s\n{decode_output(stderr)}")
    return Outcome(process.returncode, decode_output(stdout), decode_output(stderr))


def run_process(argv, job, timeout=HOOK_TIMEOUT):
    return finish_process(start_process(argv, job), argv, timeout)


def hook_argv(name, root):
    return [BASH, root / "hooks" / name]


def run_hook(name, job, timeout=HOOK_TIMEOUT, root=PLUGIN_ROOT):
    """Run a hook the way the agent does: bash, the job's environment, its checkout."""
    return run_process(hook_argv(name, root), job, timeout)


def wait_while_running(process, event, timeout):
    """Wait for the event; give up as soon as the process has ended or time is up."""
    deadline = time.monotonic() + timeout
    while not event.wait(0.1):
        if process.poll() is not None or time.monotonic() > deadline:
            return False
    return True


def output_of_ended_or_stopped(process):
    return process.communicate() if process.poll() is not None else stop_process_group(process)


def run_hook_midway(name, job, when, act, timeout=HOOK_TIMEOUT):
    """Run a hook and, once the event `when` is set, call act(process) while it runs."""
    argv = hook_argv(name, PLUGIN_ROOT)
    process = start_process(argv, job)
    if not wait_while_running(process, when, timeout):
        _, stderr = output_of_ended_or_stopped(process)
        pytest.fail(f"{argv[:3]} never got to the awaited point\n{decode_output(stderr)}")
    act(process)
    return finish_process(process, argv, timeout)


def press_ctrl_c(process):
    """What Ctrl-C in a terminal does: SIGINT to the whole foreground process group."""
    os.killpg(process.pid, signal.SIGINT)


def run_hook_interrupted(name, job, when, timeout=HOOK_TIMEOUT):
    return run_hook_midway(name, job, when, press_ctrl_c, timeout)


def run_cli(job, *args):
    """The command line the hooks call. The one place that knows the entry point."""
    return run_process([venv_python(), PLUGIN_ROOT / "artifacts.py", *args], job)


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
def plugin_copy(tmp_path):
    """The plugin's hooks and library in a directory of their own, without .venv, for
    hooks whose run would replace the .venv every other test uses."""
    root = tmp_path / "plugin"
    for part in ("hooks", "lib"):
        shutil.copytree(
            PLUGIN_ROOT / part, root / part, ignore=shutil.ignore_patterns("__pycache__")
        )
    return root


@pytest.fixture
def hook():
    return run_hook


@pytest.fixture
def hook_midway():
    return run_hook_midway


@pytest.fixture
def interrupted_hook():
    return run_hook_interrupted


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
def expect_library_failure(capsys, library):
    """How the library fails: progress on stderr, then ArtifactsError with the facts."""

    @contextlib.contextmanager
    def expecting():
        failure = LibraryFailure()
        with pytest.raises(library.ArtifactsError) as raised:
            yield failure
        failure.text = capsys.readouterr().err + str(raised.value)

    return expecting


def link_plugin_under_namespace(root):
    """Lay the plugin out the way the README's outside caller vendors it."""
    link = root / "vendor" / "qdb_artifacts"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(PLUGIN_ROOT, target_is_directory=True)
    except OSError:
        shutil.copytree(PLUGIN_ROOT, link, ignore=shutil.ignore_patterns(".venv", ".git"))
    return root


@pytest.fixture
def outside_caller(tmp_path, store, backend):
    """Run tests/outside_caller.py in a fresh interpreter with a clean sys.path."""
    namespace_root = link_plugin_under_namespace(tmp_path / "caller-tree")
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
