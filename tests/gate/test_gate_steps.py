"""The gate steps through the real hooks: what they upload, mark and store.

``widget`` takes ``engine`` as a snapshot dependency; both build ``linux``.
A build of engine played to its end leaves engine's pointer, which a build of
widget then finds.
"""

import json

import pytest
import yaml
from standin import HOST, TOKEN

ENGINE = [("build-linux", "linux", [])]
WIDGET = [("build-linux", "linux", [("engine", "linux")])]
TOKEN_PARAMETER = "/services/buildkite/credentials/artifacts/buildkite-api-token"


def failure_records(store):
    return [k for k in store.stored_keys("_snapshot/v1/failures/")]


def enter(buildkite, build):
    return buildkite.run_step(build, build.step("snapshot-enter"))


def nothing_shows(text_of_build, secret):
    return all(secret not in text for text in text_of_build)


def everything_shown(buildkite, build, run):
    """Every text a build put where people or other jobs read it."""
    annotations = [body for _, body in buildkite.agent.annotations(build.id).values()]
    meta = list(buildkite.agent.meta_data(build.id).values())
    return [run.output, *buildkite.agent.uploads(build.id), *annotations, *meta]


def test_a_build_that_builds_uploads_its_pipeline_with_the_builds_it_downloads(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    buildkite.add_pipeline("widget", WIDGET)
    engine = buildkite.build("engine")
    widget = buildkite.start("widget")

    run = enter(buildkite, widget)

    assert run.outcome == "passed", run.output
    (uploaded,) = [yaml.safe_load(u) for u in buildkite.agent.uploads(widget.id)]
    (step,) = uploaded["steps"]
    ((plugin, options),) = step["plugins"][0].items()
    assert plugin == f"bureau14/qdb-artifacts#{buildkite.plugin_commit}"
    (download,) = options["download"]["projects"]
    assert (download["build_id"], download["git_ref"]) == (engine.id, "refs/heads/master")
    assert download["require_strict_ref_match"] is True
    meta = buildkite.agent.meta_data(widget.id)
    plan = json.loads(meta["snapshot-plan"])
    assert sorted(plan["nodes"]) == ["engine", "widget"]
    assert options["upload"]["snapshot"] == options["promote"]["snapshot"]
    assert options["promote"]["snapshot"] == plan["nodes"]["widget"]["key"]
    assert json.loads(meta["snapshot-promotes"]) == {"linux": "build-linux"}
    assert meta["snapshot-uploaded-snapshot-enter"] == widget.id
    assert meta["snapshot-mode"] == "built"
    assert failure_records(store) == []
    style, body = buildkite.agent.annotations(widget.id)["snapshot-plan"]
    assert style == "info" and body.startswith("Snapshot plan of `widget` build 1")
    asked = {path: authorization for path, authorization in buildkite.api.requests[-2:]}
    assert asked == {
        "/v2/organizations/example/pipelines/widget": f"Bearer {TOKEN}",
        "/v2/organizations/example/pipelines/engine": f"Bearer {TOKEN}",
    }
    assert nothing_shows(everything_shown(buildkite, widget, run), TOKEN)


def test_a_build_whose_every_variant_exists_is_reused_and_uploads_nothing(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    buildkite.add_pipeline("widget", WIDGET)
    first = buildkite.build("widget")
    again = buildkite.start("widget")

    run = enter(buildkite, again)

    assert first.state == "passed", first.report()
    assert run.outcome == "passed", run.output
    assert buildkite.agent.uploads(again.id) == []
    assert buildkite.agent.meta_data(again.id)["snapshot-mode"] == "reused"
    style, body = buildkite.agent.annotations(again.id)["snapshot"]
    assert style == "success"
    assert f"linux in build 1 {first.url}" in body
    assert [b.number for b in buildkite.builds_of("engine")] == [1]


def test_a_root_whose_pipeline_breaks_the_rule_fails_and_stores_nothing(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    undeclared = yaml.safe_dump(
        {
            "steps": [
                {
                    "label": "hidden",
                    "key": "hidden",
                    "command": "true",
                    "plugins": [
                        {
                            "bureau14/qdb-artifacts#master": {
                                "download": {
                                    "projects": [
                                        {
                                            "project_id": "engine",
                                            "variant": "linux",
                                            "git_ref": "{git_ref}",
                                            "build_id": "LATEST_SUCCESSFUL",
                                        }
                                    ]
                                }
                            }
                        }
                    ],
                }
            ]
        }
    )
    buildkite.add_pipeline("widget", WIDGET, {".buildkite/unread-steps.txt": undeclared})
    widget = buildkite.start("widget")

    run = enter(buildkite, widget)

    assert run.outcome == "hard_failed"
    assert "step hidden: downloads from engine as a latest successful dependency" in run.output
    assert buildkite.agent.uploads(widget.id) == []
    assert buildkite.agent.meta_data(widget.id)["snapshot-mode"] == "failed"
    style, body = buildkite.agent.annotations(widget.id)["snapshot"]
    assert style == "error" and "breaks the rule of its templates" in body
    assert failure_records(store) == []


def test_a_chain_node_asked_for_a_variant_it_does_not_promote_stores_its_record(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    buildkite.add_pipeline("widget", WIDGET)
    widget = buildkite.start("widget")
    assert enter(buildkite, widget).outcome == "passed"
    trigger = widget.step("snapshot-dep-engine")["build"]
    engine = buildkite.start(
        "engine",
        branch=trigger["branch"],
        commit=trigger["commit"],
        env={**trigger["env"], "SNAPSHOT_NEEDS": '["windows"]'},
    )

    run = enter(buildkite, engine)

    assert run.outcome == "hard_failed"
    assert "asked for windows, which this pipeline does not promote" in run.output
    (where,) = failure_records(store)
    record = json.loads(store.get(where))
    assert (record["reason"], record["plan"], record["build"]["id"]) == (
        "not_declared",
        widget.id,
        engine.id,
    )
    assert buildkite.agent.meta_data(engine.id)["snapshot-mode"] == "failed"


def test_a_resume_step_whose_triggered_build_left_nothing_fails_and_triggers_nothing(
    buildkite, store
):
    buildkite.add_pipeline("engine", ENGINE)
    buildkite.add_pipeline("widget", WIDGET)
    widget = buildkite.start("widget")
    assert enter(buildkite, widget).outcome == "passed"
    label = widget.step("snapshot-dep-engine")["label"]

    run = buildkite.run_step(widget, widget.step("snapshot-resume"))

    assert run.outcome == "hard_failed"
    assert (
        "the build of engine triggered by this build ended without linux and left no record; "
        f"see the step {label}"
    ) in run.output
    assert len(buildkite.agent.uploads(widget.id)) == 1
    assert buildkite.agent.meta_data(widget.id)["snapshot-mode"] == "failed"


def test_leave_stores_the_record_of_a_variant_whose_step_failed(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    (buildkite.breaks / "engine-linux").write_text("")

    engine = buildkite.build("engine")

    assert engine.outcomes["build-linux"] == "hard_failed"
    assert engine.outcomes["snapshot-leave"] == "passed"
    (where,) = failure_records(store)
    record = json.loads(store.get(where))
    assert record["failed"] == [
        {"variant": "linux", "step": "build-linux", "outcome": "hard_failed"}
    ]
    assert record["build"]["id"] == engine.id
    style, body = buildkite.agent.annotations(engine.id)["snapshot-leave"]
    assert style == "error" and body.startswith("Snapshot failed")


def test_a_gate_step_runs_its_gate_command_after_a_failed_command_and_nothing_else(
    buildkite, store
):
    buildkite.add_pipeline("engine", ENGINE, {".buildkite/pipeline.py": "raise SystemExit(3)\n"})
    engine = buildkite.start("engine")
    (options,) = engine.step("snapshot-enter")["plugins"][0].values()
    options["upload"] = {
        "variant": "linux",
        "git_ref": "refs/heads/master",
        "files": ".buildkite/*",
    }

    run = enter(buildkite, engine)

    assert run.outcome == "hard_failed"
    assert "the command exited 3" in run.output
    assert store.plugin_requests() == []
    assert buildkite.agent.meta_data(engine.id)["snapshot-mode"] == "failed"


@pytest.mark.parametrize(
    "options, named",
    [
        ({"PLAN": "p.yml", "LEAVE": "true"}, "this one plan and leave"),
        ({"PLAN": "p.yml", "RESUME": "p.yml"}, "this one plan and resume"),
    ],
    ids=["plan-and-leave", "plan-and-resume"],
)
def test_a_plan_step_that_carries_a_second_gate_option_fails_before_anything_runs(
    job, store, hook, agent, gate_plugin, options, named
):
    outcome = hook("post-command", job.with_options(**options), root=gate_plugin)

    assert outcome.code == 1
    assert named in outcome.error
    assert store.requests == [] and agent.calls() == []


def test_a_retried_plan_step_after_its_upload_uploads_nothing_again(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    engine = buildkite.start("engine")
    assert enter(buildkite, engine).outcome == "passed"

    run = enter(buildkite, engine)

    assert run.outcome == "passed", run.output
    assert len(buildkite.agent.uploads(engine.id)) == 1
    assert "snapshot-enter uploaded in this build before" in run.output


def test_a_retried_plan_step_whose_command_fails_leaves_the_build_as_it_uploaded(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    engine = buildkite.start("engine")
    assert enter(buildkite, engine).outcome == "passed"
    (engine.checkout / ".buildkite" / "steps.yml").write_text("steps: [\n")
    (buildkite.breaks / "engine-linux").write_text("")

    retried = enter(buildkite, engine)
    buildkite.play(engine)

    assert retried.outcome == "hard_failed"
    assert "the command exited 1 this time; the build goes on as this step uploaded" in (
        retried.output
    )
    assert buildkite.agent.meta_data(engine.id)["snapshot-mode"] == "built"
    assert engine.outcomes["build-linux"] == "hard_failed"
    (where,) = failure_records(store)
    record = json.loads(store.get(where))
    assert (record["reason"], record["failed"]) == (
        None,
        [{"variant": "linux", "step": "build-linux", "outcome": "hard_failed"}],
    )


def test_without_the_variable_the_token_is_read_from_ssm(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    store.put_parameter(TOKEN_PARAMETER, "bkua_from-ssm-not-real", secure=True)
    del buildkite.env["ARTIFACTS_BUILDKITE_TOKEN"]
    engine = buildkite.start("engine")

    run = enter(buildkite, engine)

    assert run.outcome == "passed", run.output
    assert buildkite.api.requests == [
        ("/v2/organizations/example/pipelines/engine", "Bearer bkua_from-ssm-not-real")
    ]
    assert nothing_shows(everything_shown(buildkite, engine, run), "bkua_from-ssm-not-real")


def test_a_token_read_with_a_newline_at_its_end_is_sent_without_it(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    store.put_parameter(TOKEN_PARAMETER, "bkua_from-a-file-not-real\n", secure=True)
    del buildkite.env["ARTIFACTS_BUILDKITE_TOKEN"]
    engine = buildkite.start("engine")

    run = enter(buildkite, engine)

    assert run.outcome == "passed", run.output
    assert buildkite.api.requests == [
        ("/v2/organizations/example/pipelines/engine", "Bearer bkua_from-a-file-not-real")
    ]
    assert nothing_shows(everything_shown(buildkite, engine, run), "bkua_from-a-file-not-real")


def test_a_token_that_cannot_be_sent_fails_the_plan_naming_where_it_was_read(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    buildkite.env["ARTIFACTS_BUILDKITE_TOKEN"] = "bkua_with a-space-not-real"
    engine = buildkite.start("engine")

    run = enter(buildkite, engine)

    assert run.outcome == "hard_failed"
    named = "the Buildkite API token in ARTIFACTS_BUILDKITE_TOKEN is not printable ASCII"
    assert named in run.output
    assert buildkite.api.requests == []
    assert buildkite.agent.meta_data(engine.id)["snapshot-mode"] == "failed"
    style, body = buildkite.agent.annotations(engine.id)["snapshot"]
    assert style == "error" and named in body
    shown = everything_shown(buildkite, engine, run)
    assert nothing_shows(shown, "bkua_with") and nothing_shows(shown, "a-space-not-real")


def test_an_answer_of_the_api_that_is_an_error_fails_the_plan_and_is_no_answer(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    buildkite.add_pipeline("widget", WIDGET)
    buildkite.api.statuses["engine"] = 401
    widget = buildkite.start("widget")

    run = enter(buildkite, widget)

    assert run.outcome == "hard_failed"
    assert "pipeline engine: Buildkite answered 401: answer 401" in run.output
    meta = buildkite.agent.meta_data(widget.id)
    assert "snapshot-plan" not in meta and meta["snapshot-mode"] == "failed"
    style, body = buildkite.agent.annotations(widget.id)["snapshot"]
    assert style == "error"
    assert body.startswith("The snapshot step of `widget` build 1 failed")


def test_an_error_of_git_fails_the_plan(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    buildkite.add_pipeline("widget", WIDGET)
    buildkite.api.pipelines["engine"]["repository"] = f"{HOST}nowhere.git"
    widget = buildkite.start("widget")

    run = enter(buildkite, widget)

    assert run.outcome == "hard_failed"
    assert "ls-remote" in run.output and "nowhere.git" in run.output
    assert "snapshot-plan" not in buildkite.agent.meta_data(widget.id)


def test_an_error_of_the_store_fails_the_step(buildkite, store):
    buildkite.add_pipeline("engine", ENGINE)
    engine = buildkite.start("engine")
    plan_run = enter(buildkite, engine)
    key = json.loads(buildkite.agent.meta_data(engine.id)["snapshot-plan"])["nodes"]["engine"][
        "key"
    ]
    store.refuse("GET", f"_snapshot/v1/pointers/engine/{key}/linux.json", 403, "AccessDenied")
    again = buildkite.start("engine")

    run = enter(buildkite, again)

    assert plan_run.outcome == "passed"
    assert run.outcome == "hard_failed"
    assert "AccessDenied" in run.output
    assert buildkite.agent.meta_data(again.id)["snapshot-mode"] == "failed"
