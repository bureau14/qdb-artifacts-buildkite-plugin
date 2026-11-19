"""What a step of a pipeline relies on when the plugin works, through the real hooks as an
agent runs them, against the object store of ``world.py``: files travel, LATEST_SUCCESSFUL
moves, storage stays readable by the plugin's master, pointers are written once, the
library gives what the hooks give, and a chain node passes its gate.
"""

from __future__ import annotations

import dataclasses
import gzip
import io
import json
import platform
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

import pytest
import yaml
import zstandard
from chain import MATRICES, PLUGIN, SHAPED, build_step, entry, generated
from world import BUCKET, GENERATED, STEP_KINDS, build_id, build_url, paired, store_variables

from lib.pointer import Build, Pointer, decode, encode
from lib.snapshot.plan import Node, Plan, RootInfo, encode_plan

MASTER = "refs/heads/master"
BACKENDS = ("s3", "r2")
KEY = "v1-" + "5" * 32


def upload_and_promote(variant="linux", **upload) -> dict:
    """The options of a build step that uploads and promotes ``variant`` of master; an
    option given as None is left out."""
    upload = {k: v for k, v in upload.items() if v is not None}
    snapshot = {"snapshot": upload["snapshot"]} if "snapshot" in upload else {}
    return {
        "upload": {"variant": variant, "git_ref": MASTER, **upload},
        "promote": {"variant": variant, "git_ref": MASTER, **snapshot},
    }


def case_id(value) -> str:
    return "+".join(map(case_id, value)) if isinstance(value, tuple) else str(value)


# ---------------------------------------------------------------------------
# What travels: payloads, how a step names the build, transfer settings
# ---------------------------------------------------------------------------


def tar(members: dict) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def zipped(members: dict) -> bytes:
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return raw.getvalue()


MEMBERS = {"bin/tool": b"tool\x00\x01", "lib/libw.so": b"\x7fELF lib", "share/notes.txt": b"notes"}


@dataclasses.dataclass(frozen=True)
class Payload:
    """What a build step leaves in its checkout, the upload and download options of it,
    and the files that must arrive under the downloading step's output directory."""

    name: str
    workspace: dict
    upload: dict
    download: dict
    arrives: dict

    def __str__(self) -> str:
        return self.name


DIST = {"files": "dist/*", "base_dir": "dist"}
PAYLOADS = [
    Payload(
        "plain",
        {"out/a.bin": b"\x00a" * 700, "out/sub/b.txt": b"b", "out/debug.log": b"log"},
        {"files": "out/**/*", "base_dir": "out", "exclude": ["*.log"]},
        {"files": ["*"]},
        {"a.bin": b"\x00a" * 700, "sub/b.txt": b"b"},
    ),
    Payload(
        "tar.gz",
        {"dist/w.tar.gz": gzip.compress(tar(MEMBERS))},
        DIST,
        {"files": ["*.tar.gz"], "extract": True},
        MEMBERS,
    ),
    Payload(
        "tar.zst",
        {"dist/w.tar.zst": zstandard.ZstdCompressor().compress(tar(MEMBERS))},
        DIST,
        {"files": ["*.tar.zst"], "extract": True},
        MEMBERS,
    ),
    Payload(
        "zip-and-jar",
        {"dist/w.zip": zipped({"bin/tool": b"tool"}), "dist/w.jar": zipped({"META-INF/a": b"jar"})},
        DIST,
        {"files": ["*.zip", "*.jar"], "extract": True},
        {"bin/tool": b"tool", "META-INF/a": b"jar"},
    ),
    Payload(
        "entry-filters",
        {"dist/w.tar.zst": zstandard.ZstdCompressor().compress(tar(MEMBERS))},
        DIST,
        {"files": ["*.tar.zst!bin/*", "*.tar.zst!lib/*"], "extract": True},
        {"tool": MEMBERS["bin/tool"], "libw.so": MEMBERS["lib/libw.so"]},
    ),
]


