from __future__ import annotations

import http.server
import json
import socket
import threading
import time

import pytest

from lib.snapshot.buildkite import (
    BuildkiteError,
    PipelineFacts,
    PipelineNotFound,
    fetch_pipeline,
    https_repository,
    pipeline_facts,
    retry_pause,
    runs_pipeline_py,
)

TOKEN = "bkua_test-token-not-real"
ORG = "example-org"
REST_URL = "https://github.com/bureau14/qdb-api-rest.git"

# The first step of the generated pipelines, in the two forms they have.
GENERATED = """\
steps:
  - label: ":pipeline: Generate pipeline"
    priority: 100
    command: |
      set -euo pipefail
      python3 -m venv .env
      .env/bin/python3 -m pip install -q -r .buildkite/requirements.txt
      .env/bin/python3 .buildkite/pipeline.py | buildkite-agent pipeline upload
"""
GENERATED_QUOTED_KEYS = """\
"steps":
- "command": |
    python3 -m venv .env
    .env/bin/python3 -m pip install -q -r .buildkite/requirements.txt
    .env/bin/python3 .buildkite/pipeline.py | buildkite-agent pipeline upload
  "label": ":pipeline: Generate pipeline"
"""
OTHER_GENERATOR = """\
steps:
  - command: |
      python3 -m venv .env
      .env/bin/python3 .buildkite/docker-builder/pipeline.py | buildkite-agent pipeline upload
"""
STATIC = """\
# Enter your pipeline's YAML step configuration:
steps:
  - label: ":pipeline:"
    command: "buildkite-agent pipeline upload .buildkite/pipeline.yml"
"""
SHELL = """\
steps:
  - command: |
      if [ -f .buildkite/validation/pipeline.sh ]; then
        bash .buildkite/validation/pipeline.sh
      else
        buildkite-agent pipeline upload .buildkite/validation/pipeline.yml
      fi
"""


def answer(repository=REST_URL, configuration=GENERATED):
    """An answer in the shape the API gives (field names as measured)."""
    return {
        "id": "01890000-0000-0000-0000-000000000000",
        "slug": "qdb-api-rest",
        "name": "qdb-api-rest",
        "repository": repository,
        "configuration": configuration,
        "steps": [{"type": "script", "name": ":pipeline: Generate pipeline", "command": "x"}],
        "default_branch": "master",
        "env": None,
        "provider": {"id": "github"},
    }


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repository, url",
    [
        ("https://github.com/bureau14/quasardb.git", "https://github.com/bureau14/quasardb.git"),
        ("https://github.com/bureau14/quasardb", "https://github.com/bureau14/quasardb.git"),
        ("https://github.com/bureau14/quasardb/", "https://github.com/bureau14/quasardb.git"),
        (
            "git@github.com:bureau14/qdb-documentation.git",
            "https://github.com/bureau14/qdb-documentation.git",
        ),
        ("github.com:bureau14/qdb-duck", "https://github.com/bureau14/qdb-duck.git"),
        ("ssh://git@github.com/bureau14/qdb-duck.git", "https://github.com/bureau14/qdb-duck.git"),
        ("ssh://git@github.com:22/bureau14/qdb-duck", "https://github.com/bureau14/qdb-duck.git"),
        ("git://github.com/bureau14/qdb-duck.git", "https://github.com/bureau14/qdb-duck.git"),
        ("http://GitHub.com/bureau14/qdb-duck.git", "https://github.com/bureau14/qdb-duck.git"),
        ("https://x:pw@github.com/bureau14/qdb-duck", "https://github.com/bureau14/qdb-duck.git"),
        ("https://git.example:8443/a/b/c", "https://git.example:8443/a/b/c.git"),
        ("  https://github.com/bureau14/quasardb \n", "https://github.com/bureau14/quasardb.git"),
    ],
)
def test_https_repository(repository, url):
    assert https_repository(repository) == url


@pytest.mark.parametrize(
    "repository",
    ["", "quasardb", "/srv/git/quasardb.git", "file:///srv/git/q.git", "https://github.com/"],
)
def test_https_repository_refuses_what_is_not_a_repository_url(repository):
    with pytest.raises(BuildkiteError, match="cannot be read as a repository URL"):
        https_repository(repository)


def is_short_ascii_line(message: str) -> bool:
    return len(message) < 300 and all(" " <= c <= "~" for c in message)


def test_a_refused_repository_is_shown_short_on_one_line_of_ascii():
    with pytest.raises(BuildkiteError) as e:
        https_repository("file:///" + "x" * 100_000 + "\n" + chr(0xE9))
    assert is_short_ascii_line(str(e.value)), str(e.value)[:300]


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------


RUN = "python3 .buildkite/pipeline.py | buildkite-agent pipeline upload"


