"""The chain's world: projects generated from a graph, a platform matrix and the step ids of
every platform, and a simulated Buildkite that plays their builds in process, over the fakes
of ``fakes.py``.

Every project is a repository with a step template and a ``pipeline.py`` of the
runner's shape. Its template has, per platform, one step per step id, which writes
``step_id``, downloads the same platform of each snapshot dependency without naming a
step, and uploads. The step of the first step id promotes what it uploaded; each other
is promoted by a check step that waits for it and names it in its promote block. A test
may add a step that promotes a folder again (``Project.packaged``). The project's
generator does what the runner's ``load_template`` does: the key
``<step id>-<platform>``, and the plugin's ``step``.

A build starts with the runner's gate. The gate steps run the library's own
decisions: ``enter`` and ``resume`` over a ``PlanEdge`` of the fakes, ``leave``
over its ``Edge``. A trigger step starts the build it asks for, with its branch,
commit and environment, and plays it to its end. A command step is a simulated
job of the plugin, through the library's own functions: it reads its download entries
(``options.download_entry``), finds the steps each takes (``download.resolved_steps``),
compiles and captures its files when it uploads (``upload.capture_step``), and promotes
after a passed command (``promote.promote_folder``), unless its upload found the folder
promoted. The job of the step that promotes a folder the test broke fails.
"""

from __future__ import annotations

import collections
import dataclasses
import itertools

import pytest
import yaml
from fakes import (
    AgentState,
    BuildAgent,
    FakeCheckout,
    FakeGit,
    FakePipelines,
    Hosting,
    MemoryStore,
    url,
)
from world import paired

from lib import layout, manifest
from lib.download import Entry, resolved_steps
from lib.errors import ArtifactsError
from lib.forms import iso_utc
from lib.key import KeyJson, create_key_json, key_of
from lib.layout import Folder
from lib.manifest import Manifest, Promoted, Uploaded, captured, read_manifest_at
from lib.options import OwnKey, download_entry
from lib.promote import promote_folder
from lib.snapshot.buildkite import PipelineFacts
from lib.snapshot.enter import PlanEdge, enter, resume
from lib.snapshot.job import Job
from lib.snapshot.leave import leave
from lib.snapshot.plan import decode_plan
from lib.snapshot.records import decode_record
from lib.upload import capture_step

PLUGIN = "bureau14/qdb-artifacts"
PLUGIN_COMMIT = "a" * 40
T0 = 1_763_380_800
SHAPED = (
    b"from qdb_pipeline import run\n\n\ndef generate_pipeline():\n    return None\n\n\n"
    b'if __name__ == "__main__":\n    run(generate_pipeline)\n'
)

# ---------------------------------------------------------------------------
# Graphs: project -> its snapshot dependencies
# ---------------------------------------------------------------------------

# The five projects of tests/snapshot/fixtures, as their pipelines download.
REAL = {
    "qdb-api-go": ("quasardb-build",),
    "qdb-api-python": ("qdb-api-rest", "quasardb-build"),
    "qdb-api-rest": ("qdb-duck", "quasardb-build"),
    "qdb-duck": ("quasardb-build",),
    "quasardb-build": (),
}
GRAPHS = {
    "chain": {"app": ("lib",), "lib": ("core",), "core": ()},
    "diamond": {"app": ("left", "right"), "left": ("core",), "right": ("core",), "core": ()},
    "wide": {"app": ("one", "three", "two"), "one": (), "three": (), "two": ()},
    "real": REAL,
}
MATRICES = {"linux": ("linux",), "three": ("linux", "macos", "windows")}
# The step ids every platform of a project makes: one, or two.
STEP_IDS = {"build": ("build",), "build-notes": ("build", "notes")}


def below(graph: dict, p: str) -> frozenset[str]:
    """Every project ``p`` reaches through its snapshot dependencies."""
    return frozenset(graph[p]).union(*(below(graph, d) for d in graph[p]))


def above(graph: dict, p: str) -> frozenset[str]:
    """Every project that reaches ``p``."""
    return frozenset(q for q in graph if p in below(graph, q))


def roots(graph: dict) -> list[str]:
    return sorted(p for p in graph if not above(graph, p))


