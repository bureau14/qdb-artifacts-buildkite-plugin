"""What post-command runs, in which order, and what it skips."""

import pytest

POINTER = "widget-build/refs/heads/master/variants/linux/LATEST_SUCCESSFUL"
PREFIX = "widget-build/refs/heads/master/variants/linux/builds/build-7"


def step(upload=None, promote=True):
    config = {}
    if upload is not None:
        config["upload"] = {"variant": "linux", "git_ref": "refs/heads/master", **upload}
    if promote:
        config["promote"] = {"variant": "linux", "git_ref": "refs/heads/master"}
    return config


def post_command(job, hook, config, **env):
    return hook(
        "post-command", job.with_env(BUILDKITE_BUILD_ID="build-7", **env).with_plugin(config)
    )


def test_without_upload_files_or_promote_variant_the_hook_does_nothing(job, store, hook, agent):
    job = job.with_plugin({"download": {"clean": True}})

    outcome = hook("post-command", job)

    assert outcome.code == 0, outcome.output
    assert "No upload or promote configuration found" in outcome.stdout
    assert store.requests == []
    assert agent.calls() == []


def test_an_upload_block_without_files_is_skipped_like_no_upload(job, store, hook):
    job.write("default.tar.zst", b"d")

    outcome = post_command(job, hook, step(upload={}, promote=False))

    assert outcome.code == 0, outcome.output
    assert "No upload or promote configuration found" in outcome.stdout
    assert store.stored_keys() == []


def test_upload_then_promote_when_the_command_succeeded(job, store, hook):
    job.write("a.bin", b"a")

    outcome = post_command(job, hook, step(upload={"files": "a.bin"}))

    assert outcome.code == 0, outcome.output
    puts = [r.path.split("/", 2)[2] for r in store.plugin_requests("s3") if r.method == "PUT"]
    assert puts == [f"{PREFIX}/a.bin", f"{PREFIX}/manifest.json", POINTER]
    assert store.get(POINTER) == b"build-7"


def test_an_unset_command_exit_status_counts_as_success(job, store, hook):
    outcome = post_command(job, hook, step(), BUILDKITE_COMMAND_EXIT_STATUS=None)

    assert outcome.code == 0, outcome.output
    assert store.get(POINTER) == b"build-7"


@pytest.mark.parametrize("status", ["1", "2", "127"])
def test_a_failed_command_still_uploads_but_never_promotes(job, store, hook, status):
    job.write("logs/test.log", b"log")

    outcome = post_command(
        job, hook, step(upload={"files": "logs/*"}), BUILDKITE_COMMAND_EXIT_STATUS=status
    )

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == [f"{PREFIX}/logs/test.log", f"{PREFIX}/manifest.json"]
    assert f"command exited {status}" in outcome.stdout


def test_a_failed_command_with_promote_only_writes_nothing(job, store, hook):
    outcome = post_command(job, hook, step(), BUILDKITE_COMMAND_EXIT_STATUS="1")

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == []


def test_an_upload_that_matches_nothing_fails_the_hook_and_skips_promote(job, store, hook, agent):
    outcome = post_command(job, hook, step(upload={"files": "missing/*"}))

    assert outcome.code == 1
    assert "missing/*" in outcome.stderr
    assert store.requests == []
    assert agent.calls() == []


def test_an_upload_whose_matches_are_all_excluded_fails_the_hook(job, store, hook):
    job.write("a.log", b"l")

    outcome = post_command(job, hook, step(upload={"files": "a.log", "exclude": ["*.log"]}))

    assert outcome.code == 1
    assert "*.log" in outcome.stderr
    assert store.requests == []