@dataclasses.dataclass(frozen=True)
class Source:
    """How a downloading step names the build: its pipeline and branch, whether it is a job
    of the uploading build, and the options of its download entry (``{build}`` is the
    uploading build's id)."""

    name: str
    pipeline: str
    branch: str
    same_build: bool
    entry: dict

    def __str__(self) -> str:
        return self.name

    def options(self) -> dict:
        return {k: v.replace("{build}", build_id(1)) for k, v in self.entry.items()}


SOURCES = [
    Source("same-build", "widget", "master", True, {"git_ref": MASTER}),
    Source(
        "named-build",
        "app",
        "master",
        False,
        {"project_id": "widget", "build_id": "{build}", "git_ref": MASTER},
    ),
    Source("other-project", "app", "master", False, {"project_id": "widget", "git_ref": MASTER}),
    Source("other-ref", "widget", "feature", False, {"git_ref": MASTER}),
    Source(
        "latest-with-fallback",
        "app",
        "feature",
        False,
        {"project_id": "widget", "build_id": "LATEST_SUCCESSFUL", "git_ref": "refs/heads/feature"},
    ),
]


@dataclasses.dataclass(frozen=True)
class Transfer:
    """Options of both transfers, and what an earlier attempt of the downloading job left in
    its checkout, which ``clean`` removes."""

    name: str
    upload: dict
    download: dict
    left_over: dict = dataclasses.field(default_factory=dict)

    def __str__(self) -> str:
        return self.name


TRANSFERS = [
    Transfer("defaults", {}, {}),
    Transfer(
        "retried-one-at-a-time-clean",
        {"parallel": 1, "concurrency": 1, "annotate": False},
        {"parallel": 1, "concurrency": 1, "clean": True},
        {"in/stale.bin": b"from an earlier attempt"},
    ),
    Transfer("wide", {"parallel": 8, "concurrency": 2}, {"parallel": 8, "concurrency": 2}),
]


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize("payload, source, transfer", paired(PAYLOADS, SOURCES, TRANSFERS), ids=str)
def test_a_later_step_downloads_the_bytes_a_build_step_uploaded(jobs, payload, source, transfer):
    """Files a build step uploads and promotes arrive with the same bytes, at the paths the
    options name and nothing else there, in a step that downloads them by its own build, a
    build id, the project, another ref or LATEST_SUCCESSFUL, on either store."""
    producer = jobs("widget", 1)
    producer.write(payload.workspace)
    consumer = jobs(source.pipeline, 1 if source.same_build else 2, source.branch)
    consumer.write(transfer.left_over)
    project = {"variant": "linux", "output-dir": "in", **source.options(), **payload.download}

    built = producer.with_plugin(upload_and_promote(**payload.upload, **transfer.upload)).run(
        "post-command"
    )
    fetched = consumer.with_plugin({"download": {"projects": [project], **transfer.download}}).run(
        "pre-command"
    )

    assert (built.code, fetched.code) == (0, 0), built.output + fetched.output
    assert consumer.files("in") == payload.arrives


@pytest.mark.parametrize("exit_status", [0, 1])
@pytest.mark.parametrize("has_files", [True, False])
@pytest.mark.parametrize("promotes", [True, False])
def test_post_command_promotes_only_a_passed_command_with_files(
    jobs, store, exit_status, has_files, promotes
):
    """post-command uploads the step's files whatever its command did, and moves
    LATEST_SUCCESSFUL only for a step that promotes, after a passed command and an upload
    that was not empty."""
    job = jobs()
    if has_files:
        job.write({"out/a.bin": b"a"})
    both = upload_and_promote(files="out/*", allow_empty=True)
    options = both if promotes else {"upload": both["upload"]}

    outcome = (
        job.with_env(BUILDKITE_COMMAND_EXIT_STATUS=str(exit_status))
        .with_plugin(options)
        .run("post-command")
    )

    variant = f"widget/{MASTER}/variants/linux"
    assert outcome.code == 0, outcome.output
    assert (f"{variant}/builds/{build_id(1)}/manifest.json" in store.stored()) == has_files
    moved = promotes and has_files and exit_status == 0
    assert (f"{variant}/LATEST_SUCCESSFUL" in store.stored()) == moved


