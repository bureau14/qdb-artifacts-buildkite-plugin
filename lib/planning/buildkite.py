"""What Buildkite's REST API says about a pipeline: its repository, and whether its
configuration runs ``.buildkite/pipeline.py``.

One GET per pipeline, with the token as a bearer (scope ``read_pipelines``). An answer 429 or
5xx, and no answer at all, are tried again; a 404 names the project; an error is never read
as an answer. No message contains the token, and a redirect is never followed, so the token
goes to the API's host only.
"""

from __future__ import annotations

import json
import urllib.parse
from dataclasses import dataclass

import urllib3

from ..errors import ArtifactsError
from ..retry import with_retry

API = "https://api.buildkite.com"
ATTEMPTS = 5
TIMEOUT = 30.0

# How the configuration of every generated pipeline runs its generator.
GENERATOR_LINE = ".buildkite/pipeline.py | buildkite-agent pipeline upload"

HTTP = urllib3.PoolManager()


class PipelineNotFound(ArtifactsError):
    """The API answered 404: no pipeline of that slug that the token can see."""


@dataclass(frozen=True)
class PipelineFacts:
    repository: str
    runs_pipeline_py: bool


def https_repository(repository: str) -> str:
    """``repository`` as an https URL without a user part, which may hold a secret. The ssh
    form ``[user@]host:path`` becomes ``https://host/path``."""
    if "://" not in repository:
        host, _, path = repository.partition(":")
        return f"https://{host.rpartition('@')[2]}/{path}"
    url = urllib.parse.urlsplit(repository)
    return urllib.parse.urlunsplit(url._replace(netloc=url.netloc.rpartition("@")[2]))


def http_get(url: str, token: str) -> tuple[int, bytes]:
    """The status and body of one GET of ``url``, whatever the status."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        r = HTTP.request(
            "GET", url, headers=headers, redirect=False, retries=False, timeout=TIMEOUT
        )
    except ValueError:
        # The HTTP client refuses such a header with a message that holds its value.
        raise ArtifactsError("the token cannot be sent in an HTTP header") from None
    return r.status, r.data


def answer_of(url: str, token: str) -> tuple[int, bytes]:
    """``http_get``; 429 and 5xx raise, so that ``with_retry`` asks again."""
    status, body = http_get(url, token)
    if status == 429 or status >= 500:
        raise RuntimeError(f"Buildkite answered {status}")
    return status, body


def fetch_pipeline(slug: str, organisation: str, token: str) -> PipelineFacts:
    """The facts of pipeline ``slug`` of ``organisation``. Raises ``PipelineNotFound`` for a
    404 and ``ArtifactsError`` for any other answer that is not the pipeline."""
    quoted = [urllib.parse.quote(part, safe="") for part in (organisation, slug)]
    url = f"{API}/v2/organizations/{quoted[0]}/pipelines/{quoted[1]}"
    status, body = with_retry(lambda: answer_of(url, token), f"pipeline {slug}", ATTEMPTS)
    if status == 404:
        raise PipelineNotFound(
            f"project {slug}: Buildkite has no pipeline {slug} in organisation "
            f"{organisation} that the token can see (404)"
        )
    if status != 200:
        raise ArtifactsError(f"pipeline {slug}: Buildkite answered {status}")
    answer = json.loads(body)
    return PipelineFacts(
        https_repository(answer["repository"]),
        GENERATOR_LINE in (answer["configuration"] or ""),
    )