@pytest.mark.parametrize(
    "configuration, runs",
    [
        (GENERATED, True),
        (GENERATED_QUOTED_KEYS, True),
        (GENERATED.replace("\n", "\r\n"), True),
        (f"steps:\n  - command: .env/bin/{RUN}\n", True),
        (f'steps:\n  - commands:\n    - "{RUN.replace(" ", chr(92) + "t", 1)}"\n', True),
        (f"steps:\n  - commands:\n    - {RUN.replace('| ', '|   ')}\n", True),
        (OTHER_GENERATOR, False),
        (STATIC, False),
        (SHELL, False),
        ("steps:\n  - command: .buildkite/pipeline.sh\n", False),
        (GENERATED.replace("pipeline.py |", "pipeline.py generate |"), False),
        (GENERATED.replace(" .buildkite/pipeline.py", " ./.buildkite/pipeline.py"), False),
        (GENERATED.replace("pipeline upload", "pipeline upload --replace"), False),
        (GENERATED.replace("pipeline upload", "pipeline upload  # the gate"), False),
        (f'steps:\n  - command: "{RUN}"\n', True),
        (f"- command: {RUN}\n", True),
        (f"steps:\n  - commands: [set -e, '{RUN}']\n", True),
        (f"steps:\n  - command: |\n      # {RUN}\n      echo x\n", False),
        (f"# {RUN}\nsteps: []\n", False),
        ("", False),
        (None, False),
    ],
)
def test_runs_pipeline_py(configuration, runs):
    assert runs_pipeline_py(configuration) is runs


@pytest.mark.parametrize(
    "configuration",
    [
        f"steps:\n  - command: echo first\n  - command: {RUN}\n",
        f"steps:\n  - wait\n  - command: {RUN}\n",
        f"steps:\n  - trigger: other\n  - command: {RUN}\n",
        f"steps:\n  - group: g\n    steps:\n      - command: {RUN}\n",
        f"env:\n  X: {RUN}\nsteps:\n  - command: echo first\n",
    ],
    ids=["a second step", "after a wait", "after a trigger", "inside a group", "in the env"],
)
def test_only_the_first_step_counts(configuration):
    assert runs_pipeline_py(configuration) is False


# ---------------------------------------------------------------------------
# The answer and the pauses
# ---------------------------------------------------------------------------


def test_pipeline_facts():
    facts = pipeline_facts("qdb-api-rest", answer())
    assert facts == PipelineFacts("qdb-api-rest", REST_URL, True)
    assert not pipeline_facts("x", answer(configuration=None)).runs_pipeline_py
    assert not pipeline_facts("x", answer(configuration="")).runs_pipeline_py


@pytest.mark.parametrize(
    "value, message",
    [
        ([], "not a JSON object"),
        ({"configuration": GENERATED}, "has no repository"),
        ({"repository": "", "configuration": GENERATED}, "has no repository"),
        ({"repository": "https://github.com/a/b"}, "pipeline x: the answer has no configuration"),
        ({"repository": "https://github.com/a/b", "configuration": {"steps": []}}, "not text"),
        ({"repository": "https://github.com/a/b", "configuration": "steps: [\n"}, "not YAML"),
        ({"repository": "https://github.com/a/b", "configuration": "a: 2001-13-45\n"}, "not YAML"),
        (
            {"repository": "https://github.com/a/b", "configuration": "[" * 5000 + "]" * 5000},
            "not YAML",
        ),
    ],
)
def test_pipeline_facts_of_an_unexpected_answer_is_an_error(value, message):
    with pytest.raises(BuildkiteError, match=message):
        pipeline_facts("x", value)


@pytest.mark.parametrize(
    "failed, headers, pause",
    [
        (1, {}, 1.0),
        (2, {}, 2.0),
        (4, {}, 8.0),
        (1, {"ratelimit-reset": "41"}, 41.0),
        (3, {"ratelimit-reset": "2"}, 4.0),
        (1, {"ratelimit-reset": "3", "ratelimit-user-reset": "9"}, 9.0),
        (1, {"retry-after": "5"}, 5.0),
        (1, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"}, 1.0),
        (1, {"ratelimit-reset": "600"}, 60.0),
    ],
)
def test_retry_pause(failed, headers, pause):
    assert retry_pause(failed, headers) == pause


# ---------------------------------------------------------------------------
# The call, against a local HTTP server
# ---------------------------------------------------------------------------


class Scripted(http.server.BaseHTTPRequestHandler):
    """Answers each GET with the next scripted ``(status, headers, body, delay)``."""

    def do_GET(self):
        self.server.requests.append((self.path, self.headers.get("Authorization")))
        status, headers, body, delay = self.server.answers.pop(0)
        time.sleep(delay)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Api:
    def __init__(self) -> None:
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Scripted)
        self.server.requests = []
        self.server.answers = []
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.sleeps: list[float] = []

    @property
    def requests(self):
        return self.server.requests

    def answer(self, status, body=b"", headers=None, delay=0.0):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.server.answers.append((status, headers or {}, data, delay))
        return self

    def fetch(self, slug="qdb-api-rest", token=TOKEN, timeout=5.0):
        return fetch_pipeline(
            slug, ORG, token, api=self.url, sleep=self.sleeps.append, timeout=timeout
        )


