"""The world of the builds of a tree: projects generated from a graph, a platform matrix and
the step ids of every platform, and a simulated Buildkite that plays their builds in process,
over the fakes of ``fakes.py``.

Every project is a repository with a step template and a ``pipeline.py`` of the
runner's shape. Its template has, per platform, one step per step id, which writes
``step_id``, downloads the same platform of each snapshot dependency without naming a
step, and uploads. A step promotes what it uploaded, but one of a step id of ``CHECKED``,
which a check step promotes that waits for it and names it in its promote block. A test
may add a step that promotes a folder again (``Project.packaged``), and downloads of the
project's own steps at other branches (``Project.elsewhere``). The project's
generator does what the runner's ``load_template`` does: the key
``<step id>-<platform>``, and the plugin's ``step``.

A build starts with the runner's plan step and last step. They run the library's own
decisions over an ``Edge`` of the fakes: the plan step and its second run (``plan_step``,
``plan_step_again``), and the last step (``last_step``). A trigger step starts the build it
asks for, with its branch, commit and environment, and plays it to its end. A command step is
a simulated job of the plugin, through the library's own functions: it reads its options
(``options.options_of``), finds the steps each download entry takes (``resolve.entry_steps``),
compiles and captures its files when it uploads (``upload.capture_step``), and promotes
after a passed command (``promote.promote_folder``), unless its upload found the folder
promoted, moving its branch's latest file as its build's ``SNAPSHOT_BRANCH_BUILD`` says. The
job of the step that promotes a folder the test broke fails.
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

from lib import job, layout
from lib.codec import decoded
from lib.errors import ArtifactsError
from lib.forms import branch_ref, iso_utc
from lib.job import Job
from lib.key import KeyJson, OwnKey, create_key_json, key_of
from lib.layout import Folder
from lib.manifest import Manifest, Promoted, Uploaded, captured, read_manifest_at
from lib.options import Options, options_of, promoted_folder, uploaded_folder
from lib.promote import promote_folder
from lib.resolve import entry_steps
from lib.snapshot.buildkite import PipelineFacts
from lib.snapshot.edge import Edge
from lib.snapshot.last_step import last_step
from lib.snapshot.pipeline import is_plugin_key
from lib.snapshot.plan import decode_plan
from lib.snapshot.plan_step import plan_step, plan_step_again
from lib.snapshot.records import Record
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
    # The left arm is a project longer, so that a pin right writes can move core, two levels
    # below left, where left's own build does not see that pin.
    "diamond": {
        "app": ("left", "right"),
        "left": ("mid",),
        "mid": ("core",),
        "right": ("core",),
        "core": (),
    },
    "wide": {"app": ("one", "three", "two"), "one": (), "three": (), "two": ()},
    "real": REAL,
}
MATRICES = {"linux": ("linux",), "three": ("linux", "macos", "windows")}
# The step ids every platform of a project makes: one; two, of which a check step promotes the
# second; or two that each promote themselves, as the archives of a packaging pipeline do.
STEP_IDS = {
    "build": ("build",),
    "build-notes": ("build", "notes"),
    "each-promoted": ("api", "server"),
}
# The step ids a check step promotes, after the step that uploads them; the step that uploads
# any other promotes it itself.
CHECKED = ("notes",)


def below(graph: dict, p: str) -> frozenset[str]:
    """Every project ``p`` reaches through its snapshot dependencies."""
    return frozenset(graph[p]).union(*(below(graph, d) for d in graph[p]))


def above(graph: dict, p: str) -> frozenset[str]:
    """Every project that reaches ``p``."""
    return frozenset(q for q in graph if p in below(graph, q))


def tops(graph: dict) -> list[str]:
    """Every project no project depends on."""
    return sorted(p for p in graph if not above(graph, p))


def tree(graph: dict, p: str) -> frozenset[str]:
    return below(graph, p) | {p}


def folders(matrix: tuple, steps: tuple) -> set[Folder]:
    """Every folder of a project of ``matrix`` and ``steps``."""
    return {Folder(v, s) for v in matrix for s in steps}


@dataclasses.dataclass(frozen=True)
class At:
    """A project of a named graph, and the project of its root build: the first top of the
    graph whose tree holds it."""

    name: str
    project: str

    @property
    def graph(self) -> dict:
        return GRAPHS[self.name]

    @property
    def root(self) -> str:
        return next(t for t in tops(self.graph) if self.project in tree(self.graph, t))

    def __str__(self) -> str:
        return f"{self.name}-{self.project}"


GRAPH_TOPS = [At(g, t) for g in sorted(GRAPHS) for t in tops(GRAPHS[g])]
GRAPH_PROJECTS = [At(g, p) for g in sorted(GRAPHS) for p in sorted(GRAPHS[g])]
LOWER_PROJECTS = [a for a in GRAPH_PROJECTS if a.project not in tops(a.graph)]


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

SPELLINGS = {"snake": lambda k: k, "kebab": lambda k: k.replace("_", "-")}


def entry(
    project: str, ref="{git_ref}", latest=False, spelling="snake", step=None, variant=None
) -> dict:
    """A download entry; a literal ``ref`` is a pin, ``latest`` takes the latest successful
    build, ``step`` names the steps it takes. It takes the platform of its step, which the
    generator writes, unless it names a ``variant``."""
    s = SPELLINGS[spelling]
    e = {s("project_id"): project, s("git_ref"): ref, "files": ["*.tar.zst"]}
    named = {k: v for k, v in (("step", step), ("variant", variant)) if v is not None}
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
    ``plugin``. As a repository's template, it leaves each block's ``variant`` to the
    generator, which makes the step for the platform ``slug``."""
    options = {"upload": {"files": "out/*"}}
    if promotes:
        options = {**options, "promote": {}}
    if entries:
        options = {"download": {"projects": entries}, **options}
    return {
        "label": f"{project} {step} {variant}",
        "slug": variant,
        "step_id": step,
        "command": "make",
        "plugins": [{f"{plugin}#master": options}],
    }


