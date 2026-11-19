"""Stand-ins for what the gate steps talk to, so that they run through the real hooks.

- ``Hosting``: repositories in local directories, reached by their https URL
  through git's own ``url.<base>.insteadOf``; a ``git`` first on PATH lets
  git use the file transport, which the plan step otherwise refuses.
- ``BuildkiteApi``: Buildkite's REST API behind an https proxy, which a job
  reaches through ``HTTPS_PROXY`` with a certificate of a test authority
  that its Python trusts from start-up (``TRUST_MODULE``): the plan step's
  own https request, with its token, arrives here.
- ``Agent``: ``buildkite-agent`` of every build: meta-data, step outcomes,
  uploads and annotations, kept as files.
- ``Buildkite``: plays builds. A build starts with the runner's gate; a step
  runs as the agent runs it, its hooks with the options of its plugin
  configuration; a trigger step starts the build it asks for with the
  environment it gives; an upload adds its steps to the build.

Every name here is invented. Nothing reaches a network: the proxy and the
store listen on 127.0.0.1, git reads local directories.
"""

from __future__ import annotations

import dataclasses
import datetime
import http.server
import itertools
import json
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
from pathlib import Path

import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

ORGANISATION = "example"
HOST = "https://git.example.test/example/"
API_HOST = "api.buildkite.com"
TOKEN = "bkua_test-token-not-real"
PLUGIN = "bureau14/qdb-artifacts"
GENERATED = ".buildkite/snapshot-pipeline.yml"
RUNNER = Path(__file__).resolve().parent / "runner"


# ---------------------------------------------------------------------------
# Git: the repositories, as GitHub serves them
# ---------------------------------------------------------------------------

AUTHOR = {
    "GIT_AUTHOR_NAME": "snapshot test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "snapshot test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}

GIT_WRAPPER = """#!/bin/sh
# git as on an agent, but allowed the file transport, through which the
# stand-in for GitHub serves its repositories.
if [ -n "${{GIT_ALLOW_PROTOCOL:-}}" ]; then
  GIT_ALLOW_PROTOCOL="$GIT_ALLOW_PROTOCOL:file"
  export GIT_ALLOW_PROTOCOL
fi
exec {git} "$@"
"""


def git(cwd: Path, *args: str, env: dict | None = None) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if done.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} in {cwd}: {done.stderr}")
    return done.stdout.strip()


class Hosting:
    """Repositories under ``root``, one per project, each at ``HOST<project>.git``."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        wrapper = self.bin / "git"
        wrapper.write_text(GIT_WRAPPER.format(git=shutil.which("git")))
        wrapper.chmod(0o755)
        self.env = {
            **{k: v for k, v in os.environ.items() if k != "GIT_DIR"},
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            **AUTHOR,
        }

    @staticmethod
    def url(project: str) -> str:
        return f"{HOST}{project}.git"

    def path(self, project: str) -> Path:
        return self.root / f"{project}.git"

    def environment(self) -> dict:
        """What a job's git needs to reach the repositories by their https URLs."""
        return {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.root.as_uri()}/.insteadOf",
            "GIT_CONFIG_VALUE_0": HOST,
            **AUTHOR,
        }

    def commit(self, project: str, files: dict, branch: str = "master") -> str:
        """Commit ``files`` (path to text, None to remove) on ``branch``; the new commit."""
        repo = self.path(project)
        if not repo.exists():
            repo.mkdir()
            git(repo, "init", "-q", "-b", branch, env=self.env)
            git(repo, "config", "uploadpack.allowFilter", "true", env=self.env)
            git(repo, "config", "uploadpack.allowAnySHA1InWant", "true", env=self.env)
        elif git(repo, "branch", "--list", branch, env=self.env):
            git(repo, "checkout", "-q", branch, env=self.env)
        else:
            git(repo, "checkout", "-q", "-b", branch, env=self.env)
        for path, text in files.items():
            target = repo / path
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(repo, "add", "-A", env=self.env)
        git(repo, "commit", "-q", "--allow-empty", "-m", f"{project} on {branch}", env=self.env)
        return git(repo, "rev-parse", "HEAD", env=self.env)

    def head(self, project: str, branch: str) -> str:
        return git(self.path(project), "rev-parse", f"refs/heads/{branch}", env=self.env)

    def checkout(self, project: str, commit: str, into: Path) -> Path:
        """A checkout of ``commit``, as the agent makes one for a job."""
        git(into.parent, "clone", "-q", self.path(project).as_uri(), str(into), env=self.env)
        git(into, "checkout", "-q", commit, env=self.env)
        return into


