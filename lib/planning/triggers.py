"""What the plan step uploads to have dependencies built first: one trigger step per
dependency that lacks a folder, and the step of its own second run, which waits for them."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from ..forms import name_of
from ..layout import Folder
from .plan import Plan, below, dependencies_first, dependencies_of, trigger_label
from .rewrite import plugin_key

TRIGGER_KEY_PREFIX = "snapshot-dep-"
SECOND_RUN_KEY = "snapshot-plan-step-again"
SECOND_RUN_LABEL = ":camera_with_flash: snapshot: plan again"
# As the plan step: before every other step, and never for long.
PRIORITY = 100
TIMEOUT_MINUTES = 15


def needs_value(needs: Iterable[Folder]) -> str:
    """``SNAPSHOT_NEEDS``: the folders a started build must make, as a JSON list of
    ``[<platform>, <step id>]``, sorted."""
    return json.dumps([[f.platform, f.step] for f in sorted(set(needs))])


def read_needs(value: str) -> frozenset[Folder]:
    return frozenset(Folder(platform, step) for platform, step in json.loads(value))


def trigger_key(project: str) -> str:
    return f"{TRIGGER_KEY_PREFIX}{project}"


def trigger_step(plan: Plan, project: str, needs: Iterable[Folder], after: list[str]) -> dict:
    """The step that starts the build of ``project`` as the plan says, hands it ``needs`` and
    whether it writes its branch's latest files, and waits for the trigger steps ``after``.
    ``soft_fail``: the manifests say whether the dependency is usable, not the build's colour."""
    n = plan.projects[project]
    return {
        "label": trigger_label(plan, project),
        "key": trigger_key(project),
        "trigger": project,
        "soft_fail": True,
        **({"depends_on": after} if after else {}),
        "build": {
            "branch": name_of(n.ref),
            "commit": n.commit,
            "message": f"snapshot dependency of {plan.root.project} build {plan.root.build_number}",
            "env": {
                "SNAPSHOT_PLAN": plan.id,
                "SNAPSHOT_NEEDS": needs_value(needs),
                "SNAPSHOT_BRANCH_BUILD": "true" if n.writes_latest_file else "false",
            },
        },
    }


def trigger_steps(plan: Plan, needs: Mapping[str, Iterable[Folder]]) -> list[dict]:
    """One trigger step per project of ``needs``, dependencies first, each waiting for the
    trigger steps of the projects below it."""
    deps = dependencies_of(plan)
    order = [p for p in dependencies_first(deps) if p in needs]
    return [
        trigger_step(plan, p, needs[p], [trigger_key(q) for q in order if q in below(deps, p)])
        for p in order
    ]


def second_run_pipeline(
    plan: Plan,
    needs: Mapping[str, Iterable[Folder]],
    command: str,
    plugin_commit: str,
    pipeline_path: str,
) -> dict[str, Any]:
    """The trigger steps of ``needs``, and the step that runs the plan step's ``command`` again
    when every one of them has ended, with the option ``plan_again``."""
    triggers = trigger_steps(plan, needs)
    second_run = {
        "label": SECOND_RUN_LABEL,
        "key": SECOND_RUN_KEY,
        "priority": PRIORITY,
        "timeout_in_minutes": TIMEOUT_MINUTES,
        "depends_on": [{"step": t["key"], "allow_failure": True} for t in triggers],
        "command": command,
        "plugins": [{plugin_key(plugin_commit): {"plan_again": pipeline_path}}],
    }
    return {"steps": [*triggers, second_run]}
