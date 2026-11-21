"""What a step of a pipeline relies on when the plugin works, through the real hooks as an
agent runs them, against the object store of ``world.py``: a download gets the steps its entry
names at the keys it names, among builds that compete for them; an upload and a promote add
exactly what they make; the first promote of a folder wins; the library gives what the hooks
give; and a chain node passes its gate.
"""

from __future__ import annotations

import dataclasses
import gzip
import hashlib
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

from lib import layout
from lib.key import KeyJson, encode_key_json, key_of
from lib.layout import Folder
from lib.manifest import (
    Build,
    Promoted,
    Uploaded,
    captured,
    decode_manifest,
    encode_manifest,
    promoted_by,
)
from lib.snapshot.plan import First, Plan, Planned, encode_plan, keyed
from lib.snapshot.records import decode_record

FEATURE = "refs/heads/feature"
TAG = "refs/tags/v1.0.0"
REFS = (TAG, FEATURE, MASTER)
BACKENDS = ("s3", "r2")
PLANNED_KEY = "5" * 40 + "-" + "5" * 32
PROMOTED_AT = "2026-11-19T00:00:00.000Z"


def branch_of(ref: str) -> str:
    """The name Buildkite gives the build of ``ref``: a branch's, or a tag's."""
    return ref.split("/", 2)[2]


def latest_ref(ref: str) -> str:
    """The ref whose latest files a build of ``ref`` writes: its name's, as a branch."""
    return "refs/heads/" + branch_of(ref)


def made_up_key(*facts) -> str:
    """A key of the form of the plugin's, for a build the test only writes objects of."""
    digest = hashlib.sha512(" ".join(map(str, facts)).encode()).hexdigest()
    return f"{digest[:40]}-{digest[40:72]}"


def upload_and_promote(variant="linux", step="build", promotes=True, **upload) -> dict:
    """The options of a step ``step`` that uploads ``variant`` and, when ``promotes``,
    promotes it; an upload option given as None is left out."""
    upload = {k: v for k, v in upload.items() if v is not None}
    options = {"step": step, "upload": {"variant": variant, **upload}}
    return {**options, "promote": {"variant": variant}} if promotes else options


def case_id(value) -> str:
    return "+".join(map(case_id, value)) if isinstance(value, tuple) else str(value)


def step_wrote(key, ref, folder, files, number, promoted, project="widget", under="") -> dict:
    """The objects the plugin writes for a step of ``project``'s build ``number`` on ``ref``
    that uploaded ``files`` as ``folder`` at ``key``: the files and the manifest, and when
    ``promoted`` the manifest promoted and the latest file of the branch."""
    location = layout.step_folder(under, project, key, folder)
    build = Build(build_id(number), number, build_url(project, number))
    m = captured(((rel, len(data)) for rel, data in files.items()), Uploaded(branch_of(ref), build))
    if promoted:
        m = promoted_by(m, Promoted(PROMOTED_AT, branch_of(ref), build))
    objects = {f"{location}/{rel}": data for rel, data in files.items()}
    objects[layout.manifest(location)] = encode_manifest(m)
    if promoted:
        objects[layout.latest_file(under, project, latest_ref(ref), folder)] = key.encode()
    return objects


# ---------------------------------------------------------------------------
# What travels: payloads, builds that compete, how an entry names its steps
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


def members(step: str) -> tuple[str, ...]:
    """The members of the archives step ``step`` makes."""
    return (f"bin/{step}-tool", f"lib/{step}.so", f"share/{step}.txt")