# ---------------------------------------------------------------------------
# The pipelines of the tests, as their repositories commit them
# ---------------------------------------------------------------------------

PIPELINE_PY = '''import os
from pathlib import Path

import yaml
from qdb_pipeline import run


def generate_pipeline():
    """The steps of the templates, for the branch of the build.

    unread-steps.txt is no template: the reading rule never sees its steps.
    """
    ref = "refs/heads/" + os.environ["BUILDKITE_BRANCH"]
    texts = [Path(".buildkite/steps.yml").read_text()]
    unread = Path(".buildkite/unread-steps.txt")
    if unread.exists():
        texts.append(unread.read_text())
    return {"steps": [s for t in texts for s in yaml.safe_load(t.replace("{git_ref}", ref))["steps"]]}


if __name__ == "__main__":
    run(generate_pipeline)
'''

BUILD_SH = """#!/bin/sh
# What a build step compiles: one line in $CHAIN_COMPILED, and a file that
# names its project, what it makes and its commit, followed by every file it
# downloaded. A file of the test under $CHAIN_BREAKS breaks it.
set -eu
what="$1"
echo "$BUILDKITE_PIPELINE_SLUG $BUILDKITE_BUILD_NUMBER $BUILDKITE_STEP_KEY" >> "$CHAIN_COMPILED"
if [ -e "$CHAIN_BREAKS/$BUILDKITE_PIPELINE_SLUG-$what" ]; then
  echo "broken by the test" >&2
  exit 1
fi
mkdir -p out
{
  echo "$BUILDKITE_PIPELINE_SLUG $what $BUILDKITE_COMMIT"
  if [ -d in ]; then cat in/*; fi
} > "out/$BUILDKITE_PIPELINE_SLUG-$what.bin"
"""


def download_entry(project: str, variant: str, latest: bool = False) -> dict:
    entry = {
        "project_id": project,
        "variant": variant,
        "git_ref": "{git_ref}",
        "files": ["*.bin"],
        "output-dir": "in",
    }
    return {**entry, "build_id": "LATEST_SUCCESSFUL"} if latest else entry


def template_step(project: str, key: str, variant: str | None, downloads: list) -> dict:
    """A step that compiles ``variant`` from ``downloads``, uploads and promotes it.

    ``downloads`` are ``(project, variant)``, or ``(project, variant, True)`` for
    a latest successful dependency. A step without a variant only downloads.
    """
    options: dict = {}
    if downloads:
        options["download"] = {"projects": [download_entry(*d) for d in downloads]}
    if variant:
        options["upload"] = {
            "variant": variant,
            "git_ref": "{git_ref}",
            "files": "out/*",
            "base_dir": "out",
        }
        options["promote"] = {"variant": variant, "git_ref": "{git_ref}"}
    return {
        "label": f"{project} {key}",
        "key": key,
        "command": f"sh .buildkite/build.sh {variant or key}",
        "plugins": [{f"{PLUGIN}#master": options}],
    }


def pipeline_files(project: str, steps: list) -> dict:
    """The files a repository commits: its template, its pipeline.py, its build script.

    ``steps`` are ``(key, variant, downloads)`` of ``template_step``.
    """
    template = {"steps": [template_step(project, *s) for s in steps]}
    return {
        ".buildkite/steps.yml": yaml.safe_dump(template),
        ".buildkite/pipeline.py": PIPELINE_PY,
        ".buildkite/build.sh": BUILD_SH,
    }


# ---------------------------------------------------------------------------
# Buildkite's REST API, behind an https proxy
# ---------------------------------------------------------------------------


