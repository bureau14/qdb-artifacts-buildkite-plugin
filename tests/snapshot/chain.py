"""The chain's world: projects generated from a graph and a platform matrix, and a
simulated Buildkite that plays their builds in process, over the fakes of ``fakes.py``.

Every project is a repository with a step template and a ``pipeline.py`` of the
runner's shape. Its template has one step per variant, which downloads the same
variant of each snapshot dependency, uploads and promotes it.

A build starts with the runner's gate. The gate steps run the library's own
decisions: ``enter`` and ``resume`` over a ``PlanEdge`` of the fakes, ``leave``
over its ``Edge``. A trigger step starts the build it asks for, with its branch,
commit and environment, and plays it to its end. A command step is a simulated
job of the plugin: it records which build each download names, compiles (or
fails, when the test broke that step), and promotes as a job does:
``LATEST_SUCCESSFUL``, then the snapshot pointer through ``lib.pointer``.
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

from lib import layout, pointer
from lib.errors import ArtifactsError
from lib.forms import iso_utc
from lib.snapshot.buildkite import PipelineFacts
from lib.snapshot.enter import PlanEdge, enter, resume
from lib.snapshot.job import Job
from lib.snapshot.leave import leave
from lib.snapshot.plan import decode_plan
from lib.snapshot.records import decode_record

PLUGIN = "bureau14/qdb-artifacts"
PLUGIN_COMMIT = "a" * 40
LATEST = "LATEST_SUCCESSFUL"
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


def entry(project: str, variant: str, ref="{git_ref}", latest=False, spelling="snake") -> dict:
    """A download entry; a literal ``ref`` is a pin, ``latest`` names LATEST_SUCCESSFUL."""
    s = SPELLINGS[spelling]
    e = {s("project_id"): project, "variant": variant, s("git_ref"): ref, "files": ["*.tar.zst"]}
    return {**e, s("build_id"): LATEST} if latest else e


def build_step(project: str, variant: str, entries: list, spelling="snake") -> dict:
    """The step that builds ``variant`` from ``entries``, uploads and promotes it."""
    s = SPELLINGS[spelling]
    options = {
        "upload": {"variant": variant, s("git_ref"): "{git_ref}", "files": "out/*"},
        "promote": {"variant": variant, s("git_ref"): "{git_ref}"},
    }
    if entries:
        options = {"download": {"projects": entries}, **options}
    return {
        "label": f"{project} {variant}",
        "key": f"build-{variant}",
        "command": "make",
        "plugins": [{f"{PLUGIN}#master": options}],
    }


def generated(template_text: str, ref: str) -> str:
    """What the project's generator prints for a build of ``ref``."""
    return template_text.replace("{git_ref}", ref)


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Project:
    """What a project's repository says: its dependencies, variants and how it adopted."""

    deps: tuple
    variants: tuple
    pins: dict = dataclasses.field(default_factory=dict)  # dependency -> literal ref
    latest: tuple = ()  # projects it takes as latest successful
    pipeline_py: bytes = SHAPED
    template_text: str | None = None  # replaces the generated template


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
    def info(self) -> pointer.Build:
        return pointer.Build(self.id, self.number, self.url)

    def chain(self) -> list[Build]:
        """This build and every build it triggered, recursively."""
        return [self, *(b for c in self.children for b in c.chain())]


@dataclasses.dataclass(frozen=True)
class Compiled:
    build: Build
    variant: str


@dataclasses.dataclass(frozen=True)
class Download:
    build: Build
    project: str
    variant: str
    build_id: str | None


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


# The runner's gate: enter, and leave after every other step.
ENTER = {"key": "snapshot-enter"}
LEAVE = {"key": "snapshot-leave", "depends_on": [{"step": "snapshot-enter", "allow_failure": True}]}
FAILED = ("hard_failed", "broken")