@dataclasses.dataclass(frozen=True)
class Payload:
    """What a step uploads (by path below its directory), the upload's options besides its
    files, what its exclude leaves out, the download options of it, and the files that arrive
    under the output directory. ``stored`` and ``arrives`` take the stamp of the build that made
    the files, which their bytes carry, and the step's id, which their names carry, so that two
    steps of one platform make files of their own."""

    name: str
    stored: Callable[[str, str], dict]
    download: dict
    arrives: Callable[[str, str], dict]
    upload: dict = dataclasses.field(default_factory=dict)
    left_out: dict = dataclasses.field(default_factory=dict)

    def __str__(self) -> str:
        return self.name

    def made(self, stamp: str, step: str) -> dict:
        """What the step leaves in its checkout, in its own directory."""
        files = {**self.stored(stamp, step), **self.left_out}
        return {f"{step}/{rel}": data for rel, data in files.items()}

    def options(self, step: str) -> dict:
        """The upload options of the step: its directory, stripped, and the payload's."""
        return {"files": f"{step}/**/*", "base_dir": step, **self.upload}


PAYLOADS = [
    Payload(
        "plain",
        lambda s, n: made_by(s, f"{n}.bin", f"sub/{n}.txt", f"sub/{n}.tmp"),
        {"files": ["*"], "exclude": ["*.tmp"]},
        lambda s, n: made_by(s, f"{n}.bin", f"sub/{n}.txt"),
        {"exclude": ["*.log"]},
        {"debug.log": b"log"},
    ),
    Payload(
        "tar.gz",
        lambda s, n: {f"{n}.tar.gz": gzip.compress(tar(made_by(s, *members(n))))},
        {"files": ["*.tar.gz"], "extract": True},
        lambda s, n: made_by(s, *members(n)),
    ),
    Payload(
        "tar.zst",
        lambda s, n: {f"{n}.tar.zst": zstd(tar(made_by(s, *members(n))))},
        {"files": ["*.tar.zst"], "extract": True},
        lambda s, n: made_by(s, *members(n)),
    ),
    Payload(
        "zip-and-jar",
        lambda s, n: {
            f"{n}.zip": zipped(made_by(s, f"bin/{n}")),
            f"{n}.jar": zipped(made_by(s, f"META-INF/{n}")),
        },
        {"files": ["*.zip", "*.jar"], "extract": True},
        lambda s, n: made_by(s, f"bin/{n}", f"META-INF/{n}"),
    ),
    Payload(
        "entry-filters",
        lambda s, n: {f"{n}.tar.zst": zstd(tar(made_by(s, *members(n))))},
        {"files": ["*.tar.zst!bin/*", "*.tar.zst!lib/*"], "extract": True},
        lambda s, n: {Path(m).name: d for m, d in made_by(s, *members(n)[:2]).items()},
    ),
]

# Builds of widget, numbered in the order they ran; the build under test is SUBJECT.
EARLIER, NEWER, SUBJECT, LATER = 1, 2, 3, 4
NEXT = "refs/heads/next"
NEW_BRANCH = "refs/heads/new"  # a branch that has no build yet
STEPS = ("build", "notes")


def stamp(number: int, ref: str, variant: str, step: str) -> str:
    return f"build {number} of {ref} {variant} {step}"


def built(payload: Payload, number: int, ref: str, variant: str, promoted: bool) -> dict:
    """The objects of both steps of widget's build ``number`` of ``payload`` at a key of its
    own, as the plugin writes them."""
    key = made_up_key("widget", number, ref)
    return {
        k: body
        for step in STEPS
        for k, body in step_wrote(
            key,
            ref,
            Folder(variant, step),
            payload.stored(stamp(number, ref, variant, step), step),
            number,
            promoted,
        ).items()
    }


def history(payload: Payload, ref: str) -> dict:
    """The builds of widget before the build under test: on its ref and on master, for linux
    and macos, an earlier build that was promoted and a newer one that was not."""
    return {
        k: body
        for r in dict.fromkeys((ref, MASTER))
        for v in ("linux", "macos")
        for number, promoted in ((EARLIER, True), (NEWER, False))
        for k, body in built(payload, number, r, v, promoted).items()
    }


