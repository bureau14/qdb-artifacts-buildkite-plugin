"""What Buildkite's REST API says about a pipeline (specification sections 3 and 10).

One call per pipeline of the closure, in the root's enter step only:
``GET <api>/v2/organizations/<organisation>/pipelines/<slug>`` with the token as
a bearer (scope ``read_pipelines``). Of the answer two facts are kept
(``PipelineFacts``): the pipeline's repository as an https URL
(``https_repository``), and whether the first step of its configuration runs
``.buildkite/pipeline.py`` (``runs_pipeline_py``).

Answers 429 and 5xx, and failures to reach the API at all, are tried again with
a growing pause, five attempts in all; a 404 names the project; any other
answer is an error, and so is an answer without ``repository`` or
``configuration``. An error is never read as an answer, such as "cannot give
a snapshot".

The standard library and PyYAML only. The token and the organisation are
parameters; where the token comes from is the caller's business. No message
contains the token, and a redirect is never followed, so the token goes to the
API's host only.
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

import yaml

from .shown import shown_text, shown_value

API = "https://api.buildkite.com"
ATTEMPTS = 5
TIMEOUT = 30.0
# The pause after the first failed attempt; it doubles after each further one.
BACKOFF = 1.0
# No pause is longer, whatever the server asks for.
BACKOFF_CAP = 60.0


class BuildkiteError(RuntimeError):
    """The API did not answer with the pipeline."""


class PipelineNotFound(BuildkiteError):
    """The API answered 404: no pipeline of that slug that the token can see."""


@dataclass(frozen=True)
class PipelineFacts:
    """What the plan keeps of a pipeline's settings."""

    slug: str
    repository: str
    runs_pipeline_py: bool


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------

_SCP_LIKE = re.compile(r"^(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>[^/].*)$")


def _host_and_path(repository: str) -> tuple[str, str] | None:
    """The host (with the port that https keeps) and the path of a repository URL."""
    if "://" not in repository:
        m = _SCP_LIKE.match(repository)
        return (m["host"], m["path"]) if m else None
    u = urllib.parse.urlsplit(repository)
    if u.scheme not in ("https", "http", "ssh", "git", "git+ssh") or not u.hostname:
        return None
    keeps_port = u.scheme in ("https", "http") and u.port is not None
    host = f"{u.hostname}:{u.port}" if keeps_port else u.hostname
    return host, u.path


def https_repository(repository: str) -> str:
    """``repository`` as ``https://<host>/<path>.git``.

    Accepts ``https``, ``http``, ``ssh``, ``git`` and ``git+ssh`` URLs and the
    ``[user@]host:path`` form of ssh. The user is dropped, the host lowercased,
    an ssh or git port dropped (it is not the https port), the path kept as it
    is apart from slashes at either end, and ``.git`` added when it is missing,
    so that every spelling of one repository gives one string. Raises
    ``BuildkiteError`` for anything else.
    """
    parts = _host_and_path(repository.strip())
    path = parts[1].strip("/") if parts else ""
    if not parts or not path or "//" in path:
        shown = shown_value(repository)
        raise BuildkiteError(f"repository {shown} cannot be read as a repository URL")
    host = parts[0].lower()
    return f"https://{host}/{path if path.endswith('.git') else path + '.git'}"


# ---------------------------------------------------------------------------
# The configuration
# ---------------------------------------------------------------------------

# How the first step of every generated pipeline ends its command line.
GENERATOR_LINE_END = (".buildkite/pipeline.py", "|", "buildkite-agent", "pipeline", "upload")


def first_step(configuration: str) -> Any:
    """The first step of a pipeline's configuration; None when it has none.

    The configuration is YAML: a mapping with ``steps``, or the list of steps
    itself. Text that is not YAML raises ``yaml.YAMLError``, or ``ValueError``
    or ``RecursionError`` from the parser, as for a template.
    """
    doc = yaml.safe_load(configuration)
    steps = doc.get("steps") if isinstance(doc, dict) else doc
    return steps[0] if isinstance(steps, list) and steps else None


def step_command(step: Any) -> str:
    """The command of a command step as one text (its ``command`` or ``commands``); else empty."""
    command = step.get("command", step.get("commands")) if isinstance(step, dict) else None
    if isinstance(command, list) and all(isinstance(c, str) for c in command):
        return "\n".join(command)
    return command if isinstance(command, str) else ""


def is_generator_line(line: str) -> bool:
    """True for a shell line whose words end with ``.buildkite/pipeline.py | ... upload``."""
    words = line.split()
    return bool(words) and not words[0].startswith("#") and tuple(words[-5:]) == GENERATOR_LINE_END


def runs_pipeline_py(configuration: str | None) -> bool:
    """True when the pipeline's first step runs ``.buildkite/pipeline.py`` into an upload.

    Only the first step of the configuration counts, and only when it is a
    command step. A line of its command counts when, split on whitespace, its
    words end with ``.buildkite/pipeline.py | buildkite-agent pipeline upload``
    and its first word does not start with ``#``. The word before the path
    (the interpreter) is not looked at; the line must end there, so
    ``pipeline.py generate | ...`` (which prints the plain pipeline), another
    generator file, ``upload <file>``, a static pipeline, and an empty or null
    configuration give False.
    """
    command = step_command(first_step(configuration)) if configuration else ""
    return any(is_generator_line(line) for line in command.splitlines())


# ---------------------------------------------------------------------------
# The answer
# ---------------------------------------------------------------------------