# The fields every manifest has had since before snapshots.
MANIFEST = {"file_count", "files", "uploaded_at", "total_size_bytes", "base_dir"}


@pytest.mark.parametrize("prefix", ["", "ci/artifacts"])
@pytest.mark.parametrize("snapshot", [None, KEY])
def test_storage_is_additive(jobs, store, prefix, snapshot):
    """Without a snapshot key, upload and promote write exactly the objects, manifest fields
    and pointer body the plugin's master writes; with a key they add objects under
    _snapshot/v1/ and manifest fields, and change none of those."""
    job = jobs(destination=f"s3://{BUCKET}/{prefix}".rstrip("/"))
    job.write({"out/a.bin": b"a", "out/sub/b.bin": b"bb"})
    options = upload_and_promote(files="out/**/*", base_dir="out", snapshot=snapshot)

    outcome = job.with_plugin(options).run("post-command")

    under = f"{prefix}/" if prefix else ""
    variant = f"{under}widget/{MASTER}/variants/linux"
    build = f"{variant}/builds/{build_id(1)}"
    masters = {
        f"{build}/a.bin",
        f"{build}/sub/b.bin",
        f"{build}/manifest.json",
        f"{variant}/LATEST_SUCCESSFUL",
    }
    added = store.stored() - masters
    manifest = json.loads(store.get(f"{build}/manifest.json"))
    assert outcome.code == 0, outcome.output
    assert masters <= store.stored()
    assert bool(added) == bool(snapshot) and all(
        k.startswith(f"{under}_snapshot/v1/") for k in added
    )
    assert set(manifest) - MANIFEST == ({"source", "downloads"} if snapshot else set())
    assert (manifest["files"], manifest["file_count"]) == (["a.bin", "sub/b.bin"], 2)
    assert store.get(f"{variant}/LATEST_SUCCESSFUL") == build_id(1).encode()


ENGINE_BUILD = "01900000-0000-7000-8000-0000000000e7"
PROMOTERS = [(("linux",), "the same build retried"), (("linux", "macos"), "another build")]


@pytest.mark.parametrize("promoters, backend", paired(PROMOTERS, BACKENDS), ids=case_id)
def test_a_snapshot_promote_writes_one_pointer_per_variant_once(jobs, store, promoters):
    """A promote with a snapshot key writes one pointer per variant that names the build, the
    ref and the commit it came from; a second promote of the key leaves it as it was; the
    upload's manifest says where its files were made and which builds the job downloaded."""
    variants, second = promoters
    again = 1 if second == "the same build retried" else 2
    downloads = {
        v: {"project_id": "engine", "variant": v, "git_ref": MASTER, "build_id": ENGINE_BUILD}
        for v in variants
    }

    def promote(number, v):
        job = jobs("widget", number, step=f"build-{v}")
        job.write({f"out/{v}.bin": v.encode()})
        options = {
            **upload_and_promote(v, files="out/*", snapshot=KEY),
            "download": {"projects": [downloads[v]]},
        }
        return job, job.with_plugin(options).run("post-command")

    first = {v: promote(1, v) for v in variants}
    pointers = {v: store.get(f"_snapshot/v1/pointers/widget/{KEY}/{v}.json") for v in variants}
    manifests = {
        v: json.loads(store.get(f"widget/{MASTER}/variants/{v}/builds/{build_id(1)}/manifest.json"))
        for v in variants
    }
    later = [promote(again, v)[1] for v in variants]

    own = Build(build_id(1), 1, build_url("widget", 1))
    assert all(o.code == 0 for o in [*(o for _, o in first.values()), *later])
    assert store.stored(f"_snapshot/v1/pointers/widget/{KEY}/") == {
        f"_snapshot/v1/pointers/widget/{KEY}/{v}.json" for v in variants
    }
    for v, (job, _) in first.items():
        p = decode(pointers[v])
        source = {k: manifests[v]["source"][k] for k in ("snapshot", "commit", "build_id")}
        assert p == Pointer("widget", KEY, v, MASTER, job.commit, own, p.written_at)
        assert store.get(f"_snapshot/v1/pointers/widget/{KEY}/{v}.json") == pointers[v]
        assert source == {"snapshot": KEY, "commit": job.commit, "build_id": build_id(1)}
        assert manifests[v]["downloads"] == [downloads[v]]


