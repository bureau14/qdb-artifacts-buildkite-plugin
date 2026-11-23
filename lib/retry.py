"""Repeat a call with backoff, so that a store or a network that fails for a
while does not fail the build."""

import time

from .errors import ArtifactsError
from .log import log

MAX_RETRIES = 10
BACKOFF_BASE = 5  # seconds; doubles each retry
BACKOFF_CAP = 60  # 1 minute max sleep between retries


def with_retry(fn, description, attempts=MAX_RETRIES):
    """What fn() returns, tried up to ``attempts`` times with a pause that doubles."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        # An ArtifactsError explains a failure that another attempt would repeat.
        except (KeyboardInterrupt, SystemExit, ArtifactsError):
            raise
        except Exception as exc:
            if attempt == attempts:
                log(f"  FAILED after {attempts} attempts: {description}")
                raise
            sleep = min(BACKOFF_BASE * (2 ** (attempt - 1)), BACKOFF_CAP)
            log(f"  attempt {attempt}/{attempts} failed for {description}: {exc}")
            log(f"  retrying in {sleep}s...")
            time.sleep(sleep)
