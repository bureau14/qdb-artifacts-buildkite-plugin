"""One chain, and its gate steps over the fakes of the planning package, for the tests.

A root build of ``app`` on master, which depends on ``lib`` and ``core``;
``lib`` depends on ``core``. Every step runs against an in-memory store, a
recording agent, git and Buildkite from dictionaries, a fixed clock and a list
for the log.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from fakes import FakeCheckout, FakeGit, FakePipelines, MemoryStore, RecordingAgent

from lib import layout, pointer
from lib.errors import ArtifactsError
from lib.pointer import Build, Pointer
from lib.snapshot.buildkite import PipelineFacts
from lib.snapshot.declaration import read_declaration
from lib.snapshot.enter import PlanEdge, enter, resume
from lib.snapshot.job import Job
from lib.snapshot.leave import leave
from lib.snapshot.plan import encode_plan
from lib.snapshot.planner import Root, make_plan
from lib.snapshot.records import Origin, Record, decode_record, encode_record

APP, LIB, CORE = "app", "lib", "core"
LINUX, MACOS = "linux", "macos"
MASTER = "refs/heads/master"
PLUGIN = "bureau14/qdb-artifacts#master"
PLUGIN_COMMIT = "a" * 40
T = 1_763_380_800
ARRIVED = T - 60

# What each step of a project's pipeline produces and downloads: (key, variant, downloads).
STEPS = {
    CORE: [("build-linux", LINUX, []), ("build-macos", MACOS, [])],
    LIB: [("build-linux", LINUX, [(CORE, LINUX)]), ("build-macos", MACOS, [(CORE, MACOS)])],
    APP: [
        ("build-linux", LINUX, [(LIB, LINUX), (CORE, LINUX)]),
        ("test-macos", None, [(CORE, MACOS)]),
    ],
}


def build_of(project: str, number: int) -> Build:
    return Build(
        f"01890000-bbbb-7000-8000-{number:012d}", number, f"https://ci.example/{project}/{number}"
    )


ROOT_BUILD = build_of(APP, 7)
BUILDS = {APP: ROOT_BUILD, LIB: build_of(LIB, 21), CORE: build_of(CORE, 31)}
# What app's plan step hands down to each dependency it triggers.
NEEDS = {LIB: [LINUX], CORE: [LINUX, MACOS]}


def url(project: str) -> str:
    return f"https://git.example.test/example/{project}.git"


def commit(project: str) -> str:
    return hashlib.sha1(project.encode()).hexdigest()


def step(project: str, key: str, variant: str | None, downloads: list, git_ref: str) -> dict:
    options: dict = {}
    if downloads:
        entries = [
            {"project_id": p, "variant": v, "git_ref": git_ref, "files": ["*.tar.zst"]}
            for p, v in downloads
        ]
        options["download"] = {"projects": entries}
    if variant:
        options["upload"] = {"variant": variant, "git_ref": git_ref, "files": "out/*"}
        options["promote"] = {"variant": variant, "git_ref": git_ref}
    return {
        "label": f"{project} {key}",
        "key": key,
        "command": "make",
        "plugins": [{PLUGIN: options}],
    }


def pipeline(project: str, git_ref: str = MASTER) -> dict:
    """The pipeline the project's generator prints for a build on master."""
    return {"steps": [step(project, *s, git_ref) for s in STEPS[project]]}


SHAPED = (
    b"from qdb_pipeline import run\n\n\ndef generate_pipeline():\n    return None\n\n\n"
    b'if __name__ == "__main__":\n    run(generate_pipeline)\n'
)


def tree(project: str, doc: dict | None = None) -> dict:
    """The committed files: the template the pipeline comes from, and the runner's pipeline.py."""
    template = doc if doc is not None else pipeline(project, "{git_ref}")
    return {".buildkite/steps.yml": yaml.safe_dump(template), ".buildkite/pipeline.py": SHAPED}


def fake_git() -> FakeGit:
    return FakeGit(
        refs={(url(p), MASTER): commit(p) for p in STEPS},
        trees={(url(p), commit(p)): tree(p) for p in STEPS},
    )


def fake_pipelines(**facts: PipelineFacts) -> FakePipelines:
    return FakePipelines({p: facts.get(p, PipelineFacts(p, url(p), True)) for p in STEPS})


def made_plan(rebuild: str = "none"):
    """The plan of app build 7 on master, as its plan step makes it."""
    root = Root(APP, "master", commit(APP), ROOT_BUILD.id, 7, ROOT_BUILD.url, rebuild)
    return make_plan(root, read_declaration(tree(APP), APP), fake_git(), fake_pipelines(), T - 100)


