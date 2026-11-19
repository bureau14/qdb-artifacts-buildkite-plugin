"""The gate steps over the fakes of the planning package, decision by decision.

One chain: a root build of ``app`` on master, which depends on ``lib`` and
``core``; ``lib`` depends on ``core``. Every step runs against an in-memory
store, a recording agent, git and Buildkite from dictionaries, a fixed clock
and a list for the log.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path

import pytest
import yaml
from fakes import FakeCheckout, FakeGit, FakePipelines, MemoryStore, RecordingAgent
from repos import commit as commit_files
from repos import new_repository

from lib import layout, pointer
from lib.errors import ArtifactsError
from lib.pointer import Build, Pointer
from lib.snapshot import steps
from lib.snapshot.agent import AgentError
from lib.snapshot.buildkite import BuildkiteError, PipelineFacts
from lib.snapshot.declaration import read_declaration
from lib.snapshot.plan import decode_plan, encode_plan
from lib.snapshot.planner import Root, make_plan
from lib.snapshot.records import (
    GENERATOR_FAILED,
    HARD_FAILED,
    NOT_DECLARED,
    RULE,
    Failed,
    Origin,
    Record,
    compose_record,
    decode_record,
    encode_record,
)

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
        self.job = steps.Job(
            kind=kind,
            pipeline=str(generated),
            step_key={
                "plan": "snapshot-enter",
                "resume": "snapshot-resume",
                "leave": "snapshot-leave",
            }[kind],
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
    def edge(self) -> steps.Edge:
        return steps.Edge(
            store=self.store,
            prefix="",
            agent=self.agent,
            git=self.git,
            checkout=self.checkout,
            pipelines=self.pipelines,
            plugin_commit=lambda: PLUGIN_COMMIT,
            clock=lambda: float(T),
            log=self.lines.append,
        )

    def run(self) -> int:
        step = {"plan": steps.enter, "resume": steps.resume, "leave": steps.leave}[self.job.kind]
        return step(self.job, self.edge)

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


# ---------------------------------------------------------------------------
# Enter, step 1: the command failed
# ---------------------------------------------------------------------------


def test_1_a_failed_command_fails_a_root_without_a_record(tmp_path):
    gate = Gate(tmp_path, APP, exit_status=2)

    message = gate.fails()

    assert "the command exited 2" in message
    assert gate.mode == "failed"
    body, style = gate.annotation()
    assert style == "error"
    assert body.startswith("The pipeline generator of `app` build 7 failed")
    assert gate.records() == [] and gate.agent.uploads == []


def test_1_a_failed_command_of_a_chain_node_records_generator_failed(tmp_path):
    gate = Gate(tmp_path, LIB, exit_status=1)

    gate.fails()

    (record,) = gate.records()
    assert (record.project, record.key, record.plan) == (LIB, key(LIB), ROOT_BUILD.id)
    assert (record.reason, record.build) == (GENERATOR_FAILED, BUILDS[LIB])
    assert gate.mode == "failed"


# ---------------------------------------------------------------------------
# Step 2: a retry after the upload
# ---------------------------------------------------------------------------


def test_2_a_step_retried_after_its_upload_does_nothing(tmp_path):
    gate = Gate(tmp_path, LIB)
    gate.agent.meta_data["snapshot-uploaded-snapshot-enter"] = BUILDS[LIB].id

    assert gate.run() == 0

    assert gate.agent.uploads == [] and gate.mode is None
    assert gate.store.calls == []


def test_2_a_marker_of_another_build_is_not_this_steps(tmp_path):
    gate = Gate(tmp_path, LIB, objects=pointer_objects(*ALL_HELD[:2]))
    gate.agent.meta_data["snapshot-uploaded-snapshot-enter"] = BUILDS[CORE].id

    assert gate.run() == 0

    assert len(gate.agent.uploads) == 1 and gate.mode == "built"


def test_2_a_failed_command_fails_also_a_step_that_uploaded(tmp_path):
    gate = Gate(tmp_path, LIB, exit_status=1)
    gate.agent.meta_data["snapshot-uploaded-snapshot-enter"] = BUILDS[LIB].id

    assert "the command exited 1" in gate.fails()


# ---------------------------------------------------------------------------
# Step 3: the rule
# ---------------------------------------------------------------------------


def with_download(doc: dict, step_key: str, entry: dict) -> dict:
    out = json.loads(json.dumps(doc))
    for s in out["steps"]:
        if s["key"] == step_key:
            options = next(iter(s["plugins"][0].values()))
            options.setdefault("download", {"projects": []})["projects"].append(entry)
    return out


def test_3_a_download_no_template_declares_fails_and_names_step_and_project(tmp_path):
    undeclared = {"project_id": "tools", "variant": LINUX, "git_ref": MASTER}
    gate = Gate(tmp_path, APP, doc=with_download(pipeline(APP), "build-linux", undeclared))

    message = gate.fails()

    assert message.startswith("step build-linux: downloads from tools as a snapshot dependency")
    assert gate.annotation()[0].startswith(
        "The generated pipeline of `app` build 7 breaks the rule of its templates"
    )
    assert gate.records() == [] and gate.agent.uploads == []
    assert gate.agent.meta_data.get("snapshot-plan") is None


def test_3_a_chain_node_that_breaks_the_rule_records_rule(tmp_path):
    undeclared = {"project_id": APP, "variant": LINUX, "git_ref": MASTER}
    gate = Gate(tmp_path, LIB, doc=with_download(pipeline(LIB), "build-linux", undeclared))

    gate.fails()

    (record,) = gate.records()
    assert (record.reason, record.key) == (RULE, key(LIB))


def test_3_a_ref_that_is_neither_the_builds_own_nor_a_pin_breaks_the_rule(tmp_path):
    gate = Gate(tmp_path, LIB, doc=pipeline(LIB, "refs/heads/other"))

    assert "neither the build's own ref refs/heads/master nor a pin" in gate.fails()


def test_3_an_own_template_that_does_not_parse_fails_the_build_that_reads_it(tmp_path):
    gate = Gate(tmp_path, APP)
    gate.checkout.tree[".buildkite/broken.yml"] = "bureau14/qdb-artifacts: [\n"

    assert gate.fails().startswith(".buildkite/broken.yml: not valid YAML")
    assert gate.mode == "failed"


@pytest.mark.parametrize("text", [None, "steps: [\n"], ids=["not-written", "not-yaml"])
def test_3_a_generated_pipeline_that_is_not_there_or_does_not_load_is_the_generators_failure(
    tmp_path, text
):
    gate = Gate(tmp_path, LIB)
    path = Path(gate.job.pipeline)
    if text is None:
        path.unlink()
    else:
        path.write_text(text)

    assert gate.fails().startswith("the command passed, and ")
    (record,) = gate.records()
    assert record.reason == GENERATOR_FAILED


# ---------------------------------------------------------------------------
# Step 4: the plan
# ---------------------------------------------------------------------------


def test_4_a_root_makes_the_plan_writes_it_and_annotates_the_build_with_it(tmp_path):
    gate = Gate(tmp_path, APP)

    gate.run()

    written = decode_plan(gate.agent.meta_data["snapshot-plan"])
    assert dataclasses.replace(written, created_at=PLAN.created_at) == PLAN
    body, style = gate.annotation("snapshot-plan")
    assert style == "info" and body.startswith("Snapshot plan of `app` build 7")


def test_4_a_retried_root_loads_the_plan_it_wrote_and_asks_nothing(tmp_path):
    gate = Gate(tmp_path, APP)
    gate.agent.meta_data["snapshot-plan"] = encode_plan(PLAN)

    gate.run()

    assert gate.agent.meta_data["snapshot-plan"] == encode_plan(PLAN)
    assert gate.git.calls == [] and gate.pipelines.calls == []


def test_4_a_chain_node_loads_the_plan_of_its_root_and_asks_neither_git_nor_buildkite(tmp_path):
    gate = Gate(tmp_path, CORE)

    gate.run()

    assert gate.git.calls == [] and gate.pipelines.calls == []
    assert "snapshot-plan" not in gate.agent.meta_data


def test_4_a_chain_node_checked_out_at_another_commit_than_planned_fails(tmp_path):
    gate = Gate(tmp_path, CORE)
    gate.checkout = FakeCheckout("f" * 40, tree(CORE))

    assert f"plans core at {commit(CORE)}, this build is at {'f' * 40}" in gate.fails()
    assert gate.records() == [] and gate.mode == "failed"


def test_4_a_chain_node_whose_root_holds_no_plan_fails(tmp_path):
    gate = Gate(tmp_path, CORE)
    gate.agent.builds[ROOT_BUILD.id] = {}

    assert f"build '{ROOT_BUILD.id}' holds no snapshot-plan" in gate.fails()


def test_4_an_empty_snapshot_plan_is_refused(tmp_path):
    gate = Gate(tmp_path, CORE, plan_id="")

    assert gate.fails().startswith("SNAPSHOT_PLAN is empty")


def test_4_a_dependency_that_cannot_give_a_snapshot_fails_the_plan_with_the_way_out(tmp_path):
    gate = Gate(tmp_path, APP)
    gate.pipelines = fake_pipelines(core=PipelineFacts(CORE, url(CORE), False))

    message = gate.fails()

    assert message.startswith("core cannot give a snapshot at refs/heads/master")
    assert "Way out: core adopts" in message
    assert "or app takes core as a latest successful dependency" in message
    assert gate.annotation()[0].startswith("The snapshot plan of `app` build 7 cannot be made")
    assert "snapshot-plan" not in gate.agent.meta_data and gate.records() == []


def test_4_a_project_buildkite_does_not_know_fails_the_plan_and_names_it(tmp_path):
    gate = Gate(tmp_path, APP)
    del gate.pipelines.facts[CORE]

    assert "Buildkite has no pipeline core" in gate.fails()


@pytest.mark.parametrize("value, rebuilt", [("1", [APP]), ("all", [APP, LIB, CORE])])
def test_4_snapshot_rebuild_gives_the_root_or_every_node_a_key_of_its_own(tmp_path, value, rebuilt):
    gate = Gate(tmp_path, APP, rebuild=value)

    gate.run()

    written = decode_plan(gate.agent.meta_data["snapshot-plan"])
    changed = sorted(p for p in written.nodes if written.nodes[p].key != key(p))
    assert changed == sorted(rebuilt)


def test_4_any_other_snapshot_rebuild_fails_the_plan(tmp_path):
    gate = Gate(tmp_path, APP, rebuild="yes")

    assert "SNAPSHOT_REBUILD='yes': expected 1 or all" in gate.fails()
    assert gate.annotation()[0].startswith("The snapshot plan of `app` build 7 cannot be made")


# ---------------------------------------------------------------------------
# Step 5: a root without a snapshot
# ---------------------------------------------------------------------------

LATEST_TOOLS = {
    "project_id": "tools",
    "variant": LINUX,
    "git_ref": MASTER,
    "build_id": "LATEST_SUCCESSFUL",
}


def test_5_a_root_with_a_latest_successful_dependency_has_no_key_and_builds(tmp_path):
    doc = with_download(pipeline(APP), "test-macos", LATEST_TOOLS)
    template = with_download(pipeline(APP, "{git_ref}"), "test-macos", LATEST_TOOLS)
    gate = Gate(tmp_path, APP, doc=doc, objects=pointer_objects(*ALL_HELD))
    gate.checkout = FakeCheckout(commit(APP), tree(APP, template))

    assert gate.run() == 0

    written = decode_plan(gate.agent.meta_data["snapshot-plan"])
    assert written.nodes[APP].key is None and written.nodes[LIB].key == key(LIB)
    (uploaded,) = gate.uploaded()
    assert "snapshot" not in options_of(uploaded, "build-linux")["upload"]
    assert LATEST_TOOLS in entries_of(uploaded, "test-macos")
    assert gate.mode == "built" and "snapshot-promotes" not in gate.agent.meta_data
    assert not [k for _, k in gate.store.calls if k.startswith("_snapshot/v1/pointers/app/")]


# ---------------------------------------------------------------------------
# Step 6: D and N
# ---------------------------------------------------------------------------


def test_6_a_variant_promoted_by_two_steps_fails_with_the_record_of_the_rule(tmp_path):
    doc = pipeline(LIB)
    doc["steps"].append({**doc["steps"][0], "key": "again", "label": "again"})
    gate = Gate(tmp_path, LIB, doc=doc)

    assert "linux is promoted by two steps, build-linux and again" in gate.fails()
    (record,) = gate.records()
    assert record.reason == RULE


def test_6_a_variant_asked_for_and_not_promoted_records_not_declared_and_compiles_nothing(tmp_path):
    gate = Gate(tmp_path, LIB, needs=json.dumps([LINUX, "windows"]))

    message = gate.fails()

    assert (
        message
        == "asked for windows, which this pipeline does not promote; it promotes linux, macos"
    )
    (record,) = gate.records()
    assert record.reason == NOT_DECLARED
    assert gate.agent.uploads == []
    assert gate.annotation()[0].startswith(
        "`lib` build 21 was asked for a variant it does not promote"
    )


@pytest.mark.parametrize("needs", [None, "linux", '{"linux": 1}', '["linux", "a b"]'])
def test_6_snapshot_needs_that_is_not_a_list_of_variants_fails_without_a_record(tmp_path, needs):
    gate = Gate(tmp_path, LIB, needs=needs)

    assert "is not a JSON list of variants" in gate.fails()
    assert gate.records() == []


# ---------------------------------------------------------------------------
# Step 7: reused
# ---------------------------------------------------------------------------


def test_7_every_variant_asked_for_is_held_the_build_is_reused(tmp_path):
    gate = Gate(tmp_path, LIB, objects=pointer_objects(held(LIB, LINUX, 20)))

    assert gate.run() == 0

    assert gate.mode == "reused" and gate.agent.uploads == []
    body, style = gate.annotation()
    assert style == "success"
    assert "linux in build 20 https://ci.example/lib/20" in body
    assert not [k for _, k in gate.store.calls if "/core/" in k]


def test_7_a_root_is_asked_for_everything_it_promotes(tmp_path):
    objects = pointer_objects(
        *ALL_HELD,
        Pointer(
            APP, key(APP), LINUX, MASTER, commit(APP), build_of(APP, 5), "2025-11-17T12:00:00.000Z"
        ),
    )
    gate = Gate(tmp_path, APP, objects=objects)

    gate.run()

    assert gate.mode == "reused" and gate.agent.uploads == []


def test_7_a_pipeline_that_promotes_nothing_is_never_reused(tmp_path):
    doc = pipeline(APP)
    for s in doc["steps"]:
        options = next(iter(s["plugins"][0].values()))
        options.pop("promote", None)
    gate = Gate(tmp_path, APP, doc=doc, objects=pointer_objects(*ALL_HELD))

    gate.run()

    assert gate.mode == "built"
    assert gate.agent.meta_data["snapshot-promotes"] == "{}"


# ---------------------------------------------------------------------------
# Step 8: a failure shared with the build this one waited behind
# ---------------------------------------------------------------------------


def earlier_failure(number: int, at: float, outcome: str = HARD_FAILED) -> Record:
    failed = [Failed(LINUX, "build-linux", outcome), Failed(MACOS, "build-macos", outcome)]
    return compose_record(origin(CORE, number), at, None, failed, (), None)


def test_8_a_build_of_the_key_that_failed_while_this_one_waited_is_shared(tmp_path):
    gate = Gate(tmp_path, CORE, objects=record_objects(earlier_failure(30, ARRIVED + 5.001)))

    assert "build 30 of the same snapshot failed on linux, macos" in gate.fails()

    own = [r for r in gate.records() if r.build == BUILDS[CORE]]
    (record,) = own
    assert record.shared_with == build_of(CORE, 30)
    assert [f.variant for f in record.failed] == [LINUX, MACOS]
    assert gate.annotation()[0].startswith("Snapshot failed in the build this one waited behind")
    assert gate.agent.uploads == []


@pytest.mark.parametrize(
    "record",
    [
        earlier_failure(30, ARRIVED + 5),
        earlier_failure(30, ARRIVED + 60, outcome="soft_failed"),
        earlier_failure(31, ARRIVED + 60),
    ],
    ids=["recorded-before-this-build-waited", "not-hard-failed", "this-builds-own"],
)
def test_8_any_other_failure_of_the_key_is_built_again(tmp_path, record):
    gate = Gate(tmp_path, CORE, objects=record_objects(record))

    assert gate.run() == 0

    assert gate.mode == "built"


def test_8_a_stored_record_that_does_not_decode_is_skipped_with_a_line_in_the_log(tmp_path):
    where = layout.failure_record("", CORE, key(CORE), "01890000-bbbb-7000-8000-000000000030")
    gate = Gate(tmp_path, CORE, objects={where: b"not a record"})

    assert gate.run() == 0

    assert gate.mode == "built"
    assert any(
        line.startswith(f"skipped s3://example-artifacts/{where}: record: not JSON")
        for line in gate.lines
    )


def test_8_a_record_stored_under_the_key_that_names_another_key_is_skipped(tmp_path):
    other = dataclasses.replace(earlier_failure(30, ARRIVED + 60), key="v1-" + "e" * 32)
    where = layout.failure_record("", CORE, key(CORE), other.build.id)
    gate = Gate(tmp_path, CORE, objects={where: encode_record(other).encode()})

    assert gate.run() == 0

    assert gate.mode == "built"
    assert (
        f"skipped s3://example-artifacts/{where}: a record of core at v1-{'e' * 32}" in gate.lines
    )


# ---------------------------------------------------------------------------
# Steps 9 to 12: dependencies, triggers, the build
# ---------------------------------------------------------------------------


def test_9_a_step_that_completion_replaces_needs_nothing(tmp_path):
    # lib linux exists; core linux is missing, but only the replaced step downloads it.
    objects = pointer_objects(held(LIB, LINUX, 20), held(CORE, MACOS, 30))
    gate = Gate(tmp_path, LIB, needs=json.dumps([LINUX, MACOS]), objects=objects)

    assert gate.run() == 0

    (uploaded,) = gate.uploaded()
    replaced = next(s for s in uploaded["steps"] if s["key"] == "build-linux")
    assert replaced["command"] == 'echo "linux of lib exists in build 20 https://ci.example/lib/20"'
    assert "plugins" not in replaced
    (entry,) = entries_of(uploaded, "build-macos")
    assert entry["build_id"] == build_of(CORE, 30).id
    assert json.loads(gate.agent.meta_data["snapshot-promotes"]) == {MACOS: "build-macos"}


def test_10_a_dependency_that_failed_earlier_in_this_chain_fails_the_build_without_a_trigger(
    tmp_path,
):
    core = compose_record(
        origin(CORE, 31), T - 30, None, [Failed(LINUX, "build-linux", HARD_FAILED)], (), None
    )
    gate = Gate(tmp_path, APP, objects=record_objects(core))

    message = gate.fails()

    assert message.startswith(
        f"core @ {commit(CORE)[:12]} ({key(CORE)}) has no linux, macos: its build 31"
    )
    assert gate.agent.uploads == []
    (own,) = [r for r in gate.records() if r.project == APP]
    assert [(c.project, c.variants) for c in own.caused_by] == [(CORE, (LINUX, MACOS))]
    body, _ = gate.annotation()
    assert "FAILED in core build 31: https://ci.example/core/31" in body
    assert body.endswith(
        "To retry: retry the failed job in `core` `31`. When it passes, start a new build of "
        "`app`. Everything that passed is reused."
    )


def test_10_a_record_of_another_chain_triggers_the_dependency_again(tmp_path):
    other = compose_record(
        origin(CORE, 29, plan_id="01890000-bbbb-7000-8000-000000000099"),
        T - 30,
        None,
        [Failed(LINUX, "build-linux", HARD_FAILED)],
        (),
        None,
    )
    gate = Gate(tmp_path, APP, objects=record_objects(other))

    assert gate.run() == 0

    (uploaded,) = gate.uploaded()
    assert [s["key"] for s in uploaded["steps"]] == [
        "snapshot-dep-core",
        "snapshot-dep-lib",
        "snapshot-resume",
    ]


def test_11_missing_dependencies_are_triggered_dependencies_first_with_the_resume_step(tmp_path):
    gate = Gate(tmp_path, APP)

    assert gate.run() == 0

    (uploaded,) = gate.uploaded()
    core_trigger, lib_trigger, resume = uploaded["steps"]
    assert core_trigger["build"] == {
        "branch": "master",
        "commit": commit(CORE),
        "message": "snapshot dependency of app build 7",
        "env": {"SNAPSHOT_PLAN": ROOT_BUILD.id, "SNAPSHOT_NEEDS": '["linux", "macos"]'},
    }
    assert "depends_on" not in core_trigger
    assert lib_trigger["depends_on"] == ["snapshot-dep-core"]
    assert lib_trigger["build"]["env"]["SNAPSHOT_NEEDS"] == '["linux"]'
    assert resume["command"] == "make pipeline"
    assert resume["plugins"] == [
        {f"bureau14/qdb-artifacts#{PLUGIN_COMMIT}": {"resume": gate.job.pipeline}}
    ]
    assert gate.agent.uploads[0].count('"') > 20  # every string quoted
    assert gate.agent.meta_data["snapshot-uploaded-snapshot-enter"] == ROOT_BUILD.id
    assert gate.mode is None


def test_11_only_the_dependencies_with_a_missing_variant_are_triggered(tmp_path):
    gate = Gate(tmp_path, APP, objects=pointer_objects(*ALL_HELD[:2]))

    gate.run()

    (uploaded,) = gate.uploaded()
    assert [s["key"] for s in uploaded["steps"]] == ["snapshot-dep-lib", "snapshot-resume"]
    assert "depends_on" not in uploaded["steps"][0]


def test_11_a_trigger_hands_down_every_variant_the_pipeline_names_not_only_the_missing(tmp_path):
    gate = Gate(tmp_path, APP, objects=pointer_objects(held(CORE, LINUX, 30), *ALL_HELD[2:]))

    gate.run()

    (uploaded,) = gate.uploaded()
    (core_trigger, _) = uploaded["steps"]
    assert core_trigger["build"]["env"]["SNAPSHOT_NEEDS"] == '["linux", "macos"]'


def test_12_with_every_variant_there_the_rewritten_pipeline_is_uploaded(tmp_path):
    gate = Gate(tmp_path, APP, objects=pointer_objects(*ALL_HELD))

    assert gate.run() == 0

    (uploaded,) = gate.uploaded()
    lib_linux, core_linux = entries_of(uploaded, "build-linux")
    assert (lib_linux["build_id"], lib_linux["git_ref"]) == (build_of(LIB, 20).id, MASTER)
    assert lib_linux["require_strict_ref_match"] is True
    assert core_linux["build_id"] == build_of(CORE, 30).id
    options = options_of(uploaded, "build-linux")
    assert options["plugin"] == f"bureau14/qdb-artifacts#{PLUGIN_COMMIT}"
    assert options["upload"]["snapshot"] == options["promote"]["snapshot"] == key(APP)
    assert json.loads(gate.agent.meta_data["snapshot-promotes"]) == {LINUX: "build-linux"}
    assert gate.agent.meta_data["snapshot-uploaded-snapshot-enter"] == ROOT_BUILD.id
    assert gate.mode == "built"
    assert gate.agent.uploads[0] == yaml.safe_dump(uploaded)


def test_12_a_step_that_promotes_without_a_key_gets_one_for_leave(tmp_path):
    doc = pipeline(CORE)
    del doc["steps"][1]["key"]
    gate = Gate(tmp_path, CORE, doc=doc)

    gate.run()

    assert json.loads(gate.agent.meta_data["snapshot-promotes"]) == {
        LINUX: "build-linux",
        MACOS: "snapshot-promote-1",
    }


# ---------------------------------------------------------------------------
# Errors of the edge
# ---------------------------------------------------------------------------


def test_an_error_of_the_store_fails_the_step_without_a_record(tmp_path):
    gate = Gate(tmp_path, LIB)
    gate.store.refused.append("_snapshot/v1/pointers/")

    message = gate.fails()

    assert "AccessDenied" in message
    assert gate.mode == "failed"
    assert gate.annotation()[0].startswith("The snapshot step of `lib` build 21 failed")
    assert gate.records() == []


def test_a_pointer_that_does_not_read_fails_the_step(tmp_path):
    where = layout.snapshot_pointer("", LIB, key(LIB), LINUX)
    gate = Gate(tmp_path, LIB, objects={where: b"{}"})

    assert f"s3://example-artifacts/{where}: snapshot pointer" in gate.fails()


def test_an_error_of_git_fails_the_plan_step(tmp_path):
    gate = Gate(tmp_path, APP)
    del gate.git.trees[(url(CORE), commit(CORE))]

    assert f"fake git: {url(CORE)} has no commit {commit(CORE)}" in gate.fails()
    assert gate.annotation()[0].startswith("The snapshot step of `app` build 7 failed")


def test_an_error_of_the_api_fails_the_plan_step_and_is_never_an_answer(tmp_path):
    gate = Gate(tmp_path, APP)

    def refusing(slug):
        raise BuildkiteError(f"pipeline {slug}: Buildkite answered 500")

    gate.pipelines = refusing

    assert "Buildkite answered 500" in gate.fails()
    assert "snapshot-plan" not in gate.agent.meta_data


class RefusingAgent(RecordingAgent):
    """An agent that cannot upload, and says so as buildkite-agent does."""

    def pipeline_upload(self, document: str) -> None:
        raise AgentError("buildkite-agent pipeline upload failed (1): refused")


def test_an_error_of_the_agent_fails_the_step(tmp_path):
    gate = Gate(tmp_path, LIB, objects=pointer_objects(*ALL_HELD[:2]))
    gate.agent = RefusingAgent(builds=gate.agent.builds)

    assert "pipeline upload failed (1): refused" in gate.fails()
    assert gate.mode == "failed"
    assert "snapshot-uploaded-snapshot-enter" not in gate.agent.meta_data


class Mute(RecordingAgent):
    def annotate(self, body: str, style: str, context: str) -> None:
        raise AgentError("buildkite-agent annotate failed (1): gone")


def test_a_failing_step_that_cannot_annotate_still_sets_its_mode_and_says_so(tmp_path):
    gate = Gate(tmp_path, APP, exit_status=1)
    gate.agent = Mute()

    message = gate.fails()

    assert message.endswith("; could not annotate: buildkite-agent annotate failed (1): gone")
    assert gate.mode == "failed"


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------


def test_resume_builds_once_the_triggered_builds_gave_every_variant(tmp_path):
    gate = Gate(tmp_path, APP, kind="resume", objects=pointer_objects(*ALL_HELD))
    gate.agent.meta_data["snapshot-uploaded-snapshot-enter"] = ROOT_BUILD.id
    gate.agent.meta_data["snapshot-plan"] = encode_plan(PLAN)

    assert gate.run() == 0

    assert gate.mode == "built"
    assert gate.agent.meta_data["snapshot-uploaded-snapshot-resume"] == ROOT_BUILD.id
    assert gate.git.calls == [] and gate.pipelines.calls == []


def test_resume_retried_after_its_own_upload_does_nothing(tmp_path):
    gate = Gate(tmp_path, APP, kind="resume")
    gate.agent.meta_data["snapshot-uploaded-snapshot-resume"] = ROOT_BUILD.id

    assert gate.run() == 0
    assert gate.agent.uploads == [] and gate.store.calls == []


def test_resume_names_a_dependency_that_failed_in_this_chain_as_the_cause(tmp_path):
    core = compose_record(
        origin(CORE, 31), T - 30, None, [Failed(MACOS, "build-macos", HARD_FAILED)], (), None
    )
    objects = {**pointer_objects(*ALL_HELD[2:], held(CORE, LINUX, 31)), **record_objects(core)}
    gate = Gate(tmp_path, APP, kind="resume", objects=objects)
    gate.agent.meta_data["snapshot-plan"] = encode_plan(PLAN)

    assert gate.fails().startswith(
        f"core @ {commit(CORE)[:12]} ({key(CORE)}) has no macos: its build 31"
    )

    (own,) = [r for r in gate.records() if r.project == APP]
    assert [(c.project, c.variants) for c in own.caused_by] == [(CORE, (MACOS,))]
    assert gate.agent.uploads == []


def test_resume_says_a_triggered_build_that_left_no_record_and_names_its_trigger_step(tmp_path):
    gate = Gate(tmp_path, APP, kind="resume", objects=pointer_objects(*ALL_HELD[:2]))
    gate.agent.meta_data["snapshot-plan"] = encode_plan(PLAN)

    message = gate.fails()

    assert message.endswith(
        "the build of lib triggered by this build ended without linux and left no record; see "
        f"the step :link: lib @ {commit(LIB)[:12]}"
    )
    (own,) = gate.records()
    assert [(c.project, c.variants) for c in own.caused_by] == [(LIB, (LINUX,))]
    assert gate.agent.uploads == [] and gate.mode == "failed"


def test_resume_runs_the_first_steps_again(tmp_path):
    gate = Gate(tmp_path, LIB, kind="resume", needs=json.dumps(["windows"]))

    gate.fails()

    (record,) = gate.records()
    assert record.reason == NOT_DECLARED


# ---------------------------------------------------------------------------
# Leave
# ---------------------------------------------------------------------------


def leaving(tmp_path, project: str, outcomes: dict, objects=None, mode="built") -> Gate:
    gate = Gate(tmp_path, project, kind="leave", objects=objects)
    gate.agent.outcomes.update(outcomes)
    if mode is not None:
        gate.agent.meta_data["snapshot-mode"] = mode
    gate.agent.meta_data["snapshot-promotes"] = json.dumps(
        {LINUX: "build-linux", MACOS: "build-macos"}
    )
    if project == APP:
        gate.agent.meta_data["snapshot-plan"] = encode_plan(PLAN)
    return gate


def test_leave_records_the_variants_whose_step_did_not_pass_and_that_have_no_pointer(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED, "build-macos": "passed"})

    assert gate.run() == 0

    (record,) = gate.records()
    assert record.failed == (Failed(LINUX, "build-linux", HARD_FAILED),)
    assert (record.plan, record.build) == (ROOT_BUILD.id, BUILDS[CORE])
    body, style = gate.annotation("snapshot-leave")
    assert style == "error" and body.startswith("Snapshot failed")


def test_leave_records_a_step_that_did_not_finish_with_an_empty_outcome(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-macos": "passed"})

    gate.run()

    (record,) = gate.records()
    assert record.failed == (Failed(LINUX, "build-linux", ""),)


def test_leave_records_nothing_when_every_step_passed(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-linux": "passed", "build-macos": "passed"})

    gate.run()

    assert gate.records() == [] and gate.agent.annotations == []
    assert gate.store.calls == []


def test_leave_records_nothing_for_a_variant_whose_pointer_a_retried_job_wrote(tmp_path):
    objects = pointer_objects(
        Pointer(
            CORE, key(CORE), LINUX, MASTER, commit(CORE), BUILDS[CORE], "2025-11-17T12:00:00.000Z"
        )
    )
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED, "build-macos": "passed"}, objects)

    gate.run()

    assert gate.records() == []


@pytest.mark.parametrize("mode", ["reused", "failed", None])
def test_leave_records_nothing_unless_the_build_was_built(tmp_path, mode):
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED}, mode=mode)

    assert gate.run() == 0

    assert gate.store.calls == []


def test_leave_of_a_root_without_a_snapshot_records_nothing(tmp_path):
    gate = leaving(tmp_path, APP, {"build-linux": HARD_FAILED})
    no_key = dataclasses.replace(
        PLAN, nodes={**PLAN.nodes, APP: dataclasses.replace(PLAN.nodes[APP], key=None)}
    )
    gate.agent.meta_data["snapshot-plan"] = encode_plan(no_key)

    gate.run()

    assert gate.store.calls == []


def test_a_retried_leave_leaves_its_record_as_it_is(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED, "build-macos": "passed"})
    gate.run()
    first = dict(gate.store.objects)
    gate.agent.outcomes["build-macos"] = HARD_FAILED

    gate.run()

    assert gate.store.objects == first
    assert gate.lines[-1].endswith("exists; left as it is")


def test_an_error_of_the_store_fails_leave_and_leaves_the_mode(tmp_path):
    gate = leaving(tmp_path, CORE, {"build-linux": HARD_FAILED})
    gate.store.refused.append("_snapshot/v1/failures/")

    assert "AccessDenied" in gate.fails()
    assert gate.mode == "built"
    assert gate.annotation("snapshot-leave")[0].startswith(
        "The snapshot step of `core` build 31 failed"
    )


# ---------------------------------------------------------------------------
# The job and the real edge
# ---------------------------------------------------------------------------

JOB_ENV = {
    "BUILDKITE_PIPELINE_SLUG": LIB,
    "BUILDKITE_STEP_KEY": "snapshot-enter",
    "BUILDKITE_BUILD_ID": BUILDS[LIB].id,
    "BUILDKITE_BUILD_NUMBER": "21",
    "BUILDKITE_BUILD_URL": BUILDS[LIB].url,
    "BUILDKITE_BRANCH": "snapshot/feature",
    "BUILDKITE_COMMAND": "make pipeline",
    "BUILDKITE_COMMAND_EXIT_STATUS": "3",
    "SNAPSHOT_PLAN": ROOT_BUILD.id,
    "SNAPSHOT_NEEDS": '["linux"]',
}


def test_the_job_is_read_from_the_variables_the_agent_sets(monkeypatch):
    for name, value in JOB_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SNAPSHOT_REBUILD", raising=False)

    job = steps.job_of("plan", ".buildkite/snapshot-pipeline.yml")

    assert job == steps.Job(
        kind="plan",
        pipeline=".buildkite/snapshot-pipeline.yml",
        step_key="snapshot-enter",
        project=LIB,
        build=BUILDS[LIB],
        branch="snapshot/feature",
        command="make pipeline",
        exit_status=3,
        plan_id=ROOT_BUILD.id,
        needs='["linux"]',
        rebuild=None,
    )


@pytest.mark.parametrize(
    "name, value, named",
    [
        ("BUILDKITE_BUILD_ID", "build-7", "BUILDKITE_BUILD_ID"),
        ("BUILDKITE_BUILD_URL", "http://ci.example/lib/21", "BUILDKITE_BUILD_URL"),
        ("BUILDKITE_PIPELINE_SLUG", "Lib", "BUILDKITE_PIPELINE_SLUG"),
    ],
)
def test_a_job_variable_of_the_wrong_form_is_refused(monkeypatch, name, value, named):
    for n, v in {**JOB_ENV, name: value}.items():
        monkeypatch.setenv(n, v)

    with pytest.raises(ArtifactsError, match=named):
        steps.job_of("plan", "p.yml")


class FakeSsm:
    """An SSM client with one parameter, or none."""

    def __init__(self, value: str | None) -> None:
        self.value = value
        self.asked: list[str] = []

    def get_parameter(self, Name: str, WithDecryption: bool):
        from botocore.exceptions import ClientError

        self.asked.append(Name)
        if self.value is None:
            raise ClientError({"Error": {"Code": "ParameterNotFound"}}, "GetParameter")
        return {"Parameter": {"Value": self.value}}


def test_the_token_is_the_variable_else_its_ssm_parameter(monkeypatch):
    monkeypatch.setenv("ARTIFACTS_BUILDKITE_TOKEN", "bkua_from-the-variable")
    assert steps.buildkite_token(FakeSsm("bkua_from-ssm")) == "bkua_from-the-variable"

    monkeypatch.delenv("ARTIFACTS_BUILDKITE_TOKEN")
    ssm = FakeSsm("bkua_from-ssm")
    assert steps.buildkite_token(ssm) == "bkua_from-ssm"
    assert ssm.asked == ["/services/buildkite/credentials/artifacts/buildkite-api-token"]


def test_without_a_token_the_message_names_where_one_is_read(monkeypatch):
    monkeypatch.delenv("ARTIFACTS_BUILDKITE_TOKEN", raising=False)

    with pytest.raises(ArtifactsError, match="set ARTIFACTS_BUILDKITE_TOKEN or the SSM parameter"):
        steps.buildkite_token(FakeSsm(None))


def test_the_api_is_asked_with_the_organisation_and_the_token_read_once(monkeypatch):
    monkeypatch.setenv("BUILDKITE_ORGANIZATION_SLUG", "example")
    monkeypatch.delenv("ARTIFACTS_BUILDKITE_TOKEN", raising=False)
    asked = []
    monkeypatch.setattr(
        "lib.snapshot.buildkite.fetch_pipeline",
        lambda slug, organisation, token: asked.append((slug, organisation, token)) or slug,
    )
    ssm = FakeSsm("bkua_test-token-not-real")
    pipelines = steps.buildkite_pipelines(ssm)
    assert ssm.asked == []

    assert [pipelines(LIB), pipelines(CORE)] == [LIB, CORE]

    assert asked == [
        (LIB, "example", "bkua_test-token-not-real"),
        (CORE, "example", "bkua_test-token-not-real"),
    ]
    assert len(ssm.asked) == 1


def test_the_plugins_own_checkout_is_the_directory_of_its_hooks_and_library():
    assert steps.PLUGIN_ROOT == Path(steps.__file__).resolve().parents[2]
    assert (steps.PLUGIN_ROOT / "hooks" / "post-command").is_file()


def test_the_plugin_commit_is_the_head_of_the_plugins_own_checkout(
    isolated_git, tmp_path, monkeypatch
):
    checkout = new_repository(tmp_path / "plugin")
    head = commit_files(checkout, {"hooks/post-command": "#!/usr/bin/env bash\n"}, "the plugin")
    monkeypatch.setattr(steps, "PLUGIN_ROOT", checkout)

    assert steps.plugin_checkout_commit() == head