@pytest.mark.parametrize("payload, backend", paired(PAYLOADS, BACKENDS), ids=str)
def test_the_library_downloads_what_the_hooks_download(
    jobs, store, plugin, tmp_path, payload, backend
):
    """A program outside Buildkite that vendors the library, as the README shows, downloads
    the same files from the same build as the download hook."""
    producer, consumer = jobs("widget", 1), jobs("app", 2)
    producer.write(payload.workspace)
    request = {
        "project_id": "widget",
        "build_id": "LATEST_SUCCESSFUL",
        "git_ref": MASTER,
        "variant": "linux",
    }
    project = {**request, "output-dir": "in", **payload.download}
    tree = tmp_path / "caller"
    (tree / "vendor").mkdir(parents=True)
    (tree / "vendor" / "qdb_artifacts").symlink_to(plugin.root, target_is_directory=True)

    built = producer.with_plugin(upload_and_promote(**payload.upload)).run("post-command")
    fetched = consumer.with_plugin({"download": {"projects": [project]}}).run("pre-command")
    arguments = {
        **request,
        "patterns": payload.download["files"],
        "extract": payload.download.get("extract", False),
    }
    caller = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            Path(__file__).parent / "outside_caller.py",
            tree,
            json.dumps(arguments),
            tmp_path / "library",
        ],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **store_variables(store, backend)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert (built.code, fetched.code, caller.returncode) == (0, 0, 0), (
        built.output + fetched.output + caller.stderr
    )
    assert json.loads(caller.stdout)["build_id"] == build_id(1)
    files = {
        p.relative_to(tmp_path / "library").as_posix(): p.read_bytes()
        for p in (tmp_path / "library").rglob("*")
        if p.is_file()
    }
    assert files == consumer.files("in")


@pytest.mark.parametrize("step_kind", sorted(STEP_KINDS))
def test_post_checkout_makes_the_venv_of_the_step(plugin, step_kind):
    """post-checkout makes, with the Python the agent names, the venv the step's options call
    for, in which the plugin's job side imports, and its planning half only for a step that
    plans."""
    venv = plugin.root / (".venv-snapshot" if step_kind == "plan" else ".venv") / "bin" / "python"

    def imports(module):
        script = f"import platform, {module}; print(platform.python_version())"
        run = [venv, "-c", script]
        return subprocess.run(run, cwd=plugin.root, capture_output=True, text=True, check=False)

    job_side, planning = imports("lib.cli"), imports("lib.snapshot.enter")
    assert plugin.prepared[step_kind].code == 0, plugin.prepared[step_kind].output
    assert job_side.stdout.strip() == platform.python_version(), job_side.stderr
    assert (planning.returncode == 0) == (step_kind == "plan")


# ---------------------------------------------------------------------------
# A chain node's gate through the real hooks
# ---------------------------------------------------------------------------

ROOT = 9
KEYS = {"app": "v1-" + "1" * 32, "widget": "v1-" + "2" * 32, "engine": "v1-" + "3" * 32}
ENGINE_COMMIT = "e" * 40


def chain_plan(widget_commit: str) -> str:
    """The plan of a root build of app: app takes widget, widget takes engine."""
    nodes = {
        "app": ("a" * 40, ("widget",)),
        "widget": (widget_commit, ("engine",)),
        "engine": (ENGINE_COMMIT, ()),
    }
    return encode_plan(
        Plan(
            id=build_id(ROOT),
            context="master",
            rebuild="none",
            created_at="2026-11-19T00:00:00.000Z",
            root=RootInfo("app", ROOT, build_url("app", ROOT)),
            nodes={
                p: Node(
                    f"https://git.example.test/example/{p}.git", MASTER, c, KEYS[p], "master", deps
                )
                for p, (c, deps) in nodes.items()
            },
        )
    )