PLAN = made_plan()


def key(project: str) -> str:
    return PLAN.nodes[project].key


def held(project: str, variant: str, number: int) -> Pointer:
    """The pointer of a variant of the project at its key, held by build ``number``."""
    b = build_of(project, number)
    return Pointer(
        project, key(project), variant, MASTER, commit(project), b, "2025-11-17T12:00:00.000Z"
    )


def pointer_objects(*pointers: Pointer) -> dict:
    return {
        layout.snapshot_pointer("", p.project, p.key, p.variant): pointer.encode(p)
        for p in pointers
    }


def record_objects(*records: Record) -> dict:
    return {
        layout.failure_record("", r.project, r.key, r.build.id): encode_record(r).encode()
        for r in records
    }


def origin(project: str, number: int, plan_id: str = ROOT_BUILD.id) -> Origin:
    return Origin(project, key(project), plan_id, build_of(project, number))


ALL_HELD = (
    held(CORE, LINUX, 30),
    held(CORE, MACOS, 30),
    held(LIB, LINUX, 20),
    held(LIB, MACOS, 20),
)

STEP_KEYS = {"plan": "snapshot-enter", "resume": "snapshot-resume", "leave": "snapshot-leave"}
STEP_COMMANDS = {"plan": enter, "resume": resume, "leave": leave}


class Gate:
    """One gate step of a build of the chain of app build 7, over the fakes.

    ``project`` names the build: app is the root, lib and core chain nodes
    triggered by it. ``doc`` is what the step's command generated; ``objects``
    the store's content; ``job`` changes the job.
    """

    def __init__(
        self, tmp_path: Path, project: str, kind: str = "plan", doc=None, objects=None, **job
    ):
        self.store = MemoryStore(objects)
        chain = project != APP
        meta = {"snapshot-arrived": str(ARRIVED)}
        self.agent = RecordingAgent(
            meta_data=meta,
            builds={ROOT_BUILD.id: {"snapshot-plan": encode_plan(PLAN)}} if chain else {},
        )
        self.git = fake_git()
        self.pipelines = fake_pipelines()
        self.checkout = FakeCheckout(commit(project), tree(project))
        self.lines: list[str] = []
        generated = tmp_path / f"{project}-{kind}.yml"
        generated.write_text(yaml.safe_dump(doc if doc is not None else pipeline(project)))
        self.job = Job(
            kind=kind,
            pipeline=str(generated),
            step_key=STEP_KEYS[kind],
            project=project,
            build=BUILDS[project],
            branch="master",
            command="make pipeline",
            exit_status=0,
            plan_id=ROOT_BUILD.id if chain else None,
            needs=json.dumps(NEEDS[project]) if chain else None,
            rebuild=None,
        )
        self.job = dataclasses.replace(self.job, **job)

    @property
    def edge(self) -> PlanEdge:
        return PlanEdge(
            store=self.store,
            prefix="",
            agent=self.agent,
            checkout=self.checkout,
            clock=lambda: float(T),
            log=self.lines.append,
            git=self.git,
            pipelines=self.pipelines,
            plugin_commit=lambda: PLUGIN_COMMIT,
        )

    def run(self) -> int:
        return STEP_COMMANDS[self.job.kind](self.job, self.edge)

    def fails(self) -> str:
        with pytest.raises(ArtifactsError) as raised:
            self.run()
        return str(raised.value)

    @property
    def mode(self):
        return self.agent.meta_data.get("snapshot-mode")

    def records(self) -> list[Record]:
        found = sorted(k for k in self.store.objects if k.startswith("_snapshot/v1/failures/"))
        return [decode_record(self.store.objects[k].decode()) for k in found]

    def uploaded(self) -> list:
        return [yaml.safe_load(u) for u in self.agent.uploads]

    def annotation(self, context: str = "snapshot") -> tuple[str, str]:
        found = [(body, style) for body, style, c in self.agent.annotations if c == context]
        return found[-1]


def entries_of(doc: dict, step_key: str) -> list[dict]:
    (s,) = [s for s in doc["steps"] if s.get("key") == step_key]
    (options,) = [
        v for p in s["plugins"] for k, v in p.items() if k.startswith("bureau14/qdb-artifacts")
    ]
    return options.get("download", {}).get("projects", [])


def options_of(doc: dict, step_key: str) -> dict:
    (s,) = [s for s in doc["steps"] if s.get("key") == step_key]
    ((plugin, options),) = [item for p in s.get("plugins", []) for item in p.items()]
    return {"plugin": plugin, **options}