@pytest.mark.parametrize(
    "upload",
    [
        {"files": "missing/*", "allow_empty": True},
        {"files": "a.log", "exclude": ["*.log"], "allow_empty": True},
    ],
    ids=["nothing-matched", "all-excluded"],
)
def test_an_allowed_empty_upload_succeeds_without_touching_the_store_or_promoting(
    job, store, hook, agent, upload
):
    job.write("a.log", b"l")

    outcome = post_command(job, hook, step(upload=upload))

    assert outcome.code == 0, outcome.output
    assert "Skipping artifact promotion (empty upload)" in outcome.stdout
    assert store.requests == []
    assert agent.calls() == []


@pytest.mark.parametrize("value", ["true", "TRUE", " yes ", "1", "on"])
def test_allow_empty_accepts_the_usual_spellings_of_true(job, store, hook, value):
    job = job.with_options(UPLOAD_ALLOW_EMPTY=value)

    outcome = post_command(job, hook, step(upload={"files": "missing/*"}))

    assert outcome.code == 0, outcome.output


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "y"])
def test_allow_empty_treats_anything_else_as_false(job, store, hook, value):
    job = job.with_options(UPLOAD_ALLOW_EMPTY=value)

    outcome = post_command(job, hook, step(upload={"files": "missing/*"}))

    assert outcome.code == 1


def test_a_failing_upload_passes_its_exit_status_and_skips_promote(job, store, hook):
    job.write("a.bin", b"a")

    outcome = post_command(job, hook, step(upload={"files": "a.bin", "base_dir": "nowhere"}))

    assert outcome.code == 1
    assert store.stored_keys() == []


def test_promote_without_git_ref_fails_the_hook(job, store, hook):
    config = {"promote": {"variant": "linux"}}

    outcome = post_command(job, hook, config)

    assert outcome.code == 1
    assert "--git-ref" in outcome.stderr
    assert store.stored_keys() == []


def test_upload_files_arrive_as_a_list_or_as_one_string(job, store, hook):
    job.write("a.bin", b"a")
    job.write("b.bin", b"b")
    job.write("c.txt", b"c")

    as_list = post_command(job, hook, step(upload={"files": ["a.bin", "*.txt"]}, promote=False))
    as_string = post_command(job, hook, step(upload={"files": "b.bin"}, promote=False))

    assert as_list.code == 0, as_list.output
    assert as_string.code == 0, as_string.output
    assert store.stored_keys() == [
        f"{PREFIX}/a.bin",
        f"{PREFIX}/b.bin",
        f"{PREFIX}/c.txt",
        f"{PREFIX}/manifest.json",
    ]


def test_the_list_form_wins_over_the_string_form_and_ends_at_the_first_gap(job, store, hook):
    for name in ("first.bin", "second.bin", "after-gap.bin", "scalar.bin"):
        job.write(name, b"x")
    job = job.with_env(BUILDKITE_BUILD_ID="build-7").with_options(
        UPLOAD_VARIANT="linux",
        UPLOAD_GIT_REF="refs/heads/master",
        UPLOAD_FILES_0="first.bin",
        UPLOAD_FILES_1="second.bin",
        UPLOAD_FILES_3="after-gap.bin",
        UPLOAD_FILES="scalar.bin",
    )

    outcome = hook("post-command", job)

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == [
        f"{PREFIX}/first.bin",
        f"{PREFIX}/manifest.json",
        f"{PREFIX}/second.bin",
    ]


def test_exclude_arrives_as_a_list_or_as_one_string(job, store, hook):
    job.write("a.bin", b"a")
    job.write("a.log", b"l")
    job.write("a.tmp", b"t")

    outcome = post_command(
        job, hook, step(upload={"files": "a.*", "exclude": "*.log"}, promote=False)
    )

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == [f"{PREFIX}/a.bin", f"{PREFIX}/a.tmp", f"{PREFIX}/manifest.json"]


@pytest.mark.parametrize("value", ["true", "on", "1"])
def test_debug_traces_the_hook(job, store, hook, value):
    outcome = post_command(job, hook, {"debug": value, **step()})

    assert outcome.code == 0, outcome.output
    assert "+ " in outcome.stderr
    assert "run_artifacts_py" in outcome.stderr
