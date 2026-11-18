"""A step that promotes in a build with a snapshot key: it moves LATEST_SUCCESSFUL
as always, then creates the pointer of its variant, once. The first build of a
key to promote a variant holds the pointer. Without a key nothing changes."""

import datetime
import json

import pytest

KEY = "v1-0123456789abcdef0123456789abcdef"
LATEST = "widget-build/refs/heads/master/variants/linux/LATEST_SUCCESSFUL"
WHERE = f"_snapshot/v1/pointers/widget-build/{KEY}/linux.json"
OTHER_BUILD = "01900000-0000-7000-8000-0000000000ff"
OTHER_URL = "https://buildkite.example.test/example/widget-build/builds/511"


def promote_step(**promote):
    return {"promote": {"variant": "linux", "git_ref": "refs/heads/master", **promote}}


def promote(job, hook, **promote):
    return hook("post-command", job.with_plugin(promote_step(**promote)))


def pointer_of(build_id, number, url, commit):
    """A pointer of the variant as a build that promoted it wrote it."""
    fields = {
        "schema": 1,
        "project": "widget-build",
        "key": KEY,
        "variant": "linux",
        "git_ref": "refs/heads/master",
        "commit": commit,
        "build": {"id": build_id, "number": number, "url": url},
        "written_at": "2026-11-18T23:01:02.003Z",
    }
    return json.dumps(fields, separators=(",", ":")).encode()


def test_the_step_moves_latest_successful_then_creates_the_pointer_of_its_variant(job, store, hook):
    before = datetime.datetime.now(datetime.timezone.utc)

    outcome = promote(job, hook, snapshot=KEY)

    after = datetime.datetime.now(datetime.timezone.utc)
    assert outcome.code == 0, outcome.output
    puts = [(r.key, r.if_none_match, r.content_type) for r in store.plugin_requests("s3")]
    assert puts == [(LATEST, "", "text/plain"), (WHERE, "*", "application/json")]
    assert store.get(LATEST) == job.env["BUILDKITE_BUILD_ID"].encode()
    body = store.get(WHERE)
    written = json.loads(body)
    assert body == pointer_of(
        job.env["BUILDKITE_BUILD_ID"], 512, job.env["BUILDKITE_BUILD_URL"], job.env["FAKE_GIT_HEAD"]
    ).replace(b"2026-11-18T23:01:02.003Z", written["written_at"].encode())
    stamp = datetime.datetime.strptime(written["written_at"], "%Y-%m-%dT%H:%M:%S.%f%z")
    assert before - datetime.timedelta(milliseconds=1) <= stamp <= after
    assert WHERE in outcome.stderr


@pytest.mark.parametrize(
    "build_commit",
    ["HEAD", "0123456789abcdef0123456789abcdef01234567"],
    ids=["build-of-a-branch", "checkout-moved-off-the-builds-commit"],
)
def test_the_commit_of_the_pointer_is_the_head_of_the_jobs_checkout(
    job, store, hook, git, build_commit
):
    job = job.with_env(
        FAKE_GIT_HEAD="fedcba9876543210fedcba9876543210fedcba98", BUILDKITE_COMMIT=build_commit
    )

    assert promote(job, hook, snapshot=KEY).code == 0

    assert json.loads(store.get(WHERE))["commit"] == "fedcba9876543210fedcba9876543210fedcba98"
    (call,) = git.calls()
    assert call.argv == ["rev-parse", "HEAD"]
    assert call.cwd == str(job.workspace.resolve())


def test_a_pointer_another_build_holds_is_left_as_it_is_and_the_step_names_that_build(
    job, store, hook
):
    held = pointer_of(OTHER_BUILD, 511, OTHER_URL, "f" * 40)
    store.put(WHERE, held)

    outcome = promote(job, hook, snapshot=KEY)

    assert outcome.code == 0, outcome.output
    assert store.get(WHERE) == held
    assert store.get(LATEST) == job.env["BUILDKITE_BUILD_ID"].encode()
    assert "511" in outcome.stderr and OTHER_URL in outcome.stderr


def test_of_two_builds_of_one_key_the_first_to_promote_holds_the_pointer(job, store, hook):
    first = job.with_env(BUILDKITE_BUILD_ID=OTHER_BUILD, BUILDKITE_BUILD_NUMBER="511")
    assert promote(first, hook, snapshot=KEY).code == 0

    second = promote(job, hook, snapshot=KEY)

    assert second.code == 0, second.output
    assert json.loads(store.get(WHERE))["build"]["id"] == OTHER_BUILD
    assert store.get(LATEST) == job.env["BUILDKITE_BUILD_ID"].encode()


def test_a_create_still_in_flight_when_another_build_creates_the_pointer_names_that_build(
    job, store, hook, hook_midway
):
    held = store.hold("PUT", WHERE)
    other = job.with_env(
        BUILDKITE_BUILD_ID=OTHER_BUILD, BUILDKITE_BUILD_NUMBER="511", BUILDKITE_BUILD_URL=OTHER_URL
    )
    others = []

    def other_build_promotes_then_release(process):
        others.append(promote(other, hook, snapshot=KEY))
        store.release(held)

    first = hook_midway(
        "post-command",
        job.with_plugin(promote_step(snapshot=KEY)),
        held.arrived,
        other_build_promotes_then_release,
    )

    (second,) = others
    assert held.ended_by == "released"
    assert (first.code, second.code) == (0, 0), first.output + second.output
    assert json.loads(store.get(WHERE))["build"]["id"] == OTHER_BUILD
    assert "Created" in second.stderr
    assert "511" in first.stderr and OTHER_URL in first.stderr