def certificates(directory: Path) -> tuple[Path, Path, Path]:
    """A test authority and a certificate of ``API_HOST`` signed by it: (authority, cert, key)."""
    now = datetime.datetime.now(datetime.timezone.utc)
    valid = {"not_valid_before": now - datetime.timedelta(days=1)}
    valid["not_valid_after"] = now + datetime.timedelta(days=2)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "snapshot test authority")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid["not_valid_before"])
        .not_valid_after(valid["not_valid_after"])
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, API_HOST)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(valid["not_valid_before"])
        .not_valid_after(valid["not_valid_after"])
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(API_HOST)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False)
        .sign(ca_key, hashes.SHA256())
    )
    files = (directory / "authority.pem", directory / "api.pem", directory / "api.key")
    files[0].write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    files[1].write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    files[2].write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return files


TRUST_MODULE = '''"""Imported by every Python of a job whose PYTHONPATH holds this directory.

Its https requests trust the authority of the stand-in for Buildkite's API
besides the system's. SSL_CERT_FILE says that to OpenSSL only: LibreSSL, which
macOS's own Python uses, reads no variable for it.
"""
import os
import ssl

_AUTHORITY = os.environ["QDB_ARTIFACTS_TESTS_AUTHORITY"]
_create = ssl.create_default_context


def _trusting(*args, **kwargs):
    context = _create(*args, **kwargs)
    context.load_verify_locations(_AUTHORITY)
    return context


ssl._create_default_https_context = _trusting
'''


def configuration_running_pipeline_py() -> str:
    return (
        "steps:\n  - label: pipeline\n"
        "    command: python3 .buildkite/pipeline.py | buildkite-agent pipeline upload\n"
    )


class BuildkiteApi:
    """GET /v2/organizations/<organisation>/pipelines/<slug>, from ``pipelines``.

    ``pipelines`` maps a slug to the JSON answer; a slug not in it is a 404.
    ``statuses`` gives a slug another answer. ``requests`` records each
    request as ``(path, authorization)``.
    """

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True)
        self.authority, cert, key = certificates(directory)
        self.trust = directory / "trust"
        self.trust.mkdir()
        (self.trust / "sitecustomize.py").write_text(TRUST_MODULE)
        self.pipelines: dict[str, dict] = {}
        self.statuses: dict[str, int] = {}
        self.requests: list[tuple[str, str]] = []
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        api = self

        class Api(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                api.requests.append((self.path, self.headers.get("Authorization", "")))
                slug = self.path.rsplit("/", 1)[-1]
                known = self.path == f"/v2/organizations/{ORGANISATION}/pipelines/{slug}"
                status = api.statuses.get(slug, 200 if known and slug in api.pipelines else 404)
                body = api.pipelines[slug] if status == 200 else {"message": f"answer {status}"}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self) -> None:
                if self.path != f"{API_HOST}:443":
                    self.send_error(403)
                    return
                self.send_response(200, "Connection established")
                self.end_headers()
                tunnel = tls.wrap_socket(self.connection, server_side=True)
                try:
                    Api(tunnel, self.client_address, None)
                finally:
                    tunnel.close()
                self.close_connection = True

            def log_message(self, *args) -> None:
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def environment(self) -> dict:
        """What a job needs to reach this API by Buildkite's own URL: the proxy, and the trust."""
        return {
            "HTTPS_PROXY": f"http://127.0.0.1:{self.server.server_port}",
            "NO_PROXY": "127.0.0.1,localhost",
            "PYTHONPATH": str(self.trust),
            "QDB_ARTIFACTS_TESTS_AUTHORITY": str(self.authority),
        }

    def add(self, project: str) -> None:
        self.pipelines[project] = {
            "slug": project,
            "repository": Hosting.url(project),
            "configuration": configuration_running_pipeline_py(),
        }

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


# ---------------------------------------------------------------------------
# buildkite-agent: what it keeps of every build
# ---------------------------------------------------------------------------