def check_step(project: str, variant: str, step: str) -> dict:
    """The template step that checks what the step of step id ``step`` uploaded for ``variant``,
    after it, and promotes it: a step of a key of its own, which names no step id of its own."""
    return {
        "label": f"{project} check {step} {variant}",
        "slug": variant,
        "key": f"check-{step}-{variant}",
        "depends_on": f"{step}-{variant}",
        "command": "check",
        "plugins": [{f"{PLUGIN}#master": {"promote": {"step": step}}}],
    }


def promoted_again(project: str, variant: str, step: str, entries: list) -> dict:
    """A step that promotes the folder ``variant``/``step`` a second time, written as a
    pipeline without ``pipeline.py`` writes it: its key, and the plugin's ``step`` and each
    block's ``variant`` by hand."""
    options = {
        "step": step,
        "upload": {"variant": variant, "files": "out/*"},
        "promote": {"variant": variant},
    }
    if entries:
        options = {"download": {"projects": entries}, **options}
    return {
        "label": f"{project} package {variant}",
        "slug": variant,
        "key": f"package-{variant}",
        "command": "make",
        "plugins": [{f"{PLUGIN}#master": options}],
    }


def with_variant(options: dict, variant: str) -> dict:
    """The plugin's ``options`` with ``variant`` in every block and download entry that writes
    none, as the runner's ``set_artifact_plugin_options`` gives a step its platform."""
    blocks = {k: {"variant": variant, **options[k]} for k in ("upload", "promote") if k in options}
    entries = [{"variant": variant, **e} for e in options.get("download", {}).get("projects", [])]
    download = {"download": {**options["download"], "projects": entries}} if entries else {}
    return {**options, **blocks, **download}


def loaded(step: dict) -> dict:
    """A template step as the generator makes it for the platform ``slug``: that platform as
    the ``variant`` its blocks leave out and, as the runner's ``load_template`` gives a step
    that writes ``step_id``, the key ``<step id>-<slug>`` and ``step: <step id>`` at the top
    of every configuration of the plugin. A step without ``slug`` is written in full."""
    if "slug" not in step:
        return step
    slug, step_id = step["slug"], step.get("step_id")
    named = {} if step_id is None else {"step": step_id}
    plugins = [
        {
            name: with_variant({**named, **options}, slug) if is_plugin_key(name) else options
            for name, options in p.items()
        }
        for p in step["plugins"]
    ]
    kept = {k: v for k, v in step.items() if k not in ("slug", "step_id", "plugins")}
    key = {} if step_id is None else {"key": f"{step_id}-{slug}"}
    return {**kept, **key, "plugins": plugins}