def test_a_retried_job_finds_the_pointer_its_own_build_wrote(job, store, hook):
    assert promote(job, hook, snapshot=KEY).code == 0
    first = store.get(WHERE)

    retried = promote(job, hook, snapshot=KEY)

    assert retried.code == 0, retried.output
    assert store.get(WHERE) == first
    assert "this build" in retried.stderr


def test_without_a_key_promote_makes_its_one_request_and_asks_git_nothing(job, store, hook, git):
    outcome = promote(job, hook)

    assert outcome.code == 0, outcome.output
    assert [(r.method, r.key) for r in store.plugin_requests()] == [("PUT", LATEST)]
    assert git.calls() == []


def test_after_a_failed_command_a_step_with_a_key_writes_nothing(job, store, hook, git):
    outcome = promote(job.with_env(BUILDKITE_COMMAND_EXIT_STATUS="1"), hook, snapshot=KEY)

    assert outcome.code == 0, outcome.output
    assert store.requests == []
    assert git.calls() == []


def test_after_an_allowed_empty_upload_a_step_with_a_key_writes_nothing(job, store, hook):
    config = {
        "upload": {
            "variant": "linux",
            "git_ref": "refs/heads/master",
            "files": "missing/*",
            "allow_empty": True,
        },
        **promote_step(snapshot=KEY),
    }

    outcome = hook("post-command", job.with_plugin(config))

    assert outcome.code == 0, outcome.output
    assert store.requests == []


def test_the_pointer_sits_under_the_destination_prefix_beside_the_projects(job, store, hook):
    job = job.with_env(ARTIFACTS_DESTINATION="s3://example-artifacts/ci")

    assert promote(job, hook, snapshot=KEY).code == 0

    assert store.stored_keys() == [f"ci/{WHERE}", f"ci/{LATEST}"]


def test_the_pointer_is_of_the_project_the_promote_block_names(job, store, hook):
    assert promote(job, hook, snapshot=KEY, project_id="gizmo").code == 0

    pointer = f"_snapshot/v1/pointers/gizmo/{KEY}/linux.json"
    assert json.loads(store.get(pointer))["project"] == "gizmo"


def test_a_build_id_option_of_another_build_fails_a_step_with_a_key_before_any_request(
    job, store, hook
):
    outcome = promote(job, hook, snapshot=KEY, build_id=OTHER_BUILD)

    assert outcome.code == 1
    assert "promote.build_id" in outcome.error and OTHER_BUILD in outcome.error
    assert store.requests == []


def test_a_build_id_option_that_names_the_jobs_own_build_is_accepted_with_a_key(job, store, hook):
    outcome = promote(job, hook, snapshot=KEY, build_id=job.env["BUILDKITE_BUILD_ID"])

    assert outcome.code == 0, outcome.output
    assert json.loads(store.get(WHERE))["build"]["id"] == job.env["BUILDKITE_BUILD_ID"]


@pytest.mark.parametrize("backend", ["s3", "r2"])
def test_the_pointer_is_created_with_the_credentials_of_the_promote(job, store, hook, backend):
    assert promote(job, hook, snapshot=KEY).code == 0

    latest, created = store.plugin_requests("s3")
    assert created.key == WHERE
    assert (created.access_key_id, created.region) == (latest.access_key_id, latest.region)


@pytest.mark.parametrize(
    "env, promote_options, named",
    [
        ({"FAKE_GIT_HEAD": None}, {}, "git rev-parse HEAD"),
        ({"FAKE_GIT_HEAD": "HEAD"}, {}, "git rev-parse HEAD"),
        ({"BUILDKITE_BUILD_NUMBER": None}, {}, "BUILDKITE_BUILD_NUMBER"),
        ({"BUILDKITE_BUILD_NUMBER": "12a"}, {}, "BUILDKITE_BUILD_NUMBER"),
        ({"BUILDKITE_BUILD_URL": None}, {}, "BUILDKITE_BUILD_URL"),
        ({"BUILDKITE_BUILD_URL": "http://buildkite.example.test/b/1"}, {}, "build.url"),
        ({"BUILDKITE_BUILD_ID": "build-7"}, {}, "build.id"),
        ({}, {"variant": "linux amd64"}, "variant"),
        ({}, {"git_ref": "master"}, "git_ref"),
        ({}, {"project_id": "Gizmo"}, "project"),
    ],
    ids=[
        "no-checkout",
        "head-not-a-commit",
        "no-build-number",
        "build-number-not-a-number",
        "no-build-url",
        "build-url-not-https",
        "build-id-not-a-uuid",
        "variant-not-a-variant",
        "git-ref-not-a-full-ref",
        "project-not-a-slug",
    ],
)
def test_a_pointer_that_cannot_be_made_fails_the_step_before_any_request(
    job, store, hook, env, promote_options, named
):
    outcome = promote(job.with_env(**env), hook, snapshot=KEY, **promote_options)

    assert outcome.code == 1
    assert named in outcome.error
    assert store.requests == []


def test_a_pointer_at_the_key_that_does_not_read_fails_the_step_and_names_its_key(job, store, hook):
    store.put(WHERE, b"not a pointer")

    outcome = promote(job, hook, snapshot=KEY)

    assert outcome.code == 1
    assert WHERE in outcome.error
    assert store.get(WHERE) == b"not a pointer"


def test_a_create_the_store_refuses_fails_the_step_with_the_stores_error(job, store, hook):
    store.refuse("PUT", WHERE, 403, "AccessDenied")

    outcome = promote(job, hook, snapshot=KEY)

    assert outcome.code == 1
    assert "AccessDenied" in outcome.stderr
    assert store.stored_keys() == [LATEST]
