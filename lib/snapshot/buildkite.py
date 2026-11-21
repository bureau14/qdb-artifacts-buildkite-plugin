"""What Buildkite's REST API says about a pipeline (specification sections 3 and 10).

One call per pipeline of the closure, in the root's enter step only:
``GET <api>/v2/organizations/<organisation>/pipelines/<slug>`` with the token as
a bearer (scope ``read_pipelines``). Of the answer two facts are kept
(``PipelineFacts``): the pipeline's repository as an https URL
(``https_repository``), and whether its configuration runs
``.buildkite/pipeline.py`` into a pipeline upload (``runs_pipeline_py``).

Answers 429 and 5xx, and failures to reach the API at all, are tried again by
the library's ``retry.with_retry``, five attempts in all; a 404 names the
project; any other answer is an error. An error is never read as an answer,
such as "cannot give a snapshot".

The standard library only. The token and the organisation are parameters;
where the token comes from is the caller's business. No message contains the
token, and a redirect is never followed, so the token goes to the API's host
only.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..errors import ArtifactsError
from ..retry import with_retry

API = "https://api.buildkite.com"
ATTEMPTS = 5
TIMEOUT = 30.0


class PipelineNotFound(ArtifactsError):
    """The API answered 404: no pipeline of that slug that the token can see."""


@dataclass(frozen=True)
class PipelineFacts:
    """What the plan keeps of a pipeline's settings."""

    repository: str
    runs_pipeline_py: bool


# ---------------------------------------------------------------------------
# The repository and the configuration
# ---------------------------------------------------------------------------


def without_user(repository: str) -> str:
    """A repository URL without its user part, which may hold a token or a password. The scp
    form (git@host:org/repo) has no place for a password."""
    url = urllib.parse.urlsplit(repository)
    if not url.scheme or "@" not in url.netloc:
        return repository
    return urllib.parse.urlunsplit(url._replace(netloc=url.netloc.rpartition("@")[2]))


def https_repository(repository: str) -> str:
    """``repository`` as an https URL, without the user part a URL may hold a secret in.

    A URL is kept as it is apart from that; the ``[user@]host:path`` form of
    ssh becomes ``https://host/path``.
    """
    if "://" in repository:
        return without_user(repository)
    host, _, path = repository.partition(":")
    return f"https://{host.rpartition('@')[2]}/{path}"


# How the configuration of every generated pipeline runs its generator.
GENERATOR_LINE = ".buildkite/pipeline.py | buildkite-agent pipeline upload"


def runs_pipeline_py(configuration: str | None) -> bool:
    """True when the pipeline's configuration runs ``.buildkite/pipeline.py`` into an upload:
    it holds ``GENERATOR_LINE``."""
    return GENERATOR_LINE in (configuration or "")


def pipeline_facts(answer: Mapping[str, Any]) -> PipelineFacts:
    """The facts of a pipeline from the parsed JSON of the API's answer."""
    return PipelineFacts(
        https_repository(answer["repository"]), runs_pipeline_py(answer["configuration"])
    )


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


def _response(status: int, headers: Any, body: bytes) -> Response:
    names = {k.lower(): v for k, v in (headers.items() if headers else [])}
    return Response(status, names, body)


def http_get(url: str, token: str, timeout: float = TIMEOUT) -> Response:
    """One GET of ``url`` with the token as a bearer; any HTTP status is returned.

    Raises ``OSError`` or ``http.client.HTTPException`` when no answer came,
    and ``ArtifactsError`` when the token cannot be sent in a header: the HTTP
    client refuses such a header with a message that holds its value.
    """
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as r:
            return _response(r.status, r.headers, r.read())
    except urllib.error.HTTPError as e:
        return _response(e.code, e.headers, e.read())
    except ValueError:
        raise ArtifactsError("the token cannot be sent in an HTTP header") from None


def pipeline_url(api: str, organisation: str, slug: str) -> str:
    quoted = [urllib.parse.quote(part, safe="") for part in (organisation, slug)]
    return f"{api.rstrip('/')}/v2/organizations/{quoted[0]}/pipelines/{quoted[1]}"


def answer_of(url: str, token: str, timeout: float) -> Response:
    """One GET of ``url`` that Buildkite answered.

    429 and 5xx raise ``RuntimeError``, which ``with_retry`` tries again, as it
    does no answer at all (``OSError``, ``http.client.HTTPException``).
    """
    r = http_get(url, token, timeout)
    if r.status == 429 or r.status >= 500:
        raise RuntimeError(f"Buildkite answered {r.status}")
    return r


def fetch_pipeline(
    slug: str, organisation: str, token: str, *, api: str = API, timeout: float = TIMEOUT
) -> PipelineFacts:
    """Ask Buildkite for pipeline ``slug`` of ``organisation`` and return its facts.

    Up to ``ATTEMPTS`` calls (``retry.with_retry``): an answer 429 or 5xx, or
    no answer at all, is followed by a pause and another call. Raises
    ``PipelineNotFound`` for a 404 and ``ArtifactsError`` for any other answer
    that is not the pipeline.
    """
    url = pipeline_url(api, organisation, slug)
    r = with_retry(lambda: answer_of(url, token, timeout), f"pipeline {slug}", ATTEMPTS)
    if r.status == 200:
        return pipeline_facts(json.loads(r.body))
    if r.status == 404:
        raise PipelineNotFound(
            f"project {slug}: Buildkite has no pipeline {slug} in organisation "
            f"{organisation} that the token can see (404)"
        )
    raise ArtifactsError(f"pipeline {slug}: Buildkite answered {r.status}")
