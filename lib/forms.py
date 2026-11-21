"""The forms of a ref and of a time, as the jobs and the plan step write them.

This is ordinary job code, as the manifest is: nothing here imports ``snapshot``.
"""

from __future__ import annotations

import datetime

BRANCH_PREFIX = "refs/heads/"
TAG_PREFIX = "refs/tags/"


def branch_ref(branch: str) -> str:
    """The full ref of ``branch``."""
    return BRANCH_PREFIX + branch


def tag_ref(tag: str) -> str:
    """The full ref of ``tag``."""
    return TAG_PREFIX + tag


def is_tag(ref: str) -> bool:
    """Whether the full ref ``ref`` is a tag's."""
    return ref.startswith(TAG_PREFIX)


def branch_of(ref: str) -> str | None:
    """The branch a full ref names; None for a tag."""
    return ref[len(BRANCH_PREFIX) :] if ref.startswith(BRANCH_PREFIX) else None


def name_of(ref: str) -> str:
    """The name of a full ref of a branch or a tag, as Buildkite names a build of it: the ref
    without ``refs/heads/`` or ``refs/tags/``."""
    for prefix in (BRANCH_PREFIX, TAG_PREFIX):
        if ref.startswith(prefix):
            return ref[len(prefix) :]
    return ref


def iso_utc(epoch: float) -> str:
    """``epoch`` seconds as UTC ISO 8601 with a "Z", to the millisecond."""
    t = datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"
