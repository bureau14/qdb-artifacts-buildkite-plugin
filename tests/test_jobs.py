"""What a step of a pipeline relies on when the plugin works, through the real hooks as an
agent runs them, against the object store of ``world.py``: a download gets the build it names
among builds that compete for it, LATEST_SUCCESSFUL moves, storage stays what the plugin's
master writes, pointers are written once, the library gives what the hooks give, and a chain
node passes its gate.
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
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml
import zstandard
from chain import MATRICES, PLUGIN, SHAPED, build_step, entry, generated
from world import (
    BUCKET,
    GENERATED,
    MASTER,
    STEP_KINDS,
    build_id,
    build_url,
    paired,
    store_variables,
)

from lib.pointer import Build, Pointer, decode, encode
from lib.snapshot.plan import Node, Plan, RootInfo, encode_plan

FEATURE = "refs/heads/feature"
TAG = "refs/tags/v1.0.0"
REFS = (TAG, FEATURE, MASTER)
BACKENDS = ("s3", "r2")
KEY = "v1-" + "5" * 32


def upload_and_promote(variant="linux", ref=MASTER, names=None, **upload) -> dict:
    """The options of a build step that uploads and promotes ``variant`` of ``ref``, under the
    project and build that ``names`` gives (the job's own when None); an option given as None
    is left out."""
    upload = {k: v for k, v in upload.items() if v is not None}
    snapshot = {"snapshot": upload["snapshot"]} if "snapshot" in upload else {}
    named = names or {}
    return {
        "upload": {"variant": variant, "git_ref": ref, **named, **upload},
        "promote": {"variant": variant, "git_ref": ref, **named, **snapshot},
    }


def case_id(value) -> str:
    return "+".join(map(case_id, value)) if isinstance(value, tuple) else str(value)


def master_wrote(ref, variant, build, files, promoted, project="widget", under="") -> dict:
    """The objects the plugin's master writes for a build step of ``project`` in ``build`` that
    uploads ``files`` (paths below its base_dir) as ``variant`` of ``ref``, and promotes them when
    ``promoted``: the files and the manifest under the build's prefix, and LATEST_SUCCESSFUL."""
    variant_at = f"{under}{project}/{ref}/variants/{variant}"
    at = f"{variant_at}/builds/{build}"
    manifest = {
        "file_count": len(files),
        "files": sorted(files),
        "uploaded_at": "2026-11-19T00:00:00+00:00",
        "total_size_bytes": sum(map(len, files.values())),
        "base_dir": "dist",
    }
    objects = {f"{at}/{rel}": data for rel, data in files.items()}
    objects[f"{at}/manifest.json"] = json.dumps(manifest).encode()
    return {**objects, **({f"{variant_at}/LATEST_SUCCESSFUL": build.encode()} if promoted else {})}


# ---------------------------------------------------------------------------
# What travels: payloads, builds that compete, how a step names one
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


def zstd(data: bytes) -> bytes:
    return zstandard.ZstdCompressor().compress(data)


def made_by(stamp: str, *names: str) -> dict:
    """Files of ``names`` whose bytes say which build made them."""
    return {n: f"{n} of {stamp}\n".encode() for n in names}


MEMBERS = ("bin/tool", "lib/libw.so", "share/notes.txt")
DIST = {"files": "dist/**/*", "base_dir": "dist"}


@dataclasses.dataclass(frozen=True)
class Payload:
    """What the upload of a build step stores (by path below dist/), the upload's options, what
    its exclude leaves out of dist/, the download options of it, and the files that arrive under
    the output directory. ``stored`` and ``arrives`` take the stamp of the build that made the
    files, which their bytes carry."""

    name: str
    stored: Callable[[str], dict]
    download: dict
    arrives: Callable[[str], dict]
    upload: dict = dataclasses.field(default_factory=lambda: DIST)
    left_out: dict = dataclasses.field(default_factory=dict)

    def __str__(self) -> str:
        return self.name

    def made(self, stamp: str) -> dict:
        """What the build step leaves in its checkout."""
        return {
            f"dist/{rel}": data for rel, data in {**self.stored(stamp), **self.left_out}.items()
        }


PAYLOADS = [
    Payload(
        "plain",
        lambda s: made_by(s, "a.bin", "sub/b.txt", "sub/c.tmp"),
        {"files": ["*"], "exclude": ["*.tmp"]},
        lambda s: made_by(s, "a.bin", "sub/b.txt"),
        {**DIST, "exclude": ["*.log"]},
        {"debug.log": b"log"},
    ),
    Payload(
        "tar.gz",
        lambda s: {"w.tar.gz": gzip.compress(tar(made_by(s, *MEMBERS)))},
        {"files": ["*.tar.gz"], "extract": True},
        lambda s: made_by(s, *MEMBERS),
    ),
    Payload(
        "tar.zst",
        lambda s: {"w.tar.zst": zstd(tar(made_by(s, *MEMBERS)))},
        {"files": ["*.tar.zst"], "extract": True},
        lambda s: made_by(s, *MEMBERS),
    ),
    Payload(
        "zip-and-jar",
        lambda s: {
            "w.zip": zipped(made_by(s, "bin/tool")),
            "w.jar": zipped(made_by(s, "META-INF/a")),
        },
        {"files": ["*.zip", "*.jar"], "extract": True},
        lambda s: made_by(s, "bin/tool", "META-INF/a"),
    ),
    Payload(
        "entry-filters",
        lambda s: {"w.tar.zst": zstd(tar(made_by(s, *MEMBERS)))},
        {"files": ["*.tar.zst!bin/*", "*.tar.zst!lib/*"], "extract": True},
        lambda s: {Path(n).name: d for n, d in made_by(s, "bin/tool", "lib/libw.so").items()},
    ),
]

# Builds of widget, numbered in the order they ran; the build under test is SUBJECT.
EARLIER, NEWER, SUBJECT, LATER = 1, 2, 3, 4
NEXT = "refs/heads/next"
NEW_BRANCH = "refs/heads/new"  # a branch that has no build yet


def stamp(number: int, ref: str, variant: str) -> str:
    return f"build {number} of {ref} {variant}"


def master_made(payload: Payload, number: int, ref: str, variant: str, promoted: bool) -> dict:
    """The objects of widget's build ``number`` of ``payload``, as the plugin's master writes."""
    files = payload.stored(stamp(number, ref, variant))
    return master_wrote(ref, variant, build_id(number), files, promoted)


def history(payload: Payload, ref: str) -> dict:
    """The builds of widget before the build under test: on its ref and on master, for linux
    and macos, an earlier build that was promoted and a newer one that was not."""
    return {
        k: body
        for r in dict.fromkeys((ref, MASTER))
        for v in ("linux", "macos")
        for number, promoted in ((EARLIER, True), (NEWER, False))
        for k, body in master_made(payload, number, r, v, promoted).items()
    }


@dataclasses.dataclass(frozen=True)
class Naming:
    """How a downloading step names the build of widget that a build step of a ref uploaded and
    promoted: its pipeline, whether it is a job of that build (else of build 9 on ``own_ref``),
    its entry for that ref and build, and whether a later build of the ref was promoted before
    it downloads, so that LATEST_SUCCESSFUL names another build than the one it names."""

    name: str
    pipeline: str
    same_build: bool
    entry: Callable[[str, str], dict]
    later_promoted: bool
    own_ref: str = MASTER

    def __str__(self) -> str:
        return self.name


def by_id(ref: str, build: str, **more) -> dict:
    return {"project_id": "widget", "build_id": build, "git_ref": ref, **more}


NAMINGS = [
    Naming("own-build", "widget", True, lambda ref, build: {"git_ref": ref}, True),
    Naming("build-id", "app", False, by_id, True),
    Naming(
        "build-id-strictly",
        "app",
        False,
        lambda ref, build: by_id(ref, build, require_strict_ref_match=True),
        True,
    ),
    Naming(
        "latest-of-the-project",
        "app",
        False,
        lambda ref, build: {"project_id": "widget", "git_ref": ref},
        False,
    ),
    Naming(
        "latest-of-another-ref", "widget", False, lambda ref, build: {"git_ref": ref}, False, NEXT
    ),
]


@dataclasses.dataclass(frozen=True)
class Transfer:
    """Options of both transfers and of the plugin, and what an earlier attempt of the
    downloading job left in its output directory, which ``clean`` removes."""

    name: str
    upload: dict
    download: dict
    plugin: dict = dataclasses.field(default_factory=dict)
    left_over: dict = dataclasses.field(default_factory=dict)

    def __str__(self) -> str:
        return self.name

    @property
    def annotates(self) -> bool:
        return self.upload.get("annotate", True)


TRANSFERS = [
    Transfer("defaults", {}, {}),
    Transfer(
        "retried-one-at-a-time-clean-unannotated",
        {"parallel": 1, "concurrency": 1, "annotate": False},
        {"parallel": 1, "concurrency": 1, "clean": True},
        left_over={"in/stale.bin": b"from an earlier attempt"},
    ),
    Transfer(
        "wide-debug",
        {"parallel": 8, "concurrency": 2},
        {"parallel": 8, "concurrency": 2},
        {"debug": True},
    ),
]

# Who uploads the build under test: a job of it, or a job of another pipeline that names
# widget and the build in its upload and promote options.
PRODUCERS = {
    "its-own-job": ("widget", SUBJECT, None),
    "named-in-the-options": (
        "widget-release",
        70,
        {"project_id": "widget", "build_id": build_id(SUBJECT)},
    ),
}


@pytest.mark.parametrize(
    "payload, naming, transfer, ref, producer, backend",
    paired(PAYLOADS, NAMINGS, TRANSFERS, REFS, sorted(PRODUCERS), BACKENDS),
    ids=case_id,
)
def test_a_download_gets_the_bytes_of_the_build_it_names(
    jobs, store, agent, payload, naming, transfer, ref, producer
):
    """Among builds of other refs and variants, earlier and later, promoted and not, a step that
    names a build that a build step uploaded and promoted (as its own build, by its id, or as the
    latest successful of its ref) gets that build's bytes, at the paths the options name and
    nothing else there; a download from a branch without builds gets master's latest successful;
    the upload annotates its build unless told not to."""
    pipeline, number, names = PRODUCERS[producer]
    made = stamp(SUBJECT, ref, "linux")
    store.put_all(history(payload, ref))
    maker = jobs(pipeline, number, ref)
    maker.write(payload.made(made))
    options = upload_and_promote("linux", ref, names, **payload.upload, **transfer.upload)

    built = maker.with_plugin({**options, **transfer.plugin}).run("post-command")
    store.put_all(master_made(payload, LATER, ref, "linux", naming.later_promoted))
    consumer = (
        jobs(naming.pipeline, SUBJECT, ref)
        if naming.same_build
        else jobs(naming.pipeline, 9, naming.own_ref)
    )
    consumer.write(transfer.left_over)
    entries = [
        {"variant": "linux", "output-dir": "in", **naming.entry(ref, build_id(SUBJECT))},
        {
            "project_id": "widget",
            "variant": "macos",
            "git_ref": NEW_BRANCH,
            "output-dir": "in-macos",
        },
    ]
    projects = [{**e, **payload.download} for e in entries]
    download = {"download": {"projects": projects, **transfer.download}, **transfer.plugin}
    fetched = consumer.with_plugin(download).run("pre-command")

    annotations = agent.files(maker.env["BUILDKITE_BUILD_ID"], "annotations")
    assert (built.code, fetched.code) == (0, 0), built.output + fetched.output
    assert consumer.files("in") == payload.arrives(made)
    assert consumer.files("in-macos") == payload.arrives(stamp(EARLIER, MASTER, "macos"))
    assert bool(annotations) == transfer.annotates


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


@pytest.mark.parametrize("snapshot", [None, KEY])
@pytest.mark.parametrize("prefix, ref", paired(["", "ci/artifacts"], REFS), ids=case_id)
def test_storage_is_additive(jobs, store, prefix, ref, snapshot):
    """Without a snapshot key, upload and promote on a ref add exactly the objects, manifest
    fields and pointer body the plugin's master writes, and leave what master wrote on the other
    refs as it was; with a key they add objects under _snapshot/v1/ and manifest fields only."""
    under = f"{prefix}/" if prefix else ""
    files = {"a.bin": b"a", "sub/b.bin": b"bb"}
    others = {
        k: body
        for r in REFS
        if r != ref
        for k, body in master_wrote(r, "linux", build_id(7), files, True, under=under).items()
    }
    store.put_all(others)
    job = jobs("widget", 1, ref, destination=f"s3://{BUCKET}/{prefix}".rstrip("/"))
    job.write({f"dist/{rel}": data for rel, data in files.items()})
    options = upload_and_promote(ref=ref, files="dist/**/*", base_dir="dist", snapshot=snapshot)

    outcome = job.with_plugin(options).run("post-command")

    masters = master_wrote(ref, "linux", build_id(1), files, True, under=under)
    (manifest_key,) = [k for k in masters if k.endswith("/manifest.json")]
    expected = {**{k: b for k, b in masters.items() if k != manifest_key}, **others}
    objects = store.objects()
    added = objects.keys() - masters.keys() - others.keys()
    manifest = json.loads(objects[manifest_key])
    assert outcome.code == 0, outcome.output
    assert {k: objects.get(k) for k in expected} == expected
    assert bool(added) == bool(snapshot) and all(
        k.startswith(f"{under}_snapshot/v1/") for k in added
    )
    assert set(manifest) - MANIFEST == ({"source", "downloads"} if snapshot else set())
    assert (manifest["files"], manifest["file_count"]) == (sorted(files), 2)


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
    the same files from the same build as the download hook, among builds that compete."""
    store.put_all(history(payload, MASTER))
    producer, consumer = jobs("widget", SUBJECT), jobs("app", 9)
    producer.write(payload.made(stamp(SUBJECT, MASTER, "linux")))
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
        "exclude_patterns": payload.download.get("exclude"),
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
    assert json.loads(caller.stdout)["build_id"] == build_id(SUBJECT)
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