def engine_pointer(variant: str) -> bytes:
    build = Build(ENGINE_BUILD, 7, build_url("engine", 7))
    return encode(
        Pointer(
            "engine",
            KEYS["engine"],
            variant,
            MASTER,
            ENGINE_COMMIT,
            build,
            "2026-11-19T00:00:00.000Z",
        )
    )


def artifacts_options(step: dict) -> dict:
    ((options,),) = [
        [o for name, o in p.items() if name.startswith(PLUGIN)] for p in step["plugins"]
    ]
    return options


@pytest.mark.parametrize(
    "matrix, backend", paired(sorted(MATRICES.values()), BACKENDS), ids=case_id
)
def test_a_chain_node_passes_its_gate_through_the_real_hooks(jobs, agent, store, matrix):
    """Through the real post-command hook, a chain node's plan step triggers the dependency it
    lacks, its resume step uploads the pipeline with that dependency's build in every download,
    its build steps write the node's pointers, and leave records no failure."""
    steps = [build_step("widget", v, [entry("engine", v)]) for v in matrix]
    template = yaml.safe_dump({"steps": steps})
    job = jobs("widget", 1).committed(
        {".buildkite/steps.yml": template, ".buildkite/pipeline.py": SHAPED}
    )
    job.write(
        {GENERATED: generated(template, MASTER), **{f"out/{v}.bin": v.encode() for v in matrix}}
    )
    agent.set(build_id(ROOT), "meta-data", "snapshot-plan", chain_plan(job.commit))
    agent.set(build_id(1), "meta-data", "snapshot-arrived", str(int(time.time())))
    gate = job.with_env(SNAPSHOT_PLAN=build_id(ROOT), SNAPSHOT_NEEDS=json.dumps(sorted(matrix)))

    planned = (
        gate.with_env(BUILDKITE_STEP_KEY="snapshot-enter")
        .with_plugin({"plan": GENERATED})
        .run("post-command")
    )
    for v in matrix:
        store.put(f"_snapshot/v1/pointers/engine/{KEYS['engine']}/{v}.json", engine_pointer(v))
    resumed = (
        gate.with_env(BUILDKITE_STEP_KEY="snapshot-resume")
        .with_plugin({"resume": GENERATED})
        .run("post-command")
    )
    triggers, pipeline = [yaml.safe_load(u)["steps"] for u in agent.files(build_id(1), "uploads")]
    built = []
    for step in pipeline:
        built.append(
            job.with_env(BUILDKITE_STEP_KEY=step["key"])
            .with_plugin(artifacts_options(step))
            .run("post-command")
        )
        agent.set(
            build_id(1), "outcomes", step["key"], "passed" if built[-1].code == 0 else "hard_failed"
        )
    left = (
        gate.with_env(BUILDKITE_STEP_KEY="snapshot-leave")
        .with_plugin({"leave": True})
        .run("post-command")
    )

    downloads = [e for s in pipeline for e in artifacts_options(s)["download"]["projects"]]
    pointers = [
        decode(store.get(f"_snapshot/v1/pointers/widget/{KEYS['widget']}/{v}.json")) for v in matrix
    ]
    assert all(o.code == 0 for o in (planned, resumed, *built, left)), [
        o.output for o in (planned, resumed, *built, left)
    ]
    assert [s.get("trigger") for s in triggers if "trigger" in s] == ["engine"]
    assert {(e["project_id"], e["build_id"]) for e in downloads} == {("engine", ENGINE_BUILD)}
    assert all(p.build.id == build_id(1) and p.commit == job.commit for p in pointers)
    assert store.stored("_snapshot/v1/failures/") == set()
