"""The LATEST_SUCCESSFUL pointer: its key, its body, and how promote is asked for."""

import pytest

POINTER = "widget-build/refs/heads/master/variants/linux/LATEST_SUCCESSFUL"


def promote_step(**promote):
    return {"promote": {"variant": "linux", "git_ref": "refs/heads/master", **promote}}


def test_the_pointer_body_is_the_build_id_and_nothing_else(job, store, hook):
    job = job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(promote_step())

    outcome = hook("post-command", job)

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == [POINTER]
    assert store.get(POINTER) == b"build-7"
    (put,) = store.plugin_requests("s3")
    assert (put.method, put.content_type) == ("PUT", "text/plain")
    assert POINTER in outcome.stderr and "build-7" in outcome.stderr


def test_promote_overwrites_whatever_the_pointer_held(job, store, hook):
    store.put(POINTER, b"build-newer")
    job = job.with_env(BUILDKITE_BUILD_ID="build-older").with_plugin(promote_step())

    assert hook("post-command", job).code == 0

    assert store.get(POINTER) == b"build-older"


def test_promote_does_not_look_at_the_build_it_points_to(job, store, hook):
    job = job.with_env(BUILDKITE_BUILD_ID="never-uploaded").with_plugin(promote_step())

    assert hook("post-command", job).code == 0

    assert store.stored_keys() == [POINTER]
    assert [r.method for r in store.plugin_requests()] == ["PUT"]


def test_the_pointer_key_starts_with_the_destination_prefix(job, store, hook):
    job = job.with_env(ARTIFACTS_DESTINATION="s3://example-artifacts/ci").with_plugin(
        promote_step()
    )

    assert hook("post-command", job).code == 0

    assert store.stored_keys() == [f"ci/{POINTER}"]


def test_project_id_and_the_undeclared_build_id_option_override_the_job(job, store, hook):
    job = job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(
        promote_step(project_id="gizmo", build_id="build-override")
    )

    assert hook("post-command", job).code == 0

    assert store.stored_keys() == ["gizmo/refs/heads/master/variants/linux/LATEST_SUCCESSFUL"]
    assert (
        store.get("gizmo/refs/heads/master/variants/linux/LATEST_SUCCESSFUL") == b"build-override"
    )


def test_upload_and_promote_options_are_independent(job, store, hook):
    job.write("a.bin", b"a")
    job = job.with_env(BUILDKITE_BUILD_ID="build-7").with_plugin(
        {
            "upload": {"variant": "linux", "git_ref": "refs/heads/feature", "files": "a.bin"},
            "promote": {"variant": "linux-other", "git_ref": "refs/heads/master"},
        }
    )

    assert hook("post-command", job).code == 0

    assert store.stored_keys() == [
        "widget-build/refs/heads/feature/variants/linux/builds/build-7/a.bin",
        "widget-build/refs/heads/feature/variants/linux/builds/build-7/manifest.json",
        "widget-build/refs/heads/master/variants/linux-other/LATEST_SUCCESSFUL",
    ]


def test_the_command_line_takes_the_four_flags(job, store, cli):
    outcome = cli(
        job,
        "promote",
        "--variant",
        "v",
        "--git-ref",
        "refs/tags/v1.0",
        "--project-id",
        "gizmo",
        "--build-id",
        "build-9",
    )

    assert outcome.code == 0, outcome.output
    assert store.get("gizmo/refs/tags/v1.0/variants/v/LATEST_SUCCESSFUL") == b"build-9"


def test_the_command_line_defaults_project_and_build_to_the_job(job, store, cli):
    job = job.with_env(BUILDKITE_PIPELINE_SLUG="gadget", BUILDKITE_BUILD_ID="build-3")

    outcome = cli(job, "promote", "--variant", "v", "--git-ref", "refs/heads/master")

    assert outcome.code == 0, outcome.output
    assert store.get("gadget/refs/heads/master/variants/v/LATEST_SUCCESSFUL") == b"build-3"


@pytest.mark.parametrize(
    "args, env, facts",
    [
        (["--git-ref", "refs/heads/master"], {}, ["--variant"]),
        (["--variant", "v"], {}, ["--git-ref"]),
        (
            ["--variant", "v", "--git-ref", "refs/heads/master"],
            {"BUILDKITE_PIPELINE_SLUG": None},
            ["--project-id", "BUILDKITE_PIPELINE_SLUG"],
        ),
        (
            ["--variant", "v", "--git-ref", "refs/heads/master"],
            {"BUILDKITE_BUILD_ID": None},
            ["BUILDKITE_BUILD_ID", "--build-id"],
        ),
        (["--variant", "v", "--force"], {}, ["--force"]),
        (["--variant=v", "--git-ref", "refs/heads/master"], {}, ["--variant=v"]),
    ],
    ids=["no-variant", "no-git-ref", "no-project", "no-build", "unknown-flag", "equals-form"],
)
def test_the_command_line_refuses_incomplete_or_unknown_arguments(
    job, store, cli, args, env, facts
):
    outcome = cli(job.with_env(**env), "promote", *args)

    assert outcome.code == 1
    for fact in facts:
        assert fact in outcome.error
    assert store.requests == []
