"""Reduce a generated pipeline to its shape, for use as a public test fixture.

Usage: python sanitise.py <generated.yml> <out.yml>

Keeps the step structure (groups, keys, labels, dependencies) and the
qdb-artifacts plugin blocks verbatim. Commands become ``echo step``; the
configuration of every other plugin, environment, agents, retries and
priorities are dropped, so that no host name or build detail is copied.
"""

from __future__ import annotations

import sys

import yaml

KEPT = (
    "group",
    "label",
    "key",
    "depends_on",
    "allow_dependency_failure",
    "soft_fail",
    "timeout_in_minutes",
)
ARTIFACTS = "bureau14/qdb-artifacts"


def sanitise_plugin(p):
    if isinstance(p, str):
        return p
    return {
        name: (config if name.split("#", 1)[0] == ARTIFACTS else {}) for name, config in p.items()
    }


def sanitise_step(s):
    if not isinstance(s, dict):
        return s
    out = {k: s[k] for k in KEPT if k in s}
    if "steps" in s:
        out["steps"] = [sanitise_step(x) for x in s["steps"]]
    if "command" in s or "commands" in s:
        out["command"] = "echo step"
    if "plugins" in s:
        out["plugins"] = [sanitise_plugin(p) for p in s["plugins"]]
    return out


def main(src: str, dst: str) -> None:
    with open(src, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    out = {"steps": [sanitise_step(s) for s in doc["steps"]]}
    with open(dst, "w", encoding="utf-8") as f:
        yaml.safe_dump(out, f, sort_keys=False, default_flow_style=False, width=4096)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