def pipeline_facts(slug: str, answer: Any) -> PipelineFacts:
    """The facts of pipeline ``slug`` from the parsed JSON of the API's answer."""
    if not isinstance(answer, dict):
        raise BuildkiteError(f"pipeline {slug}: the answer is not a JSON object")
    repository = answer.get("repository")
    if not isinstance(repository, str) or not repository:
        raise BuildkiteError(f"pipeline {slug}: the answer has no repository")
    if "configuration" not in answer:
        raise BuildkiteError(f"pipeline {slug}: the answer has no configuration")
    configuration = answer["configuration"]
    if configuration is not None and not isinstance(configuration, str):
        raise BuildkiteError(f"pipeline {slug}: the configuration is not text")
    try:
        runs = runs_pipeline_py(configuration)
    except (yaml.YAMLError, ValueError, RecursionError) as exc:
        raise BuildkiteError(f"pipeline {slug}: the configuration is not YAML") from exc
    return PipelineFacts(slug, https_repository(repository), runs)


# ---------------------------------------------------------------------------
# The call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Response:
    """An HTTP answer; header names lowercased."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        """Refuse every redirect: urllib would send the token to the new host too."""
        raise urllib.error.HTTPError(req.full_url, code, msg, headers, fp)


def _response(status: int, headers: Any, read: Callable[[], bytes]) -> Response:
    try:
        body = read()
    except (OSError, http.client.HTTPException):
        body = b""
    names = {k.lower(): v for k, v in (headers.items() if headers else [])}
    return Response(status, names, body)


def http_get(url: str, token: str, timeout: float = TIMEOUT) -> Response:
    """One GET of ``url`` with the token as a bearer; any HTTP status is returned.

    Raises ``OSError`` or ``http.client.HTTPException`` when no answer came.
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as r:
            return _response(r.status, r.headers, r.read)
    except urllib.error.HTTPError as e:
        return _response(e.code, e.headers, e.read)


def pipeline_url(api: str, organisation: str, slug: str) -> str:
    quoted = [urllib.parse.quote(part, safe="") for part in (organisation, slug)]
    return f"{api.rstrip('/')}/v2/organizations/{quoted[0]}/pipelines/{quoted[1]}"


def is_retried(status: int) -> bool:
    return status == 429 or status >= 500


def _seconds(headers: Mapping[str, str], name: str) -> float:
    value = headers.get(name, "").strip()
    return float(value) if value.isdigit() else 0.0


# Seconds until a rate limit resets: Buildkite sends one per limit scope
# (organisation and user); Retry-After is the standard form.
_WAIT_HEADERS = ("ratelimit-reset", "ratelimit-user-reset", "retry-after")


def retry_pause(failed_attempts: int, headers: Mapping[str, str]) -> float:
    """The pause after ``failed_attempts`` failures: 1, 2, 4, 8 s, or longer when asked.

    The longest wait any of ``_WAIT_HEADERS`` asks for wins over the backoff;
    no pause is longer than ``BACKOFF_CAP``.
    """
    backoff = BACKOFF * 2 ** (failed_attempts - 1)
    asked = max(_seconds(headers, name) for name in _WAIT_HEADERS)
    return min(max(backoff, asked), BACKOFF_CAP)


def _message(body: bytes) -> str:
    """Buildkite's own explanation in an error answer, if it gave one."""
    try:
        m = json.loads(body).get("message")
    except (ValueError, AttributeError):
        return ""
    return f": {shown_text(m)}" if isinstance(m, str) and m else ""


def _answer(slug: str, organisation: str, r: Response) -> PipelineFacts:
    if r.status == 200:
        try:
            return pipeline_facts(slug, json.loads(r.body))
        except ValueError as exc:
            raise BuildkiteError(f"pipeline {slug}: the answer is not JSON: {exc}") from exc
    if r.status == 404:
        raise PipelineNotFound(
            f"project {slug}: Buildkite has no pipeline {slug} in organisation "
            f"{organisation} that the token can see (404){_message(r.body)}"
        )
    raise BuildkiteError(f"pipeline {slug}: Buildkite answered {r.status}{_message(r.body)}")


def fetch_pipeline(
    slug: str,
    organisation: str,
    token: str,
    *,
    api: str = API,
    sleep: Callable[[float], None] = time.sleep,
    timeout: float = TIMEOUT,
) -> PipelineFacts:
    """Ask Buildkite for pipeline ``slug`` of ``organisation`` and return its facts.

    Up to ``ATTEMPTS`` calls: an answer 429 or 5xx, or no answer at all, is
    followed by ``sleep(retry_pause(...))`` and another call. Raises
    ``PipelineNotFound`` for a 404 and ``BuildkiteError`` for everything else
    that is not the pipeline.
    """
    if not token:
        raise BuildkiteError("no Buildkite API token")
    url = pipeline_url(api, organisation, slug)
    last = ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            r = http_get(url, token, timeout)
        except (OSError, http.client.HTTPException) as exc:
            last, headers = f"no answer ({type(exc).__name__}: {exc})", {}
        else:
            if not is_retried(r.status):
                return _answer(slug, organisation, r)
            last, headers = f"answer {r.status}{_message(r.body)}", r.headers
        if attempt < ATTEMPTS:
            sleep(retry_pause(attempt, headers))
    raise BuildkiteError(f"pipeline {slug}: {ATTEMPTS} attempts failed, the last with {last}")