def generated(template_text: str, ref: str) -> str:
    """What the project's generator prints for a build of ``ref``."""
    doc = yaml.safe_load(template_text.replace("{git_ref}", ref))
    return yaml.safe_dump({"steps": [loaded(s) for s in doc["steps"]]})


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Project:
    """What a project's repository says: its dependencies, platforms, step ids and its
    ``pipeline.py``."""

    deps: tuple
    variants: tuple
    steps: tuple = ("build",)
    pins: dict = dataclasses.field(default_factory=dict)  # dependency -> literal ref
    latest: tuple = ()  # projects it takes as latest successful
    latest_step: list | None = None  # the steps those downloads name; None for every step
    pipeline_py: bytes = SHAPED
    extra: tuple = ()  # (dependency, variant, step) that every step downloads besides
    elsewhere: tuple = ()  # (step, ref): every step downloads its own platform of it at that ref
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
    def info(self) -> job.Build:
        return job.Build(self.id, self.number, self.url)

    def tree(self) -> list[Build]:
        """This build and every build it triggered, recursively: the builds of its tree."""
        return [self, *(b for c in self.children for b in c.tree())]


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


def artifacts_options(step: dict) -> Options | None:
    """The plugin's options of a step, as its job reads them."""
    for plugin in step.get("plugins", []):
        for name, options in plugin.items():
            if name.split("#", 1)[0] == PLUGIN:
                return options_of(options)
    return None


def dependencies(step: dict) -> list[tuple[str, bool]]:
    """``(key, allow_failure)`` of each step that ``step`` waits for."""
    found = step.get("depends_on") or []
    found = [found] if isinstance(found, str) else found
    return [
        (d, False) if isinstance(d, str) else (d["step"], bool(d.get("allow_failure")))
        for d in found
    ]


def made_by(m: Manifest) -> str:
    """The build whose bytes a folder holds: the one that promoted it, else the uploader."""
    return (m.promoted or m.uploaded).build.id


# The runner's plan step, and the last step after every other step.
PLAN_STEP = {"key": "snapshot-enter"}
LAST_STEP = {
    "key": "snapshot-leave",
    "depends_on": [{"step": "snapshot-enter", "allow_failure": True}],
}
FAILED = ("hard_failed", "broken")