class World:
    """Repositories of the projects of ``graph``, the store, and Buildkite playing builds."""

    def __init__(self, directory, graph: dict, matrix: tuple) -> None:
        self.directory = directory
        self.projects = {p: Project(tuple(deps), tuple(matrix)) for p, deps in graph.items()}
        self.hosting = Hosting()
        self.git = FakeGit(self.hosting)
        self.store = MemoryStore()
        self.facts = {p: PipelineFacts(p, url(p), True) for p in graph}
        self.pipelines = FakePipelines(self.facts)
        self.agents = AgentState()
        self.broken: set[tuple[str, str]] = set()
        self.builds: list[Build] = []
        self.compiled: list[Compiled] = []
        self.downloads: list[Download] = []
        self.now = T0
        self.ids = itertools.count(1)
        self.numbers = collections.defaultdict(lambda: itertools.count(1))
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
            return deps + [entry(t, v, latest=True) for t in p.latest]

        return {"steps": [build_step(name, v, entries(v)) for v in p.variants]}

    def commit(self, name: str, branch: str = "master") -> str:
        """A new commit of the project's repository as its ``Project`` says, on ``branch``."""
        p = self.projects[name]
        text = p.template_text or yaml.safe_dump(self.template(name))
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

    def gate_job(self, build: Build, kind: str, step_key: str, path) -> Job:
        return Job(
            kind=kind,
            pipeline=path,
            step_key=step_key,
            project=build.project,
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
        job = self.gate_job(build, kind, step["key"], str(path))
        uploads = self.agents.uploads[build.id]
        before = len(uploads)
        outcome = self.outcome(build, lambda: decide(job, self.edge(build)))
        added = [s for text in uploads[before:] for s in yaml.safe_load(text)["steps"]]
        at = build.steps.index(step) + 1
        build.steps[at:at] = added
        return outcome

    def leave(self, build: Build) -> str:
        """The leave step: its command fails when no plan step set a mode; its plugin records."""
        job = self.gate_job(build, "leave", "snapshot-leave", None)
        outcome = self.outcome(build, lambda: leave(job, self.edge(build)))
        return outcome if "snapshot-mode" in self.agents.meta_data[build.id] else "hard_failed"

    def trigger(self, build: Build, step: dict) -> str:
        asked = step["build"]
        child = self.start(step["trigger"], asked["branch"], asked["commit"], asked.get("env", {}))
        build.children.append(child)
        if self.play(child).state == "passed":
            return "passed"
        return "soft_failed" if step.get("soft_fail") else "hard_failed"

    def job(self, build: Build, options: dict | None) -> str:
        """A job of the plugin: download, compile, then promote after a passed command."""
        if options is None:  # a step completion put in place of one whose variants exist
            return "passed"
        for e in options.get("DOWNLOAD", {}).get("PROJECTS", []):
            project = e.get("PROJECT_ID") or build.project
            self.downloads.append(Download(build, project, e["VARIANT"], self.resolved(build, e)))
        variant = (options.get("UPLOAD") or options["PROMOTE"])["VARIANT"]
        if (build.project, variant) in self.broken:
            return "hard_failed"
        self.compiled.append(Compiled(build, variant))
        if "PROMOTE" in options:
            self.promote(build, options["PROMOTE"])
        return "passed"

    def resolved(self, build: Build, e: dict) -> str | None:
        """The build a download entry gets, as the job resolves it."""
        project = e.get("PROJECT_ID") or build.project
        ref = e["GIT_REF"]
        own = project == build.project and ref == "refs/heads/" + build.branch
        asked = e.get("BUILD_ID") or (build.id if own else LATEST)
        if asked != LATEST:
            return asked
        for r in dict.fromkeys((ref, "refs/heads/master", "refs/heads/main")):
            found = self.store.get(layout.latest_pointer("", project, r, e["VARIANT"]))
            if found is not None:
                return found.body.decode()
        return None

    def promote(self, build: Build, block: dict) -> None:
        ref, variant = block["GIT_REF"], block["VARIANT"]
        where = layout.latest_pointer("", build.project, ref, variant)
        self.store.put(where, build.id.encode(), "text/plain")
        if block.get("SNAPSHOT"):
            made = pointer.Pointer(
                build.project,
                block["SNAPSHOT"],
                variant,
                ref,
                build.commit,
                build.info,
                iso_utc(self.clock()),
            )
            pointer.create(self.store, "", made)

    # -- what a test reads -------------------------------------------------

    def builds_of(self, project: str) -> list[Build]:
        return [b for b in self.builds if b.project == project]

    def plan(self, root: Build):
        return decode_plan(self.agents.meta_data[root.id]["snapshot-plan"])

    def pointer(self, project: str, key: str, variant: str) -> pointer.Pointer | None:
        return pointer.find(self.store, "", project, key, variant)

    def objects(self, prefix: str) -> dict[str, bytes]:
        return {k: b for k, b in self.store.objects.items() if k.startswith(prefix)}

    def records(self) -> list:
        return [decode_record(b.decode()) for b in self.objects("_snapshot/v1/failures/").values()]

    def latest(self, project: str, ref: str, variant: str) -> str | None:
        found = self.store.get(layout.latest_pointer("", project, ref, variant))
        return None if found is None else found.body.decode()


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


@pytest.fixture(params=sorted(MATRICES))
def matrix(request) -> tuple:
    """The variants every project builds: one platform, or three."""
    return MATRICES[request.param]


@pytest.fixture
def worlds(tmp_path):
    """Make a world: ``worlds(graph, matrix)``."""
    count = itertools.count(1)

    def make(graph: dict, matrix: tuple = ("linux",)) -> World:
        directory = tmp_path / f"world-{next(count)}"
        directory.mkdir()
        return World(directory, graph, matrix)

    return make