@dataclasses.dataclass(frozen=True)
class Naming:
    """How a downloading step names steps of linux that the build under test made on its ref:
    its pipeline, whether it is a job of that build (else of build 9 on ``own_ref``), its entry
    for that ref, whether the notes step of the build under test passed, which build made each
    step the entry takes (``taken``: the build under test, or the earlier one), and whether a
    later build of the ref was promoted before it downloads, so that the latest files name
    another key than the one it names."""

    name: str
    pipeline: str
    same_build: bool
    entry: Callable[[str, str], dict]
    notes_pass: bool
    taken: dict
    later_promoted: bool = False
    own_ref: str = MASTER

    def __str__(self) -> str:
        return self.name


NAMINGS = [
    Naming(
        "own-build",
        "widget",
        True,
        lambda ref, key: {"step": list(STEPS)},
        False,
        {"build": SUBJECT, "notes": SUBJECT},
        later_promoted=True,
    ),
    Naming(
        "keys",
        "app",
        False,
        lambda ref, key: {"project_id": "widget", "keys": {s: key for s in STEPS}},
        True,
        {"build": SUBJECT, "notes": SUBJECT},
        later_promoted=True,
    ),
    Naming(
        "one-step",
        "app",
        False,
        lambda ref, key: {"project_id": "widget", "git_ref": ref, "step": "build"},
        False,
        {"build": SUBJECT},
    ),
    Naming(
        "a-list-one-captured",
        "app",
        False,
        lambda ref, key: {"project_id": "widget", "git_ref": ref, "step": list(STEPS)},
        False,
        {"build": SUBJECT, "notes": EARLIER},
    ),
    Naming(
        "no-step",
        "app",
        False,
        lambda ref, key: {"project_id": "widget", "git_ref": ref},
        True,
        {"build": SUBJECT, "notes": SUBJECT},
    ),
    Naming(
        "empty-list",
        "app",
        False,
        lambda ref, key: {"project_id": "widget", "git_ref": ref, "step": []},
        True,
        {"build": SUBJECT, "notes": SUBJECT},
    ),
    Naming(
        "own-project-at-another-ref",
        "widget",
        False,
        lambda ref, key: {"git_ref": ref, "step": "build", "require_strict_ref_match": True},
        False,
        {"build": SUBJECT},
        own_ref=NEXT,
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

# Where the key of the build under test comes from: its plan step, or each job of a build
# without one.
PRODUCERS = ("its-plan-step", "each-job")


@pytest.mark.parametrize(
    "payload, naming, transfer, ref, producer, backend",
    paired(PAYLOADS, NAMINGS, TRANSFERS, REFS, PRODUCERS, BACKENDS),
    ids=case_id,
)
def test_a_download_gets_the_bytes_of_the_steps_it_names(
    jobs, store, agent, payload, naming, transfer, ref, producer
):
    """Among builds of other refs, platforms and keys, earlier and later, promoted and not, a
    step whose entry names steps of a build (its own build's, by their keys, or the latest
    successful of a ref, by name, as a list, or without a step) gets exactly the files of
    those steps at the keys they name, at the paths the options name, and nothing else there;
    an entry of a branch without builds gets every step of master's newest key; the uploads
    annotate their build unless told not to."""
    store.put_all(history(payload, ref))
    maker = jobs("widget", SUBJECT, ref)
    for s in STEPS:
        maker.write(payload.made(stamp(SUBJECT, ref, "linux", s), s))
    planned = {"key": PLANNED_KEY} if producer == "its-plan-step" else {}
    outcomes = [
        maker.with_env(
            BUILDKITE_COMMAND_EXIT_STATUS="0" if s == "build" or naming.notes_pass else "1"
        )
        .with_plugin(
            {
                **planned,
                **upload_and_promote("linux", s, **payload.options(s), **transfer.upload),
                **transfer.plugin,
            }
        )
        .run("post-command")
        for s in STEPS
    ]
    own = KeyJson("widget", maker.commit, (), build_id(SUBJECT))
    key = PLANNED_KEY if planned else key_of(own)
    if naming.later_promoted:
        store.put_all(built(payload, LATER, ref, "linux", promoted=True))
    consumer = maker if naming.same_build else jobs(naming.pipeline, 9, naming.own_ref)
    consumer.write(transfer.left_over)
    entries = [
        {"variant": "linux", "output-dir": "in", **naming.entry(latest_ref(ref), key)},
        {
            "project_id": "widget",
            "variant": "macos",
            "git_ref": NEW_BRANCH,
            "output-dir": "in-macos",
        },
    ]
    projects = [{**e, **payload.download} for e in entries]
    download = {"download": {"projects": projects, **transfer.download}, **transfer.plugin}
    fetched = consumer.with_plugin({**(planned if naming.same_build else {}), **download}).run(
        "pre-command"
    )

    annotations = agent.files(maker.env["BUILDKITE_BUILD_ID"], "annotations")
    expected = {
        rel: data
        for s, number in sorted(naming.taken.items())
        for rel, data in payload.arrives(stamp(number, ref, "linux", s), s).items()
    }
    macos = {
        rel: data
        for s in STEPS
        for rel, data in payload.arrives(stamp(EARLIER, MASTER, "macos", s), s).items()
    }
    assert [o.code for o in (*outcomes, fetched)] == [0, 0, 0], [
        o.output for o in (*outcomes, fetched)
    ]
    assert consumer.files("in") == expected
    assert consumer.files("in-macos") == macos
    assert bool(annotations) == transfer.annotates


# One job of a step at a key: its build, whether its command passed, whether it has files to
# upload, and whether the step promotes.
Attempt = tuple[int, int, bool, bool]

# Orders of jobs of one step of one platform at one key: a job retried after it failed or
# passed, another build of the key, a job without files, a step that only uploads.
ORDERS = {
    "one-passes": [(1, 0, True, True)],
    "retried-after-a-failure": [(1, 1, True, True), (1, 0, True, True)],
    "retried-after-it-passed": [(1, 0, True, True), (1, 0, True, True)],
    "another-build-after": [(1, 0, True, True), (2, 0, True, True)],
    "no-files-then-a-failure": [(1, 0, False, True), (2, 1, True, True)],
    "uploads-only-then-promotes": [(1, 0, True, False), (2, 1, False, True), (3, 0, True, True)],
}


def first_promoter(order: list[Attempt]) -> int | None:
    """The position of the job that promotes the folder: the first whose step promotes, whose
    command passed, and that uploaded files."""
    return next((i for i, (_, s, f, p) in enumerate(order) if p and s == 0 and f), None)


@pytest.mark.parametrize("order, backend", paired(sorted(ORDERS), BACKENDS), ids=case_id)
def test_the_first_promote_wins(jobs, store, order):
    """Jobs of one step at one key upload their files whatever their command did, and promote
    only after a passed command and an upload that was not empty: the folder's manifest names
    the first job that promoted it, with that job's files, and nothing is uploaded into it
    after; without a promote it names the last job that uploaded files, captured; the branch's
    latest file names the key exactly when a job promoted."""
    attempts = ORDERS[order]
    for i, (number, status, has_files, promotes) in enumerate(attempts):
        job = jobs("widget", number)
        if has_files:
            job.write({"out/a.bin": f"attempt {i} of build {number}".encode()})
        options = upload_and_promote(promotes=promotes, files="out/*", allow_empty=True)
        outcome = (
            job.with_env(BUILDKITE_COMMAND_EXIT_STATUS=str(status))
            .with_plugin({"key": PLANNED_KEY, **options})
            .run("post-command")
        )
        assert outcome.code == 0, outcome.output

    folder = layout.step_folder("", "widget", PLANNED_KEY, Folder("linux", "build"))
    stored = store.get(layout.manifest(folder))
    winner = first_promoter(attempts)
    uploaders = [i for i, (_, _, has_files, _) in enumerate(attempts) if has_files]
    holder = winner if winner is not None else (uploaders[-1] if uploaders else None)
    latest = store.get(layout.latest_file("", "widget", MASTER, Folder("linux", "build")))
    if holder is None:
        assert stored is None
        return
    m = decode_manifest(stored)
    number = attempts[holder][0]
    assert (m.promoted.build.number if m.promoted else None) == (
        number if winner is not None else None
    )
    assert m.uploaded.build.number == number
    assert [f.path for f in m.files] == ["out/a.bin"]
    assert store.get(f"{folder}/out/a.bin") == f"attempt {holder} of build {number}".encode()
    assert latest == (PLANNED_KEY.encode() if winner is not None else None)


@pytest.mark.parametrize(
    "prefix, ref, producer",
    paired(["", "ci/artifacts"], (*REFS, "refs/heads/snapshot/feature"), PRODUCERS),
    ids=case_id,
)
def test_storage_is_additive(jobs, store, prefix, ref, producer):
    """An upload and a promote on a branch add exactly the step's files, its manifest, the
    branch's latest file (none under a snapshot/ name) and, in a build without a plan step,
    the key.json of the key the job computed; and leave every other object as it was."""
    under = prefix
    files = {"a.bin": b"a", "sub/b.bin": b"bb"}
    folder = Folder("linux", "build")
    others = {
        k: body
        for r in REFS
        if r != ref
        for k, body in step_wrote(made_up_key(r), r, folder, files, 7, True, under=under).items()
    }
    store.put_all(others)
    job = jobs("widget", 1, ref, destination=f"s3://{BUCKET}/{prefix}".rstrip("/"))
    job.write({f"dist/{rel}": data for rel, data in files.items()})
    planned = {"key": PLANNED_KEY} if producer == "its-plan-step" else {}
    options = {**planned, **upload_and_promote(files="dist/**/*", base_dir="dist")}

    outcome = job.with_plugin(options).run("post-command")

    own = KeyJson("widget", job.commit, (), build_id(1))
    key = PLANNED_KEY if planned else key_of(own)
    location = layout.step_folder(under, "widget", key, folder)
    written = {f"{location}/{rel}": data for rel, data in files.items()}
    latest = layout.latest_file(under, "widget", latest_ref(ref), folder)
    expected = {
        *written,
        layout.manifest(location),
        *([] if "/snapshot/" in ref else [latest]),
        *([] if planned else [layout.key_json(under, "widget", key)]),
    }
    objects = store.objects()
    m = decode_manifest(objects[layout.manifest(location)])
    assert outcome.code == 0, outcome.output
    assert objects.keys() - others.keys() == expected
    assert {k: objects[k] for k in others} == others
    assert {k: objects[k] for k in written} == written
    assert [(f.path, f.size) for f in m.files] == sorted((r, len(d)) for r, d in files.items())
    assert (m.promoted.build.id, m.uploaded.build.id) == (build_id(1), build_id(1))
    assert objects.get(latest, key.encode()) == key.encode()
    assert planned or objects[layout.key_json(under, "widget", key)] == encode_key_json(own)


@pytest.mark.parametrize(
    "payload, by, backend", paired(PAYLOADS, ("branch", "key"), BACKENDS), ids=str
)
def test_the_library_downloads_what_the_hooks_download(
    jobs, store, plugin, tmp_path, payload, by, backend
):
    """A program outside Buildkite that vendors the library, as the README shows, downloads
    one step of a platform, by a branch or by a key, and gets the same files at the same key
    as the download hook, among builds that compete."""
    store.put_all(history(payload, MASTER))
    producer, consumer = jobs("widget", SUBJECT), jobs("app", 9)
    producer.write(payload.made(stamp(SUBJECT, MASTER, "linux", "build"), "build"))
    options = {"key": PLANNED_KEY, **upload_and_promote(**payload.options("build"))}
    found = {"git_ref": MASTER} if by == "branch" else {"key": PLANNED_KEY}
    hooks_found = {"git_ref": MASTER} if by == "branch" else {"keys": {"build": PLANNED_KEY}}
    project = {
        "project_id": "widget",
        "variant": "linux",
        "step": "build",
        "output-dir": "in",
        **hooks_found,
        **payload.download,
    }
    request = {
        "project_id": "widget",
        "variant": "linux",
        "step": "build",
        **found,
        "patterns": payload.download["files"],
        "exclude_patterns": payload.download.get("exclude"),
        "extract": payload.download.get("extract", False),
    }
    tree = tmp_path / "caller"
    (tree / "vendor").mkdir(parents=True)
    (tree / "vendor" / "qdb_artifacts").symlink_to(plugin.root, target_is_directory=True)

    made = producer.with_plugin(options).run("post-command")
    fetched = consumer.with_plugin({"download": {"projects": [project]}}).run("pre-command")
    caller = subprocess.run(
        [
            sys.executable,
            "-I",
            "-B",
            Path(__file__).parent / "outside_caller.py",
            tree,
            json.dumps(request),
            tmp_path / "library",
        ],
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), **store_variables(store, backend)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert (made.code, fetched.code, caller.returncode) == (0, 0, 0), (
        made.output + fetched.output + caller.stderr
    )
    assert json.loads(caller.stdout)["key"] == PLANNED_KEY
    files = {
        p.relative_to(tmp_path / "library").as_posix(): p.read_bytes()
        for p in (tmp_path / "library").rglob("*")
        if p.is_file()
    }
    assert files == consumer.files("in")


@pytest.mark.parametrize("step_kind", sorted(STEP_KINDS))
def test_post_checkout_makes_the_venv_of_the_step(plugin, step_kind):
    """post-checkout makes, with the Python that QDB_CICD_AGENT_PLUGIN_PYTHON names (the suite's
    own), the venv the step's options call for, in which the plugin's job side imports, and its
    planning half only for a step that plans. It runs no other Python the agent names, in
    QDB_CICD_AGENT_PYTHON3 or as python3 on PATH, so without that variable it fails."""
    venv = plugin.root / (".venv-snapshot" if step_kind == "plan" else ".venv") / "bin" / "python"

    def imports(module):
        script = f"import platform, {module}; print(platform.python_version())"
        run = [venv, "-c", script]
        return subprocess.run(run, cwd=plugin.root, capture_output=True, text=True, check=False)

    job_side, planning = imports("lib.cli"), imports("lib.snapshot.enter")
    assert plugin.prepared[step_kind].code == 0, plugin.prepared[step_kind].output
    assert plugin.unnamed[step_kind].code != 0, plugin.unnamed[step_kind].output
    assert job_side.stdout.strip() == platform.python_version(), job_side.stderr
    assert (planning.returncode == 0) == (step_kind == "plan")


# ---------------------------------------------------------------------------
# A chain node's gate through the real hooks
# ---------------------------------------------------------------------------

ROOT = 9
ENGINE_COMMIT = "e" * 40
ENGINE_BUILD = 7
OUTCOMES = ("every-step-passes", "the-last-platform-fails")


def chain_plan(widget_commit: str) -> Plan:
    """The plan of a root build of app: app takes widget, widget takes engine, each of one
    step id, with the keys their commits give."""
    projects = {
        "app": Planned(MASTER, "a" * 40, None, "master", ("widget",), ("build",)),
        "widget": Planned(MASTER, widget_commit, None, "master", ("engine",), ("build",)),
        "engine": Planned(MASTER, ENGINE_COMMIT, None, "master", (), ("build",)),
    }
    return keyed(Plan(build_id(ROOT), "master", "none", First("app", ROOT), projects), ())


def engine_file(number: int, variant: str) -> dict:
    return {"engine.tar.zst": f"engine {number} {variant}".encode()}


def engine_made(plan: Plan, variant: str) -> dict:
    """engine's build that made the planned key, and a later build at another key that was
    promoted after it, so that engine's latest files name that other key."""
    folder = Folder(variant, "build")
    later = made_up_key("engine", "later")
    return {
        **step_wrote(
            plan.projects["engine"].key,
            MASTER,
            folder,
            engine_file(ENGINE_BUILD, variant),
            ENGINE_BUILD,
            True,
            "engine",
        ),
        **step_wrote(later, MASTER, folder, engine_file(8, variant), 8, True, "engine"),
    }


def artifacts_options(step: dict) -> dict:
    ((options,),) = [
        [o for name, o in p.items() if name.startswith(PLUGIN)] for p in step["plugins"]
    ]
    return options


@pytest.mark.parametrize(
    "matrix, backend, outcome", paired(sorted(MATRICES.values()), BACKENDS, OUTCOMES), ids=case_id
)
def test_a_chain_node_passes_its_gate_through_the_real_hooks(jobs, agent, store, matrix, outcome):
    """Through the real hooks, a chain node's plan step creates the key.json of its planned key
    and triggers the dependency it lacks, its resume step uploads the pipeline, each build step
    downloads the dependency's step at the key the plan names although its latest files name
    another, and promotes the node's folder at its key after a passed command; leave records
    exactly the folder whose command failed."""
    failing = set(matrix[-1:]) if outcome == "the-last-platform-fails" else set()
    steps = [build_step("widget", v, [entry("engine", v)]) for v in matrix]
    template = yaml.safe_dump({"steps": steps})
    job = jobs("widget", 1).committed(
        {".buildkite/steps.yml": template, ".buildkite/pipeline.py": SHAPED}
    )
    job.write(
        {GENERATED: generated(template, MASTER), **{f"out/{v}.bin": v.encode() for v in matrix}}
    )
    plan = chain_plan(job.commit)
    needs = json.dumps([[v, "build"] for v in sorted(matrix)])
    agent.set(build_id(ROOT), "meta-data", "snapshot-plan", encode_plan(plan))
    agent.set(build_id(1), "meta-data", "snapshot-arrived", str(int(time.time())))
    gate = job.with_env(SNAPSHOT_PLAN=build_id(ROOT), SNAPSHOT_NEEDS=needs)

    planned = (
        gate.with_env(BUILDKITE_STEP_KEY="snapshot-enter")
        .with_plugin({"plan": GENERATED})
        .run("post-command")
    )
    for v in matrix:
        store.put_all(engine_made(plan, v))
    resumed = (
        gate.with_env(BUILDKITE_STEP_KEY="snapshot-resume")
        .with_plugin({"resume": GENERATED})
        .run("post-command")
    )
    triggers, pipeline = [yaml.safe_load(u)["steps"] for u in agent.files(build_id(1), "uploads")]
    ran, fetched = [], {}
    for step in pipeline:
        options = artifacts_options(step)
        v = options["upload"]["variant"]
        this = job.with_env(BUILDKITE_STEP_KEY=step["key"]).with_plugin(options)
        ran.append(this.run("pre-command"))
        fetched.update({v: (job.workspace / "engine.tar.zst").read_bytes()})
        status = 1 if v in failing else 0
        ran.append(this.with_env(BUILDKITE_COMMAND_EXIT_STATUS=str(status)).run("post-command"))
        agent.set(build_id(1), "outcomes", step["key"], "hard_failed" if status else "passed")
    left = (
        gate.with_env(BUILDKITE_STEP_KEY="snapshot-leave")
        .with_plugin({"leave": True})
        .run("post-command")
    )

    key = plan.projects["widget"].key
    manifests = {
        v: store.get(layout.manifest(layout.step_folder("", "widget", key, Folder(v, "build"))))
        for v in matrix
    }
    made = {v for v, m in manifests.items() if m and decode_manifest(m).promoted is not None}
    records = [
        decode_record(body.decode(), "widget", key)
        for k, body in store.objects(f"widget/{key}/failures/").items()
    ]
    assert all(o.code == 0 for o in (planned, resumed, *ran, left)), [
        o.output for o in (planned, resumed, *ran, left)
    ]
    assert store.get(layout.key_json("", "widget", key)) is not None
    assert [s.get("trigger") for s in triggers if "trigger" in s] == ["engine"]
    assert fetched == {v: engine_file(ENGINE_BUILD, v)["engine.tar.zst"] for v in matrix}
    assert made == set(matrix) - failing
    assert all(decode_manifest(manifests[v]).promoted.build.id == build_id(1) for v in made)
    assert {f.folder for r in records for f in r.failed} == {Folder(v, "build") for v in failing}