@pytest.fixture
def api():
    a = Api()
    # A short poll interval: shutdown() waits for the next poll.
    thread = threading.Thread(target=a.server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield a
    a.server.shutdown()
    a.server.server_close()


def test_a_generated_pipeline(api):
    api.answer(200, answer())
    assert api.fetch() == PipelineFacts("qdb-api-rest", REST_URL, True)
    assert api.requests == [
        ("/v2/organizations/example-org/pipelines/qdb-api-rest", f"Bearer {TOKEN}")
    ]
    assert api.sleeps == []


def test_a_static_pipeline_and_an_ssh_repository(api):
    api.answer(200, answer("git@github.com:bureau14/qdb-cicd-tools.git", STATIC))
    facts = api.fetch("qdb-cicd-tools")
    assert facts == PipelineFacts(
        "qdb-cicd-tools", "https://github.com/bureau14/qdb-cicd-tools.git", False
    )


def test_the_slug_is_quoted_into_the_path(api):
    api.answer(200, answer())
    api.fetch("a/b c")
    assert api.requests[0][0] == "/v2/organizations/example-org/pipelines/a%2Fb%20c"


def test_429_and_5xx_are_tried_again(api):
    api.answer(429, {"message": "slow down"}, {"RateLimit-Reset": "7"})
    api.answer(502, b"<html>bad gateway</html>")
    api.answer(200, answer())
    assert api.fetch().runs_pipeline_py
    assert api.sleeps == [7.0, 2.0]
    assert len(api.requests) == 3


def test_five_failed_attempts_are_an_error(api):
    for _ in range(5):
        api.answer(503, {"message": "maintenance"})
    last = "5 attempts failed, the last with answer 503: maintenance"
    with pytest.raises(BuildkiteError, match=last) as raised:
        api.fetch()
    assert api.sleeps == [1.0, 2.0, 4.0, 8.0]
    assert len(api.requests) == 5
    assert TOKEN not in str(raised.value)


def test_404_names_the_project_and_is_not_tried_again(api):
    api.answer(404, {"message": "No pipeline found"})
    named = "project qdb-duck: .* no pipeline qdb-duck in organisation example-org .*404.*No"
    with pytest.raises(PipelineNotFound, match=named):
        api.fetch("qdb-duck")
    assert len(api.requests) == 1 and api.sleeps == []


@pytest.mark.parametrize("status", [400, 401, 403, 422])
def test_other_answers_are_errors_and_not_tried_again(api, status):
    api.answer(status, {"message": "nope"})
    with pytest.raises(BuildkiteError, match=f"Buildkite answered {status}: nope") as raised:
        api.fetch()
    assert not isinstance(raised.value, PipelineNotFound)
    assert len(api.requests) == 1
    assert TOKEN not in str(raised.value)


def test_buildkites_explanation_is_shown_short_on_one_line_of_ascii(api):
    api.answer(422, {"message": "forged\n" * 10_000 + chr(0xE9)})
    with pytest.raises(BuildkiteError) as e:
        api.fetch()
    assert is_short_ascii_line(str(e.value)), str(e.value)[:300]


def test_a_redirect_is_not_followed(api):
    api.answer(301, b"", {"Location": f"{api.url}/elsewhere"})
    with pytest.raises(BuildkiteError, match="answered 301"):
        api.fetch()
    assert len(api.requests) == 1


@pytest.mark.parametrize(
    "body, message",
    [(b"not json", "not JSON"), (b"[]", "not a JSON object"), (b"{}", "has no repository")],
)
def test_an_answer_that_is_not_a_pipeline_is_an_error(api, body, message):
    api.answer(200, body)
    with pytest.raises(BuildkiteError, match=message):
        api.fetch()


def test_a_timeout_is_tried_again(api):
    api.answer(200, answer(), delay=1.0)
    api.answer(200, answer())
    assert api.fetch(timeout=0.2).runs_pipeline_py
    assert api.sleeps == [1.0]


def test_no_answer_at_all_is_tried_again_then_an_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sleeps = []
    with pytest.raises(BuildkiteError, match="5 attempts failed, the last with no answer"):
        fetch_pipeline("x", ORG, TOKEN, api=f"http://127.0.0.1:{port}", sleep=sleeps.append)
    assert sleeps == [1.0, 2.0, 4.0, 8.0]


def test_an_empty_token_is_refused_before_any_call(api):
    with pytest.raises(BuildkiteError, match="no Buildkite API token"):
        api.fetch(token="")
    assert api.requests == []
