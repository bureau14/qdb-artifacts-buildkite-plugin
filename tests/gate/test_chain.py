"""A chain of builds, played by the stand-in for Buildkite through the real hooks.

Three pipelines, each in a repository of its own::

    top    -> middle -> base
    top    -> base

base builds linux and macos; middle builds linux from base's linux; top
builds linux from middle's and base's linux, and macos from base's macos. A
build of top on master is the root; the builds it triggers are its chain.
Every build step's command appends a line to the stand-in's list of what was
compiled, so a test sees which steps ran in which build.
"""

import json

import yaml
from standin import PIPELINE_PY, download_entry

BASE = [("build-linux", "linux", []), ("build-macos", "macos", [])]
MIDDLE = [("build-linux", "linux", [("base", "linux")])]
TOP = [
    ("build-linux", "linux", [("middle", "linux"), ("base", "linux")]),
    ("build-macos", "macos", [("base", "macos")]),
]


def three_pipelines(buildkite, top=TOP):
    buildkite.add_pipeline("base", BASE)
    buildkite.add_pipeline("middle", MIDDLE)
    buildkite.add_pipeline("top", top)


def compiled(buildkite, project=None):
    """``(project, build number, step)`` of every build step that ran, in order."""
    lines = (line.split() for line in buildkite.compiled_steps())
    return [(p, int(n), s) for p, n, s in lines if project in (None, p)]


def numbers(buildkite, project):
    return [b.number for b in buildkite.builds_of(project)]


def plan_of(buildkite, root):
    return json.loads(buildkite.agent.meta_data(root.id)["snapshot-plan"])


def manifest(store, project, variant, build):
    """The builds the step that uploaded ``variant`` of ``project`` in ``build`` downloaded."""
    key = f"{project}/refs/heads/master/variants/{variant}/builds/{build.id}/manifest.json"
    return json.loads(store.get(key))["downloads"]


def artifact(store, project, variant, build):
    """The file a build step of ``project`` uploaded for ``variant`` in ``build``."""
    key = (
        f"{project}/refs/heads/master/variants/{variant}/builds/{build.id}/{project}-{variant}.bin"
    )
    return store.get(key).decode()


def test_a_cold_chain_builds_bottom_up_with_one_build_per_key(buildkite, store):
    three_pipelines(buildkite)

    top = buildkite.build("top")

    assert top.state == "passed", top.report()
    assert compiled(buildkite) == [
        ("base", 1, "build-linux"),
        ("base", 1, "build-macos"),
        ("middle", 1, "build-linux"),
        ("top", 1, "build-linux"),
        ("top", 1, "build-macos"),
    ]
    (base,) = buildkite.builds_of("base")
    (middle,) = buildkite.builds_of("middle")
    assert base.parent is top and middle.parent is top
    # The diamond: middle downloads the build of base that top downloads.
    base_commit = base.commit
    lines = artifact(store, "top", "linux", top).splitlines()
    assert lines == [
        f"top linux {top.commit}",
        f"base linux {base_commit}",
        f"middle linux {middle.commit}",
        f"base linux {base_commit}",
    ]
    plan = plan_of(buildkite, top)
    for project, build in (("base", base), ("middle", middle), ("top", top)):
        key = plan["nodes"][project]["key"]
        pointer = json.loads(store.get(f"_snapshot/v1/pointers/{project}/{key}/linux.json"))
        assert pointer["build"]["id"] == build.id
    assert store.stored_keys("_snapshot/v1/failures/") == []


def test_a_second_root_build_is_reused_and_triggers_nothing(buildkite, store):
    three_pipelines(buildkite)
    buildkite.build("top")

    again = buildkite.build("top")

    assert again.state == "passed", again.report()
    assert again.children == []
    assert buildkite.agent.meta_data(again.id)["snapshot-mode"] == "reused"
    assert buildkite.agent.uploads(again.id) == []
    assert numbers(buildkite, "base") == numbers(buildkite, "middle") == [1]
    assert compiled(buildkite, "top") == [("top", 1, "build-linux"), ("top", 1, "build-macos")]


