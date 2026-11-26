"""What the plan step and the last step read and change (``Edge``), and the reads and writes
they share.

The steps keep no state but meta-data of their own build:

    snapshot-plan       the plan, on the root build
    snapshot-outcome    reused, built or failed
    snapshot-promotes   [[<platform>, <step id>, <Buildkite key of the step that promotes it>]]

``snapshot-arrived``, the time a build began, is set by the runner in every build's first step.
"""

from __future__ import annotations

import functools
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import layout
from ..codec import decoded, encoded
from ..config import WRITE, env_or_ssm
from ..errors import ArtifactsError
from ..job import Job, checkout_commit
from ..key import create_key_json
from ..layout import Folder
from ..log import log
from ..manifest import Manifest, is_made, read_manifest_at
from ..objects import open_destination
from .agent import RealAgent
from .buildkite import PipelineFacts, fetch_pipeline
from .git import RealGit
from .plan import Plan, Planned, decode_plan, key_json_of, planned_project, tree_of
from .records import Cause, Origin, Record, failure_in_plan
from .states import Held

OUTCOME = "snapshot-outcome"
REUSED, BUILT, FAILED = "reused", "built", "failed"
PROMOTES = "snapshot-promotes"
PLAN = "snapshot-plan"
ARRIVED = "snapshot-arrived"


TOKEN_VARIABLE = "ARTIFACTS_BUILDKITE_TOKEN"
TOKEN_SSM_PARAM = "/services/buildkite/credentials/artifacts/buildkite-api-token"

# The plugin's own checkout: lib/planning/edge.py is two levels below it.
PLUGIN_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Edge:
    """``store``: the destination's store, as a job opens it. ``agent``:
    ``buildkite-agent``. ``checkout``: the build's own checkout. ``git``: other repositories.
    ``pipelines(slug)``: Buildkite's facts of a pipeline. ``plugin_commit()``: the commit of
    the plugin this step runs. ``clock``: epoch seconds. ``log``: one line to the job log."""

    store: Any
    agent: Any
    checkout: Any
    git: Any
    pipelines: Callable[[str], PipelineFacts]
    plugin_commit: Callable[[], str]
    clock: Callable[[], float]
    log: Callable[[str], None]


def buildkite_pipelines(ssm: Any) -> Callable[[str], PipelineFacts]:
    """Buildkite's facts of a pipeline slug. The organisation and the token
    (``ARTIFACTS_BUILDKITE_TOKEN``, else its SSM parameter) are read at the first call, which
    only the plan of a root build makes. The token goes to Buildkite's API only."""

    @functools.cache
    def credentials() -> tuple[str, str]:
        # A parameter written from a file ends with a newline.
        token = (env_or_ssm(ssm, TOKEN_VARIABLE, TOKEN_SSM_PARAM) or "").strip()
        if not token:
            raise ArtifactsError(
                f"no Buildkite API token: set {TOKEN_VARIABLE} or the SSM parameter {TOKEN_SSM_PARAM}"
            )
        return os.environ["BUILDKITE_ORGANIZATION_SLUG"], token

    return lambda slug: fetch_pipeline(slug, *credentials())


def real_edge() -> Edge:
    """The edge of a step on an agent."""
    at = open_destination(WRITE)
    git = RealGit(log=log)
    return Edge(
        store=at.store,
        agent=RealAgent(),
        checkout=git,
        git=git,
        pipelines=buildkite_pipelines(at.ssm),
        plugin_commit=lambda: checkout_commit(str(PLUGIN_ROOT)),
        clock=time.time,
        log=log,
    )


# ---------------------------------------------------------------------------
# Manifests and failure records
# ---------------------------------------------------------------------------


def manifests_at(
    edge: Edge, project: str, key: str, folders: Iterable[Folder]
) -> dict[Folder, Manifest | None]:
    """The manifest of each folder of ``project`` at ``key``; None where no upload wrote one."""
    return {
        f: read_manifest_at(edge.store, layout.step_folder(edge.store.prefix, project, key, f))
        for f in sorted(folders)
    }


def made_only(found: Mapping[Any, Manifest | None]) -> dict[Any, Manifest]:
    """The manifests of ``found`` that say promoted."""
    return {k: m for k, m in found.items() if is_made(m)}