AGENT_SCRIPT = '''#!{python}
"""buildkite-agent of the stand-in for Buildkite: every build's meta-data, step
outcomes, uploads and annotations, as files under $FAKE_BUILDKITE_STATE."""
import os
import sys
from pathlib import Path

STATE = Path(os.environ["FAKE_BUILDKITE_STATE"])


def read_input():
    return sys.stdin.buffer.read().decode("utf-8")


def write_output(text):
    sys.stdout.buffer.write(text.encode("utf-8"))


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
    here = STATE / "builds" / build
    if args[:2] == ["meta-data", "exists"]:
        return 0 if (here / "meta-data" / args[2]).exists() else 100
    if args[:2] == ["meta-data", "get"]:
        found = here / "meta-data" / args[2]
        if not found.exists():
            print("fatal: failed to get meta-data: not found", file=sys.stderr)
            return 1
        write_output(found.read_text(encoding="utf-8") + "\\n")
        return 0
    if args[:2] == ["meta-data", "set"]:
        value = args[3] if len(args) > 3 else read_input()
        if not value.strip():
            print("fatal: value cannot be empty", file=sys.stderr)
            return 1
        (here / "meta-data").mkdir(parents=True, exist_ok=True)
        (here / "meta-data" / args[2]).write_text(value, encoding="utf-8")
        return 0
    if args[:3] == ["step", "get", "outcome"]:
        found = here / "outcomes" / option(args, "--step")
        write_output(found.read_text(encoding="utf-8") if found.exists() else "")
        return 0
    if args[:2] == ["pipeline", "upload"]:
        numbered(here / "uploads").write_text(read_input(), encoding="utf-8")
        return 0
    if args[:1] == ["annotate"]:
        found = here / "annotations"
        found.mkdir(parents=True, exist_ok=True)
        context = option(args, "--context")
        given = len(args) > 1 and not args[1].startswith("--")
        body = args[1] if given else read_input()
        (found / (context + ".style")).write_text(option(args, "--style"), encoding="utf-8")
        (found / context).write_text(body, encoding="utf-8")
        return 0
    print("fatal: the stand-in knows no such command", file=sys.stderr)
    return 1


sys.exit(main(sys.argv[1:]))
'''


class Agent:
    """The state the stand-in agent keeps, read and written by the test as Buildkite would."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bin = root / "bin"
        self.bin.mkdir(parents=True)
        script = self.bin / "buildkite-agent"
        script.write_text(AGENT_SCRIPT.format(python=sys.executable))
        script.chmod(0o755)

    def environment(self) -> dict:
        return {"FAKE_BUILDKITE_STATE": str(self.root)}

    def _build(self, build: str) -> Path:
        return self.root / "builds" / build

    def meta_data(self, build: str) -> dict:
        found = self._build(build) / "meta-data"
        return {p.name: p.read_text() for p in sorted(found.iterdir())} if found.exists() else {}

    def set_meta_data(self, build: str, key: str, value: str) -> None:
        found = self._build(build) / "meta-data"
        found.mkdir(parents=True, exist_ok=True)
        (found / key).write_text(value)

    def set_outcome(self, build: str, step: str, outcome: str) -> None:
        found = self._build(build) / "outcomes"
        found.mkdir(parents=True, exist_ok=True)
        (found / step).write_text(outcome)

    def uploads(self, build: str) -> list[str]:
        found = self._build(build) / "uploads"
        return [p.read_text() for p in sorted(found.iterdir())] if found.exists() else []

    def annotations(self, build: str) -> dict:
        """``{context: (style, body)}`` of the build's annotations."""
        found = self._build(build) / "annotations"
        if not found.exists():
            return {}
        return {
            p.name: ((found / f"{p.name}.style").read_text(), p.read_text())
            for p in sorted(found.iterdir())
            if p.suffix != ".style"
        }


# ---------------------------------------------------------------------------
# Buildkite: builds, their steps and jobs
# ---------------------------------------------------------------------------

LEAVE = "snapshot-leave"
# The gate steps whose post-checkout installs PyYAML; leave reads no document.
READS_DOCUMENTS = {"plan", "resume"}


def gate_steps(project: str, commit: str, plugin_commit: str) -> list[dict]:
    """The runner's gate (specification section 6): enter and leave."""
    group = f"snapshot/{project}/{commit}"
    plugin = f"{PLUGIN}#{plugin_commit}"
    common = {"priority": 100, "timeout_in_minutes": 15, "concurrency": 1}
    return [
        {
            "label": ":camera_with_flash: snapshot",
            "key": "snapshot-enter",
            **common,
            "concurrency_group": group,
            "command": (
                "python3 -m venv .env\n"
                ".env/bin/python3 -m pip install -q -r .buildkite/requirements.txt\n"
                f".env/bin/python3 .buildkite/pipeline.py generate > {GENERATED}"
            ),
            "plugins": [{plugin: {"plan": GENERATED}}],
        },
        {
            "label": ":camera_with_flash: snapshot leave",
            "key": LEAVE,
            **common,
            "concurrency_group": group,
            "depends_on": [{"step": "snapshot-enter", "allow_failure": True}],
            "command": "buildkite-agent meta-data exists snapshot-mode",
            "plugins": [{plugin: {"leave": True}}],
        },
    ]