class World:
    """Repositories of the projects of ``graph``, the store, and Buildkite playing builds.

    ``at_plan_step`` is what happens while a triggered build waits at its plan step, after
    it arrived and before it plays: nothing, unless a test plays the builds of another tree
    there.
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
        self.ran: list[Compiled] = []  # each folder a job makes, also one that only promotes
        self.downloads: list[Download] = []
        self.now = T0
        self.ids = itertools.count(1)
        self.numbers = collections.defaultdict(lambda: itertools.count(1))
        self.at_plan_step = lambda build: None
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
                entry(d, p.pins.get(d, "{git_ref}"))
                for d in p.deps
                if v in self.projects[d].variants
            ]
            extra = [entry(d, p.pins.get(d, "{git_ref}"), step=s, variant=x) for d, x, s in p.extra]
            own = [entry(name, ref, step=s) for s, ref in p.elsewhere]
            latest = [entry(t, latest=True, step=p.latest_step) for t in p.latest]
            return deps + extra + own + latest

        built = [
            build_step(name, v, entries(v), step=s, promotes=s not in CHECKED)
            for v in p.variants
            for s in p.steps
        ]
        checks = [check_step(name, v, s) for v in p.variants for s in p.steps if s in CHECKED]
        packaged = [promoted_again(name, v, p.steps[0], entries(v)) for v in p.packaged]
        return {"steps": built + checks + packaged}

    def commit(self, name: str, branch: str | None = "master") -> str:
        """A new commit of the project's repository as its ``Project`` says, on ``branch``, or
        on no branch."""
        p = self.projects[name]
        text = yaml.safe_dump(self.template(name))
        files = {".buildkite/steps.yml": text, ".buildkite/pipeline.py": p.pipeline_py}
        return self.hosting.commit(name, files, branch)

    def tag(self, name: str, tag: str) -> str:
        """A new commit of the project's repository on no branch, and the tag ``tag`` put on it;
        the commit."""
        commit = self.commit(name, branch=None)
        self.hosting.tag(name, tag, commit)
        return commit

    # -- builds ------------------------------------------------------------

    def build(
        self, project: str, branch: str = "master", commit: str | None = None, **env: str
    ) -> Build:
        """A build of ``project`` started by a person under the name ``branch``, played to its
        end: of ``commit``, else of the tip of the branch, else of the tag, of that name."""
        return self.play(self.start(project, branch, commit, env))

    def start(self, project: str, branch: str, commit: str | None, env: dict) -> Build:
        number, n = next(self.numbers[project]), next(self.ids)
        build = Build(
            project=project,
            number=number,
            id=f"01900000-0000-7000-8000-{n:012x}",
            url=f"https://buildkite.example.test/example/{project}/builds/{number}",
            branch=branch,
            commit=commit or self.hosting.checked_out(project, branch),
            env=dict(env),
            steps=[dict(PLAN_STEP), dict(LAST_STEP)],
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
        """The ready step furthest down the pipeline, whose dependencies ended; the last step
        once every other step ended.

        Buildkite may start ready steps in any order. The one furthest down first
        is one of them, and it runs a step that waits for less than it should
        before what it should have waited for.
        """
        pending = [s for s in build.steps if s["key"] not in build.outcomes]
        others = [s for s in pending if s["key"] != LAST_STEP["key"]]
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
            return self.plan_step_run(build, step, "plan", plan_step)
        if step["key"] == "snapshot-leave":
            return self.last_step_run(build)
        if "trigger" in step:
            return self.trigger(build, step)
        options = artifacts_options(step)
        if options is not None and options.resume:
            return self.plan_step_run(build, step, "resume", plan_step_again)
        return self.job(build, options)

    def git_at(self, commit: str) -> FakeGit:
        """Git in a checkout of ``commit``, whose questions join the world's (``git.asked``)."""
        return dataclasses.replace(self.git, head=commit)

    def edge(self, build: Build) -> Edge:
        return Edge(
            store=self.store,
            agent=BuildAgent(self.agents, build.id),
            checkout=FakeCheckout(self.hosting, build.commit),
            git=self.git_at(build.commit),
            pipelines=self.pipelines,
            plugin_commit=lambda: PLUGIN_COMMIT,
            clock=self.clock,
            log=build.outputs.append,
        )

    def job_of(self, build: Build) -> Job:
        """The job of a step of ``build``, whose command passed."""
        return Job(
            project=build.project,
            repository=url(build.project),
            branch=build.branch,
            tag=None,
            build=build.info,
            command="download",
            exit_status=0,
            root_build=build.env.get("SNAPSHOT_PLAN"),
            needs=build.env.get("SNAPSHOT_NEEDS"),
            rebuild=build.env.get("SNAPSHOT_REBUILD"),
            writes_latest_files=build.env.get("SNAPSHOT_BRANCH_BUILD") != "false",
        )

    def outcome(self, build: Build, call) -> str:
        try:
            call()
        except ArtifactsError as exc:
            build.outputs.append(str(exc))
            return "hard_failed"
        return "passed"

    def plan_step_run(self, build: Build, step: dict, kind: str, decide) -> str:
        """The plan step or its second run: the command puts the generated pipeline in the
        checkout, the plugin decides, and what it uploads joins the build after this step."""
        path = self.directory / f"{build.project}-{build.number}-{kind}.yml"
        text = self.hosting.trees[build.commit][".buildkite/steps.yml"].decode()
        path.write_text(generated(text, branch_ref(build.branch)))
        uploads = self.agents.uploads[build.id]
        before = len(uploads)
        decided = lambda: decide(self.job_of(build), self.edge(build), str(path))
        outcome = self.outcome(build, decided)
        added = [s for text in uploads[before:] for s in yaml.safe_load(text)["steps"]]
        at = build.steps.index(step) + 1
        build.steps[at:at] = added
        return outcome

    def last_step_run(self, build: Build) -> str:
        """The last step: its command fails when no plan step set a mode; its plugin records."""
        outcome = self.outcome(build, lambda: last_step(self.job_of(build), self.edge(build)))
        return outcome if "snapshot-mode" in self.agents.meta_data[build.id] else "hard_failed"

    def trigger(self, build: Build, step: dict) -> str:
        asked = step["build"]
        child = self.start(step["trigger"], asked["branch"], asked["commit"], asked.get("env", {}))
        build.children.append(child)
        self.at_plan_step(child)
        if self.play(child).state == "passed":
            return "passed"
        return "soft_failed" if step.get("soft_fail") else "hard_failed"

    def own_key(self, build: Build, options: Options) -> OwnKey:
        """The build's key: the plan step's; else, as a job computes it, a key of the build's
        own, whose key.json its upload creates."""
        if options.key:
            return OwnKey(options.key, None)
        stands_for = KeyJson(build.project, build.commit, (), build.id)
        return OwnKey(key_of(stands_for), stands_for)

    def capture(self, build: Build, own: OwnKey, folder: Folder) -> Manifest | None:
        """Capture what a job of ``build`` compiled for ``folder``; the promoted manifest that
        held the folder, else None."""
        location = layout.step_folder("", build.project, own.key, folder)
        name = f"out/{folder.platform}.bin"

        def write_files() -> None:
            if own.stands_for is not None:
                create_key_json(self.store, own.key, own.stands_for)
            self.store.put(f"{location}/{name}", build.id.encode(), "application/octet-stream")

        uploaded = captured([(name, len(build.id))], Uploaded(build.branch, build.info))
        return capture_step(self.store, location, write_files, uploaded)

    def job(self, build: Build, options: Options | None) -> str:
        """A job of the plugin: download; compile and capture its files when it uploads; then
        promote after a passed command, unless its upload found the folder promoted already,
        which makes it empty."""
        if options is None:  # a step completion put in place of one whose folders are made
            return "passed"
        own = self.own_key(build, options)
        job = self.job_of(build)
        for e in options.download.projects if options.download else ():
            name = f"{build.project} {e.variant}"
            for s in entry_steps(self.store, e, job, lambda: own.key, name):
                made = made_by(s.manifest)
                self.downloads.append(Download(build, s.project, s.folder, s.key, made))
        uploads = uploaded_folder(options) if options.upload else None
        promotes = promoted_folder(options) if options.promote else None
        held = None if uploads is None else self.capture(build, own, uploads)
        if (build.project, promotes) in self.broken:
            return "hard_failed"
        if uploads is not None:
            self.compiled.append(Compiled(build, uploads))
        self.ran.extend(Compiled(build, f) for f in (uploads, promotes) if f is not None)
        if promotes is not None and held is None:
            # Read, not advanced: the margin of a shared failure counts the plan step's ticks only.
            promotion = Promoted(iso_utc(float(self.now)), build.branch, build.info)
            writes = job.writes_latest_files
            promote_folder(self.store, build.project, own.key, promotes, promotion, writes)
        return "passed"

    # -- what a test reads -------------------------------------------------

    def builds_of(self, project: str) -> list[Build]:
        return [b for b in self.builds if b.project == project]

    def plan(self, root_build: Build):
        return decode_plan(self.agents.meta_data[root_build.id]["snapshot-plan"])

    def made(self, project: str, key: str, folder: Folder) -> Manifest | None:
        """The manifest of ``folder`` of ``project`` at ``key`` when it says promoted."""
        m = read_manifest_at(self.store, layout.step_folder("", project, key, folder))
        return m if m is not None and m.promoted is not None else None

    def objects(self, prefix: str) -> dict[str, bytes]:
        return {k: b for k, b in self.store.objects.items() if k.startswith(prefix)}

    def records(self) -> list:
        found = (k.split("/") for k in self.store.objects)
        paths = [p for p in found if len(p) == 4 and p[2] == layout.FAILURES]
        return [decoded(Record, self.store.objects["/".join(p)]) for p in paths]

    def latest(self, project: str, ref: str, folder: Folder) -> str | None:
        found = self.store.get(layout.latest_file("", project, ref, folder))
        return None if found is None else found.decode()


# ---------------------------------------------------------------------------
# Fixtures: the dimensions of the planning half
# ---------------------------------------------------------------------------


@pytest.fixture(params=GRAPH_TOPS, ids=str)
def graph_root(request) -> At:
    """The project of every root build: every top of every graph."""
    return request.param


@pytest.fixture(params=GRAPH_PROJECTS, ids=str)
def graph_project(request) -> At:
    """Every project of every graph."""
    return request.param


@pytest.fixture(params=LOWER_PROJECTS, ids=str)
def lower_project(request) -> At:
    """Every project of every graph that is a snapshot dependency of another."""
    return request.param


# The platforms every project builds and the step ids every platform makes, each value of
# each once, without the cross product.
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