def stored_records(edge: Edge, project: str, key: str) -> list[Record]:
    listed = edge.store.list(layout.failure_records(edge.store.prefix, project, key))
    found = (edge.store.get(item.key) for item in listed)
    return [decoded(Record, b) for b in found if b is not None]


def folders_at(edge: Edge, project: str, key: str) -> list[Folder]:
    """Every folder of ``project`` that holds an object at ``key``."""
    at = layout.at_key(edge.store.prefix, project, key)
    platforms = [p for p in edge.store.below(at) if p != layout.FAILURES]
    return [Folder(p, s) for p in platforms for s in edge.store.below(f"{at}{p}/")]


def held_at(edge: Edge, project: str, n: Planned, asked: Iterable[Folder]) -> Held:
    """What the store holds of ``project`` at its planned key, of the folders ``asked`` of it
    and of those found there."""
    manifests = manifests_at(edge, project, n.key, {*asked, *folders_at(edge, project, n.key)})
    keys = edge.store.below(layout.keys_of_commit(edge.store.prefix, project, n.commit))
    records = tuple(stored_records(edge, project, n.key))
    return Held(manifests, records, built_before=any(k != n.key for k in keys))


def held_of_tree(
    edge: Edge, p: Plan, project: str, asked: Mapping[str, Iterable[Folder]]
) -> dict[str, Held]:
    """``held_at`` of every project of the tree of ``project``, all read at once: each takes
    a request per platform and per folder."""
    projects = tree_of(p, project)
    with ThreadPoolExecutor() as pool:
        held = pool.map(lambda q: held_at(edge, q, p.projects[q], asked.get(q, ())), projects)
        return dict(zip(projects, held))


def create_record(edge: Edge, r: Record) -> None:
    """Write ``r`` once: a record already at its place is left as it is."""
    where = layout.failure_record(edge.store.prefix, r.project, r.key, r.build.id)
    created = edge.store.create(where, encoded(r), "application/json")
    shown = f"s3://{edge.store.bucket}/{where}"
    edge.log(f"wrote the failure record {shown}" if created else f"{shown} exists; left as it is")


def records_of_plan(edge: Edge, plan: Plan, causes: Iterable[Cause]) -> dict[str, Record]:
    """By key, the record of this plan of every key the causes name, and of the keys those
    records name."""
    found: dict[str, Record] = {}
    asked: set[str] = set()
    pending = list(causes)
    while pending:
        c = pending.pop()
        if c.key in asked:
            continue
        asked.add(c.key)
        r = failure_in_plan(stored_records(edge, c.project, c.key), plan.id)
        if r is not None:
            found[c.key] = r
            pending.extend(r.caused_by)
    return found


# ---------------------------------------------------------------------------
# The plan of a build, and its own place in it
# ---------------------------------------------------------------------------


def stored_plan(job: Job, edge: Edge) -> Plan:
    """The plan from the meta-data of the root build: this build, or the one that started it."""
    return decode_plan(edge.agent.meta_data_get(PLAN, job.root_build))


def root_build_began(job: Job, edge: Edge) -> int:
    """When the root build began, in epoch seconds."""
    return int(edge.agent.meta_data_get(ARRIVED, job.root_build))


def own_planned(job: Job, edge: Edge, p: Plan) -> Planned:
    """This build's project in the plan, which must plan it at the commit it checked out."""
    return planned_project(p, job.project, edge.checkout.head_commit())


def create_own_key_json(job: Job, edge: Edge, p: Plan) -> Origin:
    """Create ``key.json`` of the key the plan fixed for this build unless it is there; the
    origin of the records this build writes at that key."""
    key = own_planned(job, edge, p).key
    if create_key_json(edge.store, key, key_json_of(p, job.project)):
        where = layout.key_json(edge.store.prefix, job.project, key)
        edge.log(f"wrote s3://{edge.store.bucket}/{where}")
    return Origin(job.project, key, p.id, job.build)


# ---------------------------------------------------------------------------
# The steps that promote, as the last step reads them
# ---------------------------------------------------------------------------


def promotes_value(promotes: Mapping[Folder, str]) -> str:
    return json.dumps([[f.platform, f.step, key] for f, key in sorted(promotes.items())])


def read_promotes(value: str) -> dict[Folder, str]:
    return {Folder(platform, step): key for platform, step, key in json.loads(value)}