@dataclasses.dataclass
class Run:
    """One job of a step: its outcome, and what each part of it printed."""

    step: str
    outcome: str
    output: str


@dataclasses.dataclass
class Build:
    project: str
    number: int
    id: str
    url: str
    branch: str
    commit: str
    env: dict
    checkout: Path
    parent: Build | None = None
    steps: list = dataclasses.field(default_factory=list)
    outcomes: dict = dataclasses.field(default_factory=dict)
    runs: list = dataclasses.field(default_factory=list)
    children: list = dataclasses.field(default_factory=list)
    seen_uploads: int = 0

    @property
    def state(self) -> str:
        return "failed" if "hard_failed" in self.outcomes.values() else "passed"

    def step(self, key: str) -> dict:
        (found,) = [s for s in self.steps if s.get("key") == key]
        return found

    def report(self) -> str:
        """Every job of the build and of the builds it triggered, for a failing assertion."""
        lines = [f"== {self.project} build {self.number}: {self.state} {self.outcomes}"]
        for run in self.runs:
            lines.append(f"-- {run.step}: {run.outcome}\n{run.output}")
        return "\n".join([*lines, *(c.report() for c in self.children)])


def plugin_of(step: dict) -> tuple[str, dict] | None:
    """The ref and the configuration of the artifacts plugin in a step, if it has one."""
    for entry in step.get("plugins", []):
        for name, options in entry.items():
            if name.split("#", 1)[0] == PLUGIN:
                return name.split("#", 1)[1], options
    return None


def dependencies(step: dict) -> list[tuple[str, bool]]:
    """``(key, allow_failure)`` of each step ``step`` waits for."""
    found = step.get("depends_on") or []
    found = [found] if isinstance(found, str) else found
    return [
        (d, False) if isinstance(d, str) else (d["step"], bool(d.get("allow_failure")))
        for d in found
    ]


