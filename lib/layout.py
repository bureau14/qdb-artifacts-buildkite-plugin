"""Every object key the plugin reads or writes.

    <prefix>/<project>/<key>/key.json                           what the key stands for
    <prefix>/<project>/<key>/<platform>/<step id>/<file>        what one step made for one platform
    <prefix>/<project>/<key>/<platform>/<step id>/manifest.json the files of that upload, and who
                                                                promoted them
    <prefix>/<project>/<key>/failures/<build id>.json           a build of this key failed, and why
    <prefix>/<project>/<ref>/<platform>/<step id>               the branch's latest file: a key

The destination names the bucket and the prefix. A branch's latest file lies under its full
ref, ``refs/heads/<branch>``, which no key starts with.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

KEY_JSON = "key.json"
MANIFEST = "manifest.json"
FAILURES = "failures"

# It starts with a letter because the agent reads YAML 1.2, in which a plain 1e3 or 0889 is a
# number.
STEP_ID = re.compile(r"[a-z][a-z0-9_]*")


def is_step_id(value: object) -> bool:
    return isinstance(value, str) and STEP_ID.fullmatch(value) is not None


@dataclass(frozen=True, order=True)
class Folder:
    """One folder of a key: what the step ``step`` made for ``platform``."""

    platform: str
    step: str

    def __str__(self) -> str:
        return f"{self.platform}/{self.step}"


def parse_destination(destination: str) -> tuple[str, str]:
    """(bucket, prefix) of `s3://bucket/prefix`; a bare name is a bucket."""
    if not destination.startswith("s3://"):
        return destination.strip("/"), ""
    parsed = urlparse(destination)
    return parsed.netloc, parsed.path.strip("/")


def key_join(*parts: str) -> str:
    """Join key segments with one slash; an empty segment is left out."""
    return "/".join(p.strip("/") for p in parts if p.strip("/"))


def key_json(prefix: str, project: str, key: str) -> str:
    return key_join(prefix, project, key, KEY_JSON)


def platform_prefix(prefix: str, project: str, key: str, platform: str) -> str:
    return key_join(prefix, project, key, platform) + "/"


def step_folder(prefix: str, project: str, key: str, folder: Folder) -> str:
    return key_join(prefix, project, key, folder.platform, folder.step)


def manifest(step_folder: str) -> str:
    return key_join(step_folder, MANIFEST)


def failure_records(prefix: str, project: str, key: str) -> str:
    return key_join(prefix, project, key, FAILURES) + "/"


def failure_record(prefix: str, project: str, key: str, build_id: str) -> str:
    return failure_records(prefix, project, key) + f"{build_id}.json"


def latest_file(prefix: str, project: str, ref: str, folder: Folder) -> str:
    return key_join(prefix, project, ref, folder.platform, folder.step)


def latest_files(prefix: str, project: str, ref: str, platform: str) -> str:
    """The prefix of the latest files of every step of ``platform`` on ``ref``."""
    return key_join(prefix, project, ref, platform) + "/"