def tree(graph: dict, root: str) -> frozenset[str]:
    return below(graph, root) | {root}


def folders(matrix: tuple, steps: tuple) -> set[Folder]:
    """Every folder of a project of ``matrix`` and ``steps``."""
    return {Folder(v, s) for v in matrix for s in steps}


@dataclasses.dataclass(frozen=True)
class At:
    """A node of a named graph, and the root of the first tree that holds it."""

    name: str
    node: str

    @property
    def graph(self) -> dict:
        return GRAPHS[self.name]

    @property
    def root(self) -> str:
        return next(r for r in roots(self.graph) if self.node in tree(self.graph, r))

    def __str__(self) -> str:
        return f"{self.name}-{self.node}"


GRAPH_ROOTS = [At(g, r) for g in sorted(GRAPHS) for r in roots(GRAPHS[g])]
GRAPH_NODES = [At(g, n) for g in sorted(GRAPHS) for n in sorted(GRAPHS[g])]
LOWER_NODES = [a for a in GRAPH_NODES if a.node not in roots(a.graph)]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

SPELLINGS = {"snake": lambda k: k, "kebab": lambda k: k.replace("_", "-")}


def entry(
    project: str, variant: str, ref="{git_ref}", latest=False, spelling="snake", step=None
) -> dict:
    """A download entry; a literal ``ref`` is a pin, ``latest`` takes the latest successful
    build, ``step`` names the steps it takes."""
    s = SPELLINGS[spelling]
    e = {s("project_id"): project, "variant": variant, s("git_ref"): ref, "files": ["*.tar.zst"]}
    named = {} if step is None else {"step": step}
    return {**e, **named, **({"latest": True} if latest else {})}


def build_step(
    project: str,
    variant: str,
    entries: list,
    spelling="snake",
    step="build",
    plugin=PLUGIN,
    promotes=True,
) -> dict:
    """The template step of step id ``step`` that builds ``variant`` from ``entries``, uploads
    it and, when ``promotes``, promotes it, with the plugin's options under the name
    ``plugin``."""
    options = {"upload": {"variant": variant, "files": "out/*"}}
    if promotes:
        options = {**options, "promote": {"variant": variant}}
    if entries:
        options = {"download": {"projects": entries}, **options}
    return {
        "label": f"{project} {step} {variant}",
        "step_id": step,
        "command": "make",
        "plugins": [{f"{plugin}#master": options}],
    }


def check_step(project: str, variant: str, step: str) -> dict:
    """The template step that checks what the step of step id ``step`` uploaded for ``variant``,
    after it, and promotes it: a step of a key of its own, which names no step id of its own."""
    return {
        "label": f"{project} check {step} {variant}",
        "key": f"check-{step}-{variant}",
        "depends_on": f"{step}-{variant}",
        "command": "check",
        "plugins": [{f"{PLUGIN}#master": {"promote": {"variant": variant, "step": step}}}],
    }


def promoted_again(project: str, variant: str, entries: list) -> dict:
    """A step that promotes the folder ``variant``/build a second time, written as a pipeline
    without ``pipeline.py`` writes it: its key, and the plugin's ``step`` by hand."""
    options = {
        "step": "build",
        "upload": {"variant": variant, "files": "out/*"},
        "promote": {"variant": variant},
    }
    if entries:
        options = {"download": {"projects": entries}, **options}
    return {
        "label": f"{project} package {variant}",
        "key": f"package-{variant}",
        "command": "make",
        "plugins": [{f"{PLUGIN}#master": options}],
    }


def loaded(step: dict) -> dict:
    """A template step as the runner's ``load_template`` gives it: its ``step_id`` gone, the
    key ``<step id>-<slug>``, and ``step: <step id>`` at the top of every configuration of the
    plugin. The slug of these templates is the platform the step uploads."""
    if "step_id" not in step:
        return step
    step_id = step["step_id"]
    plugins = [
        {
            name: {"step": step_id, **options} if name.split("#", 1)[0] == PLUGIN else options
            for name, options in p.items()
        }
        for p in step["plugins"]
    ]
    slug = next(o["upload"]["variant"] for p in plugins for o in p.values() if "upload" in o)
    kept = {k: v for k, v in step.items() if k not in ("step_id", "plugins")}
    return {**kept, "key": f"{step_id}-{slug}", "plugins": plugins}