class Buildkite:
    """Plays builds with the real hooks, as the agents of one organisation would.

    ``template`` is a job of the suite's harness (``tests/conftest.py``), whose
    environment holds the store and the host's variables; ``hook`` runs a hook
    as the agent does. The steps of enter and resume run the plugin at
    ``gate_plugin``, whose .venv post-checkout made with PyYAML; every other
    step, leave too, the plugin at ``plugin_root``, whose .venv has none, as
    on an agent. ``compiled`` is the file every build step's command appends
    to.
    """

    def __init__(self, root, template, hook, gate_plugin, plugin_root, hosting, api, agent):
        self.root = root
        self.template = template
        self.hook = hook
        self.gate_plugin = gate_plugin
        self.plugin_commit = git(gate_plugin, "rev-parse", "HEAD")
        self.plugin_root = plugin_root
        self.hosting = hosting
        self.api = api
        self.agent = agent
        self.compiled = root / "compiled"
        self.breaks = root / "breaks"
        self.breaks.mkdir(parents=True)
        self.builds: list[Build] = []
        self.ids = itertools.count(1)
        self.numbers: dict[str, itertools.count] = {}
        self.env = self.base_environment()

    def base_environment(self) -> dict:
        kept = {
            k: v
            for k, v in self.template.env.items()
            if not k.startswith(("BUILDKITE", "FAKE_")) and k != "PATH"
        }
        path = os.pathsep.join([str(self.agent.bin), str(self.hosting.bin), os.environ["PATH"]])
        return {
            **kept,
            **self.hosting.environment(),
            **self.api.environment(),
            **self.agent.environment(),
            "PATH": path,
            "ARTIFACTS_BUILDKITE_TOKEN": TOKEN,
            "CHAIN_COMPILED": str(self.compiled),
            "CHAIN_BREAKS": str(self.breaks),
        }

    def add_pipeline(self, project: str, steps: list, more: dict | None = None) -> str:
        """A repository and a Buildkite pipeline of ``project``; the commit on master.

        ``steps`` as ``pipeline_files`` takes them; ``more`` maps further paths to texts.
        """
        commit = self.hosting.commit(project, {**pipeline_files(project, steps), **(more or {})})
        self.api.add(project)
        return commit

    def compiled_steps(self) -> list[str]:
        return self.compiled.read_text().splitlines() if self.compiled.exists() else []

    def builds_of(self, project: str) -> list[Build]:
        return [b for b in self.builds if b.project == project]

    # -- starting a build -------------------------------------------------

    def start(self, project, branch="master", commit=None, env=None, parent=None) -> Build:
        """A new build of ``project``, its checkout made and the runner's gate uploaded."""
        number = next(self.numbers.setdefault(project, itertools.count(1)))
        n = next(self.ids)
        build = Build(
            project=project,
            number=number,
            id=f"01900000-0000-7000-8000-{n:012x}",
            url=f"https://buildkite.example.test/{ORGANISATION}/{project}/builds/{number}",
            branch=branch,
            commit=commit or self.hosting.head(project, branch),
            env=dict(env or {}),
            checkout=self.root / "builds" / f"{project}-{number}",
            parent=parent,
        )
        build.checkout.parent.mkdir(parents=True, exist_ok=True)
        self.hosting.checkout(project, build.commit, build.checkout)
        self.agent.set_meta_data(build.id, "snapshot-arrived", str(int(time.time())))
        build.steps = gate_steps(project, build.commit, self.plugin_commit)
        self.builds.append(build)
        return build

    def build(self, project, **start) -> Build:
        """A build of ``project`` played to its end."""
        return self.play(self.start(project, **start))

    # -- running steps ----------------------------------------------------

    def play(self, build: Build) -> Build:
        """Run the steps of ``build`` until none is left that can run."""
        while True:
            step = self.next_step(build)
            if step is None:
                return build
            ready = self.readiness(build, step)
            if ready == "broken":
                build.outcomes[step["key"]] = "broken"
            else:
                self.run_step(build, step)

    def pending(self, build: Build) -> list[dict]:
        return [s for s in build.steps if s["key"] not in build.outcomes]

    def readiness(self, build: Build, step: dict) -> str:
        """``ready``, ``wait`` for a dependency that has not ended, or ``broken`` for one that failed."""
        for key, allowed in dependencies(step):
            outcome = build.outcomes.get(key)
            if outcome is None:
                return "wait"
            failed = outcome in ("hard_failed", "broken")
            if failed and not (allowed or step.get("allow_dependency_failure")):
                return "broken"
        return "ready"

    def next_step(self, build: Build) -> dict | None:
        """The first pending step that can be decided, leave only once every other step ended.

        Buildkite makes leave, which depends on enter, wait for every step
        that enter uploaded (experiments 30 and 35); here it waits for all.
        """
        pending = self.pending(build)
        others = [s for s in pending if s["key"] != LEAVE]
        for s in others:
            if self.readiness(build, s) != "wait":
                return s
        if others:
            raise AssertionError(f"{build.project} {build.number}: steps wait forever: {others}")
        return pending[0] if pending else None

    def run_step(self, build: Build, step: dict) -> Run:
        run = self.trigger(build, step) if "trigger" in step else self.job(build, step)
        build.outcomes[step["key"]] = run.outcome
        build.runs.append(run)
        self.agent.set_outcome(build.id, step["key"], run.outcome)
        return run

    def retry(self, build: Build, key: str) -> Run:
        """Retry the job of step ``key`` after its build ended."""
        return self.run_step(build, build.step(key))

    def trigger(self, build: Build, step: dict) -> Run:
        asked = step["build"]
        child = self.start(
            step["trigger"],
            branch=asked["branch"],
            commit=asked["commit"],
            env=asked.get("env", {}),
            parent=build,
        )
        build.children.append(child)
        self.play(child)
        if child.state == "passed":
            outcome = "passed"
        else:
            outcome = "soft_failed" if step.get("soft_fail") else "hard_failed"
        return Run(step["key"], outcome, f"triggered {child.project} build {child.number}")

    def job_environment(self, build: Build, step: dict) -> dict:
        n = next(self.ids)
        return {
            **self.env,
            "BUILDKITE": "true",
            "BUILDKITE_ORGANIZATION_SLUG": ORGANISATION,
            "BUILDKITE_PIPELINE_SLUG": build.project,
            "BUILDKITE_REPO": Hosting.url(build.project),
            "BUILDKITE_BUILD_ID": build.id,
            "BUILDKITE_BUILD_NUMBER": str(build.number),
            "BUILDKITE_BUILD_URL": build.url,
            "BUILDKITE_BRANCH": build.branch,
            "BUILDKITE_COMMIT": build.commit,
            "BUILDKITE_BUILD_CHECKOUT_PATH": str(build.checkout),
            "BUILDKITE_JOB_ID": f"01900000-0000-7000-9000-{n:012x}",
            "BUILDKITE_STEP_KEY": step["key"],
            "BUILDKITE_LABEL": step.get("label", ""),
            "BUILDKITE_COMMAND": step.get("command", ""),
            **build.env,
        }

    def job(self, build: Build, step: dict) -> Run:
        """A command step as the agent runs it: a clean checkout, pre-command, the command,
        post-command."""
        git(build.checkout, "clean", "-ffxdq", env=self.hosting.env)
        job = dataclasses.replace(
            self.template, workspace=build.checkout, env=self.job_environment(build, step)
        )
        plugin = plugin_of(step)
        output = []
        root = None
        if plugin is not None:
            ref, options = plugin
            if ref != self.plugin_commit:
                raise AssertionError(f"{build.project} {step['key']} runs the plugin at {ref}")
            root = self.gate_plugin if READS_DOCUMENTS & set(options) else self.plugin_root
            job = job.with_plugin(options)
            pre = self.hook("pre-command", job, root=root)
            output.append(pre.output)
            if pre.code != 0:
                return self.ended(step, 1, output)
        status = self.command(build, step, job, output)
        if root is not None:
            post = self.hook(
                "post-command", job.with_env(BUILDKITE_COMMAND_EXIT_STATUS=str(status)), root=root
            )
            output.append(post.output)
            status = status or post.code
        self.add_uploads(build, step)
        return self.ended(step, status, output)

    def command(self, build: Build, step: dict, job, output: list) -> int:
        """The step's command. The gate's enter and resume print the generated pipeline.

        Their command makes a virtualenv and runs the runner's ``generate``;
        here the runner's stand-in prints the pipeline in the test's own
        interpreter, which has PyYAML.
        """
        plugin = plugin_of(step)
        options = plugin[1] if plugin else {}
        if "plan" in options or "resume" in options:
            done = subprocess.run(
                [sys.executable, ".buildkite/pipeline.py", "generate"],
                cwd=build.checkout,
                env={**job.env, "PYTHONPATH": str(RUNNER)},
                capture_output=True,
                text=True,
                check=False,
            )
            (build.checkout / GENERATED).write_text(done.stdout)
        else:
            done = subprocess.run(
                [shutil.which("bash"), "-c", step["command"]],
                cwd=build.checkout,
                env=job.env,
                capture_output=True,
                text=True,
                check=False,
            )
        output.append(f"--- command exit {done.returncode}\n{done.stdout}{done.stderr}")
        return done.returncode

    def ended(self, step: dict, status: int, output: list) -> Run:
        if status == 0:
            outcome = "passed"
        else:
            outcome = "soft_failed" if step.get("soft_fail") else "hard_failed"
        return Run(step["key"], outcome, "\n".join(output))

    def add_uploads(self, build: Build, step: dict) -> None:
        """The steps uploaded by a job, placed after the step that uploaded them."""
        uploaded = self.agent.uploads(build.id)
        fresh, build.seen_uploads = uploaded[build.seen_uploads :], len(uploaded)
        added = [s for text in fresh for s in yaml.safe_load(text)["steps"]]
        for s in added:
            if "group" in s or "wait" in s or "key" not in s:
                raise AssertionError(f"the stand-in plays keyed command and trigger steps: {s}")
        at = build.steps.index(step) + 1
        build.steps[at:at] = added