def test_a_new_commit_rebuilds_exactly_the_snapshots_it_is_part_of(buildkite, store):
    three_pipelines(buildkite)
    buildkite.build("top")

    buildkite.hosting.commit("base", {"src/change": "a new commit of base\n"})
    after_base = buildkite.build("top")
    buildkite.hosting.commit("middle", {"src/change": "a new commit of middle\n"})
    after_middle = buildkite.build("top")

    assert after_base.state == after_middle.state == "passed", after_middle.report()
    assert numbers(buildkite, "base") == [1, 2]
    assert numbers(buildkite, "middle") == [1, 2, 3]
    assert numbers(buildkite, "top") == [1, 2, 3]
    assert [c.project for c in after_middle.children] == ["middle"]
    assert compiled(buildkite)[5:] == [
        ("base", 2, "build-linux"),
        ("base", 2, "build-macos"),
        ("middle", 2, "build-linux"),
        ("top", 2, "build-linux"),
        ("top", 2, "build-macos"),
        ("middle", 3, "build-linux"),
        ("top", 3, "build-linux"),
        ("top", 3, "build-macos"),
    ]


def test_a_failing_step_in_base_fails_the_builds_above_it_with_the_cause_and_unbuilt(
    buildkite, store
):
    three_pipelines(buildkite)
    breaker = buildkite.breaks / "base-linux"
    breaker.write_text("")

    failed = buildkite.build("top")

    (base,) = buildkite.builds_of("base")
    (middle,) = buildkite.builds_of("middle")
    assert (failed.state, base.state, middle.state) == ("failed", "failed", "failed")
    assert base.outcomes["build-linux"] == "hard_failed"
    assert compiled(buildkite) == [("base", 1, "build-linux"), ("base", 1, "build-macos")]
    assert middle.outcomes["snapshot-enter"] == "hard_failed"
    assert failed.outcomes["snapshot-resume"] == "hard_failed"
    style, body = buildkite.agent.annotations(failed.id)["snapshot"]
    assert style == "error" and body.startswith("Snapshot dependency failed")
    assert f"FAILED in base build 1: {base.url}" in body
    assert "linux: step build-linux: hard_failed" in body
    assert body.endswith(
        "To retry: retry the failed job in `base` `1`. When it passes, start a new build of "
        "`top`. Everything that passed is reused."
    )
    _, middle_body = buildkite.agent.annotations(middle.id)["snapshot"]
    assert f"FAILED in base build 1: {base.url}" in middle_body
    records = [json.loads(store.get(k)) for k in store.stored_keys("_snapshot/v1/failures/")]
    assert sorted((r["project"], r["plan"]) for r in records) == [
        ("base", failed.id),
        ("middle", failed.id),
        ("top", failed.id),
    ]

    breaker.unlink()
    retried = buildkite.retry(base, "build-linux")
    again = buildkite.build("top")

    assert retried.outcome == "passed", retried.output
    assert again.state == "passed", again.report()
    assert numbers(buildkite, "base") == [1]
    assert [c.project for c in again.children] == ["middle"]
    assert compiled(buildkite)[2:] == [
        ("base", 1, "build-linux"),
        ("middle", 2, "build-linux"),
        ("top", 2, "build-linux"),
        ("top", 2, "build-macos"),
    ]


def test_a_key_with_one_of_two_variants_present_builds_only_the_missing_one(buildkite, store):
    three_pipelines(buildkite)
    breaker = buildkite.breaks / "base-macos"
    breaker.write_text("")
    failed = buildkite.build("top")
    breaker.unlink()

    again = buildkite.build("top")

    assert failed.state == "failed"
    assert again.state == "passed", again.report()
    first, second = buildkite.builds_of("base")
    assert compiled(buildkite, "base") == [
        ("base", 1, "build-linux"),
        ("base", 1, "build-macos"),
        ("base", 2, "build-macos"),
    ]
    replaced = second.step("build-linux")
    assert replaced["label"] == "base build-linux (exists in build 1)"
    assert "plugins" not in replaced and second.outcomes["build-linux"] == "passed"
    assert json.loads(buildkite.agent.meta_data(second.id)["snapshot-promotes"]) == {
        "macos": "build-macos"
    }
    downloaded = {
        variant: [(d["project_id"], d["build_id"]) for d in manifest(store, "top", variant, again)]
        for variant in ("linux", "macos")
    }
    assert downloaded["macos"] == [("base", second.id)]
    assert ("base", first.id) in downloaded["linux"]
    assert numbers(buildkite, "middle") == [1]


