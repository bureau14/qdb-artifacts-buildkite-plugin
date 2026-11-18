"""Plans for the tests of the planning half.

The graph mirrors the real one (a diamond)::

    qdb-api-python -> qdb-api-rest -> qdb-duck -> quasardb-build
    qdb-api-python -> quasardb-build
    qdb-api-rest   -> quasardb-build
"""

from __future__ import annotations

import hashlib

from lib.forms import iso_utc
from lib.snapshot.plan import REBUILD_NONE, Node, Plan, RootInfo

PYTHON, REST, DUCK, QDB = "qdb-api-python", "qdb-api-rest", "qdb-duck", "quasardb-build"
URLS = {
    PYTHON: "https://github.com/bureau14/qdb-api-python.git",
    REST: "https://github.com/bureau14/qdb-api-rest.git",
    DUCK: "https://github.com/bureau14/qdb-duck.git",
    QDB: "https://github.com/bureau14/quasardb.git",
}
# Direct dependencies, sorted as the planner writes them.
EDGES = {PYTHON: (REST, QDB), REST: (DUCK, QDB), DUCK: (QDB,), QDB: ()}
# A made-up root build uuid.
PLAN_ID = "01890000-aaaa-7000-8000-000000000001"
T0 = 1_763_380_800.0


def sha(project: str) -> str:
    return hashlib.sha1(project.encode()).hexdigest()


def key(project: str) -> str:
    return "v1-" + hashlib.sha1(("key/" + project).encode()).hexdigest()[:32]


def plan(
    rebuild: str = REBUILD_NONE,
    created: float = T0,
    label: str = "master",
    root_has_snapshot: bool = True,
) -> Plan:
    """The diamond as planned by a root build of qdb-api-python on ``label``.

    Without ``root_has_snapshot`` the root has no key, as a root with a
    latest successful dependency.
    """
    nodes = {
        p: Node(
            URLS[p],
            "refs/heads/master",
            sha(p),
            key(p) if root_has_snapshot or p != PYTHON else None,
            label,
            EDGES[p],
        )
        for p in EDGES
    }
    return Plan(
        id=PLAN_ID,
        context=label,
        rebuild=rebuild,
        created_at=iso_utc(created),
        root=RootInfo(PYTHON, 512, "https://ci.example/qdb-api-python/512"),
        nodes=nodes,
    )