def generated(template_text: str, ref: str) -> str:
    """What the project's generator prints for a build of ``ref``."""
    doc = yaml.safe_load(template_text.replace("{git_ref}", ref))
    return yaml.safe_dump({"steps": [loaded(s) for s in doc["steps"]]})


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Project:
    """What a project's repository says: its dependencies, platforms, step ids and how it
    adopted."""

    deps: tuple
    variants: tuple
    steps: tuple = ("build",)
    pins: dict = dataclasses.field(default_factory=dict)  # dependency -> literal ref
    latest: tuple = ()  # projects it takes as latest successful
    pipeline_py: bytes = SHAPED
    extra: tuple = ()  # (dependency, variant, step) that every step downloads besides
    packaged: tuple = ()  # platforms whose build folder a second step promotes again


@dataclasses.dataclass(eq=False)
class Build:
    project: str
    number: int
    id: str
    url: str
    branch: str
    commit: str
    env: dict
    steps: list
    outcomes: dict = dataclasses.field(default_factory=dict)
    outputs: list = dataclasses.field(default_factory=list)  # its log lines and errors
    children: list = dataclasses.field(default_factory=list)

    @property
    def state(self) -> str:
        failed = {"hard_failed", "broken"} & set(self.outcomes.values())
        return "failed" if failed else "passed"

    @property
    def info(self) -> manifest.Build:
        return manifest.Build(self.id, self.number, self.url)

    def chain(self) -> list[Build]:
        """This build and every build it triggered, recursively."""
        return [self, *(b for c in self.children for b in c.chain())]


@dataclasses.dataclass(frozen=True)
class Compiled:
    build: Build
    folder: Folder


@dataclasses.dataclass(frozen=True)
class Download:
    """A step a job downloaded: the project, the folder, the key it took it at, and the build
    whose upload the manifest names there."""

    build: Build
    project: str
    folder: Folder
    key: str
    made_by: str


def agent_view(value):
    """Options as the agent reads them: every key upper case, ``-`` as ``_``."""
    if isinstance(value, dict):
        return {str(k).upper().replace("-", "_"): agent_view(v) for k, v in value.items()}
    if isinstance(value, list):
        return [agent_view(v) for v in value]
    return value


def artifacts_options(step: dict) -> dict | None:
    for plugin in step.get("plugins", []):
        for name, options in plugin.items():
            if name.split("#", 1)[0] == PLUGIN:
                return agent_view(options or {})
    return None


def dependencies(step: dict) -> list[tuple[str, bool]]:
    """``(key, allow_failure)`` of each step that ``step`` waits for."""
    found = step.get("depends_on") or []
    found = [found] if isinstance(found, str) else found
    return [
        (d, False) if isinstance(d, str) else (d["step"], bool(d.get("allow_failure")))
        for d in found
    ]


def entry_of(n: int, e: dict, build: Build, own: OwnKey) -> Entry:
    """The ``n``-th download entry as a job of ``build``, whose key ``own`` is, reads it: the
    options the agent hands it, checked and completed by the plugin."""
    steps = e.get("STEP") or []
    handed = {
        "variant": e["VARIANT"],
        "git_ref": e.get("GIT_REF"),
        "require_strict_ref_match": bool(e.get("REQUIRE_STRICT_REF_MATCH")),
        "project_id": e.get("PROJECT_ID"),
        "step": [steps] if isinstance(steps, str) else list(steps),
        "keys": {s.lower(): k for s, k in e.get("KEYS", {}).items()},
        "output_dir": ".",
        "extract": False,
        "files": e["FILES"],
        "exclude": [],
    }
    return download_entry(n, handed, build.project, "refs/heads/" + build.branch, lambda: own)


def made_by(m: Manifest) -> str:
    """The build whose bytes a folder holds: the one that promoted it, else the uploader."""
    return (m.promoted or m.uploaded).build.id


# The runner's gate: enter, and leave after every other step.
ENTER = {"key": "snapshot-enter"}
LEAVE = {"key": "snapshot-leave", "depends_on": [{"step": "snapshot-enter", "allow_failure": True}]}
FAILED = ("hard_failed", "broken")