def test_a_dependency_that_cannot_give_a_snapshot_fails_the_plan_with_the_way_out(buildkite, store):
    three_pipelines(buildkite)
    plain = PIPELINE_PY.replace("    run(generate_pipeline)\n", "    print(generate_pipeline())\n")
    commit = buildkite.hosting.commit("base", {".buildkite/pipeline.py": plain})

    top = buildkite.build("top")

    assert top.state == "failed"
    assert top.children == [] and compiled(buildkite) == []
    style, body = buildkite.agent.annotations(top.id)["snapshot"]
    assert style == "error"
    assert body.startswith("The snapshot plan of `top` build 1 cannot be made")
    assert f"base cannot give a snapshot at refs/heads/master {commit}" in body
    assert "does not hold exactly the call run(generate_pipeline)" in body
    assert "Way out: base adopts on refs/heads/master" in body
    assert (
        "or top takes base as a latest successful dependency, with build_id: LATEST_SUCCESSFUL "
        "in its entry of base in .buildkite/steps.yml"
    ) in body
    assert "snapshot-plan" not in buildkite.agent.meta_data(top.id)


def test_a_root_with_a_latest_successful_dependency_has_no_key_and_always_builds(buildkite, store):
    store.put_build("tools", "refs/heads/master", "linux", "tools-7", {"tools-linux.bin": b"t\n"})
    store.put_pointer("tools", "refs/heads/master", "linux", "tools-7")
    with_tools = [*TOP, ("test-linux", None, [("tools", "linux", True), ("base", "linux")])]
    three_pipelines(buildkite, top=with_tools)

    first = buildkite.build("top")
    second = buildkite.build("top")

    assert first.state == second.state == "passed", second.report()
    plan = plan_of(buildkite, second)
    assert plan["nodes"]["top"]["key"] is None
    assert plan["nodes"]["base"]["key"] and plan["nodes"]["middle"]["key"]
    assert numbers(buildkite, "base") == numbers(buildkite, "middle") == [1]
    assert [s for p, n, s in compiled(buildkite, "top")] == [
        "build-linux",
        "build-macos",
        "test-linux",
    ] * 2
    (uploaded,) = [yaml.safe_load(u) for u in buildkite.agent.uploads(second.id)]
    (test_step,) = [s for s in uploaded["steps"] if s["key"] == "test-linux"]
    (options,) = test_step["plugins"][0].values()
    tools, base = options["download"]["projects"]
    expected = {**download_entry("tools", "linux", latest=True), "git_ref": "refs/heads/master"}
    assert tools == expected
    assert base["build_id"] == buildkite.builds_of("base")[0].id
    (build_linux,) = [s for s in uploaded["steps"] if s["key"] == "build-linux"]
    (build_options,) = build_linux["plugins"][0].values()
    assert "snapshot" not in build_options["upload"] and "snapshot" not in build_options["promote"]
    assert store.stored_keys("_snapshot/v1/pointers/top/") == []


def test_snapshot_rebuild_builds_the_root_again_with_1_and_every_node_with_all(buildkite, store):
    three_pipelines(buildkite)
    buildkite.build("top")

    one = buildkite.build("top", env={"SNAPSHOT_REBUILD": "1"})
    every = buildkite.build("top", env={"SNAPSHOT_REBUILD": "all"})

    assert one.state == every.state == "passed", every.report()
    assert one.children == []
    assert [c.project for c in every.children] == ["base", "middle"]
    assert numbers(buildkite, "base") == numbers(buildkite, "middle") == [1, 2]
    assert compiled(buildkite)[5:] == [
        ("top", 2, "build-linux"),
        ("top", 2, "build-macos"),
        ("base", 2, "build-linux"),
        ("base", 2, "build-macos"),
        ("middle", 2, "build-linux"),
        ("top", 3, "build-linux"),
        ("top", 3, "build-macos"),
    ]
    assert plan_of(buildkite, one)["rebuild"] == "one"
    assert plan_of(buildkite, every)["rebuild"] == "all"
