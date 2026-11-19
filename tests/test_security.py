"""Two properties that are no happy path and must hold all the same: a downloaded archive
writes nothing outside its output directory, and the Buildkite API token shows nowhere."""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
import zstandard
from chain import GRAPHS
from fakes import url
from world import TOKEN

from lib.extract import extract_local_archive
from lib.snapshot import buildkite
from lib.snapshot.buildkite import Response
from lib.snapshot.enter import buildkite_pipelines

# ---------------------------------------------------------------------------
# Tar members that would leave the output directory
# ---------------------------------------------------------------------------

ESCAPES = ("parent-path", "symbolic-link", "hard-link", "absolute-path")
COMPRESSIONS = {".tar.gz": gzip.compress, ".tar.zst": zstandard.ZstdCompressor().compress}


def member(
    name: str, kind=tarfile.REGTYPE, target: str = ""
) -> tuple[tarfile.TarInfo, bytes | None]:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, target
    data = b"escaped" if kind == tarfile.REGTYPE else None
    info.size = len(data or b"")
    return info, data


def escaping(escape: str, outside: Path, prefix: str) -> list:
    """Members that would write into ``outside``, or next to the output directory."""
    if escape == "parent-path":
        return [member(prefix + "../escaped")]
    if escape == "symbolic-link":
        return [
            member(prefix + "link", tarfile.SYMTYPE, str(outside)),
            member(prefix + "link/escaped"),
        ]
    if escape == "hard-link":
        return [
            member(prefix + "hard", tarfile.LNKTYPE, str(outside / "secret")),
            member(prefix + "hard"),
        ]
    return [member(str(outside / "escaped"))]


def archived(members: list, suffix: str) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for info, data in members:
            archive.addfile(info, io.BytesIO(data) if data is not None else None)
    return COMPRESSIONS[suffix](raw.getvalue())


def everything_but(root: Path, out: Path) -> dict:
    """What lies under ``root`` outside ``out``: each path with its bytes or link target."""

    def content(p: Path):
        return os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None

    return {
        p.relative_to(root).as_posix(): content(p)
        for p in root.rglob("*")
        if p != out and out not in p.parents
    }


@pytest.mark.parametrize("suffix", sorted(COMPRESSIONS))
@pytest.mark.parametrize("escape", ESCAPES)
@pytest.mark.parametrize("rule", ["whole archive", "prefix stripped"])
def test_no_tar_member_is_written_outside_the_output_directory(tmp_path, suffix, escape, rule):
    """Extracting a downloaded tar archive whose member would land outside the output directory,
    through a parent path, a link or an absolute path, creates and changes nothing outside it,
    with or without an entry rule that strips a prefix."""
    out, outside = tmp_path / "out", tmp_path / "outside"
    out.mkdir()
    outside.mkdir()
    (outside / "secret").write_bytes(b"secret")
    prefix, rules = ("pkg/", [("pkg/*", "pkg/")]) if rule == "prefix stripped" else ("", None)
    archive = tmp_path / f"w{suffix}"
    archive.write_bytes(archived(escaping(escape, outside, prefix), suffix))
    before = everything_but(tmp_path, out)

    with contextlib.suppress(Exception):
        extract_local_archive(str(archive), archive.name, out, entry_rules=rules)

    assert everything_but(tmp_path, out) == before


# ---------------------------------------------------------------------------
# The Buildkite API token
# ---------------------------------------------------------------------------

RUNS_PIPELINE_PY = (
    "steps:\n  - command: python3 .buildkite/pipeline.py | buildkite-agent pipeline upload\n"
)


class TokenParameter:
    """The plan step's SSM client, which holds the token's parameter, written from a file."""

    def get_parameter(self, Name, WithDecryption):
        return {"Parameter": {"Value": TOKEN + "\n"}}


def api(answer: int, received: list):
    """Buildkite's API behind its transport: every pipeline's settings, and ``answer`` for
    ``core``. Each token it is sent goes into ``received``."""

    def http_get(address: str, token: str, timeout: float = 30.0) -> Response:
        received.append(token)
        slug = address.rsplit("/", 1)[-1]
        status = 200 if slug != "core" else answer
        body = (
            {"repository": url(slug), "configuration": RUNS_PIPELINE_PY}
            if status == 200
            else {"message": "no"}
        )
        return Response(status, {}, json.dumps(body).encode())

    return http_get


@pytest.mark.parametrize("token_source", ["variable", "ssm"])
@pytest.mark.parametrize("answer", [200, 404, 401])
def test_the_buildkite_token_never_shows(worlds, monkeypatch, capsys, token_source, answer):
    """The plan step of a root sends the Buildkite API token to the API and puts it in nothing
    the chain prints, logs, annotates, raises, sets as meta-data, uploads or stores, whatever
    the API answers and wherever the token was read. (Every job of ``world.py`` holds the token
    too, and its ``Job.run`` fails a hook that prints it.)"""
    received: list[str] = []
    monkeypatch.setenv("BUILDKITE_ORGANIZATION_SLUG", "example")
    if token_source == "variable":
        monkeypatch.setenv("ARTIFACTS_BUILDKITE_TOKEN", TOKEN)
    else:
        monkeypatch.delenv("ARTIFACTS_BUILDKITE_TOKEN", raising=False)
    monkeypatch.setattr(buildkite, "http_get", api(answer, received))
    world = worlds(GRAPHS["diamond"])
    world.pipelines = buildkite_pipelines(TokenParameter())

    top = world.build("app")

    printed = capsys.readouterr()
    kept = (world.agents.meta_data, world.agents.annotations, world.agents.uploads)
    shown = [
        printed.out,
        printed.err,
        *(line for b in world.builds for line in b.outputs),
        *map(json.dumps, kept),
    ]
    assert top.state == ("passed" if answer == 200 else "failed")
    assert received and set(received) == {TOKEN}
    assert [text for text in shown if TOKEN in text] == []
    assert [key for key, body in world.store.objects.items() if TOKEN.encode() in body] == []