class World:
    """Repositories of the projects of ``graph``, the store, and Buildkite playing builds.

    ``at_gate`` is what happens while a triggered build waits at its gate, after it
    arrived and before it plays: nothing, unless a test plays another chain there.
    """

    def __init__(self, directory, graph: dict, matrix: tuple, steps: tuple) -> None:
        self.directory = directory
        self.projects = {
            p: Project(tuple(deps), tuple(matrix), tuple(steps)) for p, deps in graph.items()
        }
        self.hosting = Hosting()
        self.git = FakeGit(self.hosting)
        self.store = MemoryStore()
        self.facts = {p: PipelineFacts(url(p), True) for p in graph}
        self.pipelines = FakePipelines(self.facts)
        self.agents = AgentState()
        self.broken: set[tuple[str, Folder]] = set()
        self.builds: list[Build] = []
        self.compiled: list[Compiled] = []
        self.downloads: list[Download] = []
        self.now = T0
        self.ids = itertools.count(1)
        self.numbers = collections.defaultdict(lambda: itertools.count(1))
        self.at_gate = lambda build: None
        for p in graph:
            self.commit(p)

    def clock(self) -> float:
        self.now += 1
        return float(self.now)

    # -- repositories ------------------------------------------------------

    def template(self, name: str) -> dict:
        p = self.projects[name]

        def entries(v):
            deps = [
                entry(d, v, p.pins.get(d, "{git_ref}"))
                for d in p.deps
                if v in self.projects[d].variants
            ]
            extra = [entry(d, x, p.pins.get(d, "{git_ref}"), step=s) for d, x, s in p.extra]
            return deps + extra + [entry(t, v, latest=True) for t in p.latest]

        first, *checked = p.steps
        built = [
            build_step(name, v, entries(v), step=s, promotes=s == first)
            for v in p.variants
            for s in p.steps
        ]
        checks = [check_step(name, v, s) for v in p.variants for s in checked]
        packaged = [promoted_again(name, v, entries(v)) for v in p.packaged]
        return {"steps": built + checks + packaged}

    def commit(self, name: str, branch: str = "master") -> str:
        """A new commit of the project's repository as its ``Project`` says, on ``branch``."""
        p = self.projects[name]
        text = yaml.safe_dump(self.template(name))
        files = {".buildkite/steps.yml": text, ".buildkite/pipeline.py": p.pipeline_py}
        return self.hosting.commit(name, files, branch)

    # -- builds ------------------------------------------------------------

    def build(self, project: str, branch: str = "master", **env: str) -> Build:
        """A build of ``project`` started by a person, played to its end."""
        return self.play(self.start(project, branch, None, env))

    def start(self, project: str, branch: str, commit: str | None, env: dict) -> Build:
        number, n = next(self.numbers[project]), next(self.ids)
        build = Build(
            project=project,
            number=number,
            id=f"01900000-0000-7000-8000-{n:012x}",
            url=f"https://buildkite.example.test/example/{project}/builds/{number}",
            branch=branch,
            commit=commit or self.hosting.head(project, branch),
            env=dict(env),
            steps=[dict(ENTER), dict(LEAVE)],
        )
        self.agents.meta_data[build.id]["snapshot-arrived"] = str(int(self.clock()))
        self.builds.append(build)
        return build

    def play(self, build: Build) -> Build:
        while (step := self.next_step(build)) is not None:
            outcome = "broken" if self.blocked(build, step) else self.run(build, step)
            build.outcomes[step["key"]] = self.agents.outcomes[build.id][step["key"]] = outcome
        return build

    def next_step(self, build: Build) -> dict | None:
        """The last step whose dependencies ended; leave once every other step ended.

        Buildkite may start ready steps in any order. The last first is one of
        them, and it runs a step that waits for less than it should before what
        it should have waited for.
        """
        pending = [s for s in build.steps if s["key"] not in build.outcomes]
        others = [s for s in pending if s["key"] != LEAVE["key"]]
        ready = [s for s in others if all(d in build.outcomes for d, _ in dependencies(s))]
        if others and not ready:
            raise AssertionError(f"{build.project} {build.number}: steps wait forever: {others}")
        return (ready[-1:] or pending or [None])[0]

    def blocked(self, build: Build, step: dict) -> bool:
        allowed = step.get("allow_dependency_failure")
        return any(
            build.outcomes[d] in FAILED and not (ok or allowed) for d, ok in dependencies(step)
        )

    def run(self, build: Build, step: dict) -> str:
        if step["key"] == "snapshot-enter":
            return self.gate(build, step, "plan", enter)
        if step["key"] == "snapshot-leave":
            return self.leave(build)
        if "trigger" in step:
            return self.trigger(build, step)
        options = artifacts_options(step)
        if options is not None and "RESUME" in options:
            return self.gate(build, step, "resume", resume)
        return self.job(build, options)

    def edge(self, build: Build) -> PlanEdge:
        return PlanEdge(
            store=self.store,
            prefix="",
            agent=BuildAgent(self.agents, build.id),
            checkout=FakeCheckout(self.hosting, build.commit),
            clock=self.clock,
            log=build.outputs.append,
            git=self.git,
            pipelines=self.pipelines,
            plugin_commit=lambda: PLUGIN_COMMIT,
        )

    def gate_job(self, build: Build, path) -> Job:
        return Job(
            pipeline=path,
            project=build.project,
            repository=url(build.project),
            build=build.info,
            branch=build.branch,
            command="generate",
            exit_status=0,
            plan_id=build.env.get("SNAPSHOT_PLAN"),
            needs=build.env.get("SNAPSHOT_NEEDS"),
            rebuild=build.env.get("SNAPSHOT_REBUILD"),
        )

    def outcome(self, build: Build, call) -> str:
        try:
            call()
        except ArtifactsError as exc:
            build.outputs.append(str(exc))
            return "hard_failed"
        return "passed"

    def gate(self, build: Build, step: dict, kind: str, decide) -> str:
        """Enter or resume: the command generates the pipeline, the plugin decides, and
        what it uploads joins the build after this step."""
        path = self.directory / f"{build.project}-{build.number}-{kind}.yml"
        text = self.hosting.trees[build.commit][".buildkite/steps.yml"].decode()
        path.write_text(generated(text, "refs/heads/" + build.branch))
        job = self.gate_job(build, str(path))
        uploads = self.agents.uploads[build.id]
        before = len(uploads)
        outcome = self.outcome(build, lambda: decide(job, self.edge(build)))
        added = [s for text in uploads[before:] for s in yaml.safe_load(text)["steps"]]
        at = build.steps.index(step) + 1
        build.steps[at:at] = added
        return outcome

    def leave(self, build: Build) -> str:
        """The leave step: its command fails when no plan step set a mode; its plugin records."""
        job = self.gate_job(build, None)
        outcome = self.outcome(build, lambda: leave(job, self.edge(build)))
        return outcome if "snapshot-mode" in self.agents.meta_data[build.id] else "hard_failed"

    def trigger(self, build: Build, step: dict) -> str:
        asked = step["build"]
        child = self.start(step["trigger"], asked["branch"], asked["commit"], asked.get("env", {}))
        build.children.append(child)
        self.at_gate(child)
        if self.play(child).state == "passed":
            return "passed"
        return "soft_failed" if step.get("soft_fail") else "hard_failed"

    def own_key(self, build: Build, options: dict) -> OwnKey:
        """The build's key: the plan step's; else, as a job computes it, a key of the build's
        own, whose key.json its upload creates."""
        if options.get("KEY"):
            return OwnKey(options["KEY"], None)
        stands_for = KeyJson(build.project, build.commit, (), build.id)
        return OwnKey(key_of(stands_for), stands_for)

    def capture(self, build: Build, own: OwnKey, folder: Folder) -> Manifest | None:
        """Capture what a job of ``build`` compiled for ``folder``; the promoted manifest that
        held the folder, else None."""
        location = layout.step_folder("", build.project, own.key, folder)
        name = f"out/{folder.platform}.bin"

        def write_files() -> None:
            if own.stands_for is not None:
                create_key_json(self.store, "", own.key, own.stands_for)
            self.store.put(f"{location}/{name}", build.id.encode(), "application/octet-stream")

        uploaded = captured([(name, len(build.id))], Uploaded(build.branch, build.info))
        return capture_step(self.store, location, write_files, uploaded)

    def job(self, build: Build, options: dict | None) -> str:
        """A job of the plugin: download; compile and capture its files when it uploads; then
        promote after a passed command, unless its upload found the folder promoted already,
        which makes it empty."""
        if options is None:  # a step completion put in place of one whose folders are made
            return "passed"
        own = self.own_key(build, options)
        for n, e in enumerate(options.get("DOWNLOAD", {}).get("PROJECTS", []), start=1):
            found = entry_of(n, e, build, own)
            for s in resolved_steps(self.store, "", found, f"{build.project} {found.variant}"):
                made = made_by(s.manifest)
                self.downloads.append(Download(build, s.project, s.folder, s.key, made))
        upload, promote = options.get("UPLOAD"), options.get("PROMOTE")
        uploads = None if upload is None else Folder(upload["VARIANT"], options["STEP"])
        promotes = (
            None
            if promote is None
            else Folder(promote["VARIANT"], promote.get("STEP") or options["STEP"])
        )
        held = None if uploads is None else self.capture(build, own, uploads)
        if (build.project, promotes) in self.broken:
            return "hard_failed"
        if uploads is not None:
            self.compiled.append(Compiled(build, uploads))
        if promotes is not None and held is None:
            # Read, not advanced: the margin of a shared failure counts the gate's ticks only.
            promotion = Promoted(iso_utc(float(self.now)), build.branch, build.info)
            promote_folder(self.store, "", build.project, own.key, promotes, promotion)
        return "passed"

    # -- what a test reads -------------------------------------------------

    def builds_of(self, project: str) -> list[Build]:
        return [b for b in self.builds if b.project == project]

    def plan(self, root: Build):
        return decode_plan(self.agents.meta_data[root.id]["snapshot-plan"])

    def made(self, project: str, key: str, folder: Folder) -> Manifest | None:
        """The manifest of ``folder`` of ``project`` at ``key`` when it says promoted."""
        m = read_manifest_at(self.store, layout.step_folder("", project, key, folder))
        return m if m is not None and m.promoted is not None else None

    def objects(self, prefix: str) -> dict[str, bytes]:
        return {k: b for k, b in self.store.objects.items() if k.startswith(prefix)}

    def records(self) -> list:
        found = (k.split("/") for k in self.store.objects)
        paths = [p for p in found if len(p) == 4 and p[2] == layout.FAILURES]
        return [decode_record(self.store.objects["/".join(p)].decode(), p[0], p[1]) for p in paths]

    def latest(self, project: str, ref: str, folder: Folder) -> str | None:
        found = self.store.get(layout.latest_file("", project, ref, folder))
        return None if found is None else found.decode()


