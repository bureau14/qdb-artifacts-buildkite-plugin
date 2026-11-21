"""The forms of a branch's ref and of a time, as the jobs and the plan step write them.

This is ordinary job code, as the manifest is: nothing here imports ``snapshot``.
"""

from __future__ import annotations

import datetime

BRANCH_PREFIX = "refs/heads/"
TAG_PREFIX = "refs/tags/"


def iso_utc(epoch: float) -> str:
    """``epoch`` seconds as UTC ISO 8601 with a "Z", to the millisecond."""
    t = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
