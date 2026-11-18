"""Repeat a call with backoff, so that a store or a network that fails for a
while does not fail the build."""

import time

from .errors import ArtifactsError
from .log import log

MAX_RETRIES = 10
BACKOFF_BASE = 5  # seconds; doubles each retry
BACKOFF_CAP = 60  # 1 minute max sleep between retries


def _with_retry(fn, description):
    """Execute fn() with exponential backoff on transient errors.

    Retries up to MAX_RETRIES times. Sleep between attempts starts at BACKOFF_BASE
    seconds and doubles each time, capped at BACKOFF_CAP.

    Backoff schedule (defaults): 5 -> 10 -> 20 -> 40 -> 60 -> 60 -> ... seconds.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        # An ArtifactsError explains a failure that another attempt would repeat.
        except (KeyboardInterrupt, SystemExit, ArtifactsError):
            raise
        except Exception as exc:
            if attempt == MAX_RETRIES:
                log(f"  FAILED after {MAX_RETRIES} attempts: {description}")
                raise
            sleep = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
            log(f"  attempt {attempt}/{MAX_RETRIES} failed for {description}: {exc}")
            log(f"  retrying in {sleep}s...")
            time.sleep(sleep)