# ---------------------------------------------------------------------------
# Fixtures: the dimensions of the planning half
# ---------------------------------------------------------------------------


@pytest.fixture(params=GRAPH_ROOTS, ids=str)
def graph_root(request) -> At:
    """Every root of every graph."""
    return request.param


@pytest.fixture(params=GRAPH_NODES, ids=str)
def graph_node(request) -> At:
    """Every node of every graph."""
    return request.param


@pytest.fixture(params=LOWER_NODES, ids=str)
def lower_node(request) -> At:
    """Every node of every graph that is a snapshot dependency of another."""
    return request.param


# The platforms every project builds and the step ids every platform makes, each value of
# each once: one platform of one step id, three platforms of two.
SHAPES = {
    f"{m}-{s}": (MATRICES[m], STEP_IDS[s]) for m, s in paired(sorted(MATRICES), sorted(STEP_IDS))
}


@pytest.fixture(params=sorted(SHAPES))
def shape(request) -> tuple[tuple, tuple]:
    """``(platforms, step ids)`` of every project."""
    return SHAPES[request.param]


@pytest.fixture
def worlds(tmp_path):
    """Make a world: ``worlds(graph, matrix, steps)``."""
    count = itertools.count(1)

    def make(graph: dict, matrix: tuple = ("linux",), steps: tuple = ("build",)) -> World:
        directory = tmp_path / f"world-{next(count)}"
        directory.mkdir()
        return World(directory, graph, matrix, steps)

    return make
