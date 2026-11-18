"""Which build a download takes: explicit ids, the pointer, defaults, and the ref walk."""

import pytest

CURRENT_BUILD = "build-current"
MASTER = "refs/heads/master"
MAIN = "refs/heads/main"
FEATURE = "refs/heads/feature/widget"


def download_step(**project):
    entry = {"variant": "linux", "git_ref": FEATURE, "files": ["*.bin"], "output-dir": "out"}
    return {"download": {"projects": [{**entry, **project}]}}


def download(job, hook, **project):
    return hook(
        "pre-command",
        job.with_env(BUILDKITE_BUILD_ID=CURRENT_BUILD).with_plugin(download_step(**project)),
    )


def downloaded(job):
    path = job.workspace / "out" / "a.bin"
    return path.read_bytes() if path.exists() else None


def put_build(store, project, ref, build_id, content=None):
    store.put_build(project, ref, "linux", build_id, {"a.bin": content or build_id.encode()})


def listings(store):
    return [
        r.query
        for r in store.plugin_requests("s3")
        if r.method == "GET" and r.query.get("list-type") == "2"
    ]


def list_prefixes(store):
    return [query["prefix"] for query in listings(store)]


def object_reads(store):
    return [
        r.path.split("/", 2)[2]
        for r in store.plugin_requests("s3")
        if r.method == "GET" and "list-type" not in r.query
    ]


def test_an_explicit_build_id_is_downloaded_without_reading_a_pointer(job, store, hook):
    put_build(store, "engine", FEATURE, "b-42")
    put_build(store, "engine", FEATURE, "b-promoted")
    store.put_pointer("engine", FEATURE, "linux", "b-promoted")

    outcome = download(job, hook, project_id="engine", build_id="b-42")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"b-42"
    assert not [k for k in object_reads(store) if k.endswith("LATEST_SUCCESSFUL")]


def test_latest_successful_reads_the_pointer_and_strips_whitespace_around_its_body(
    job, store, hook
):
    put_build(store, "engine", FEATURE, "b-9")
    store.put_pointer("engine", FEATURE, "linux", "  b-9\n")

    outcome = download(job, hook, project_id="engine", build_id="LATEST_SUCCESSFUL")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"b-9"
    assert object_reads(store)[0] == f"engine/{FEATURE}/variants/linux/LATEST_SUCCESSFUL"
    assert "b-9" in outcome.stderr and FEATURE in outcome.stderr


@pytest.mark.parametrize(
    "project, env, expected",
    [
        ({"project_id": "engine"}, {}, "b-promoted"),
        ({}, {"BUILDKITE_BRANCH": "feature/widget"}, CURRENT_BUILD),
        ({"project_id": "widget-build"}, {"BUILDKITE_BRANCH": "feature/widget"}, CURRENT_BUILD),
        ({}, {"BUILDKITE_BRANCH": "master"}, "b-promoted"),
        ({}, {"BUILDKITE_BRANCH": FEATURE}, CURRENT_BUILD),
        ({}, {"BUILDKITE_BRANCH": "feature/widget", "BUILDKITE_TAG": "v1.0"}, "b-promoted"),
        ({"git_ref": "refs/tags/v1.0"}, {"BUILDKITE_TAG": "v1.0"}, CURRENT_BUILD),
        ({"git_ref": "refs/tags/v1.0"}, {"BUILDKITE_TAG": "refs/tags/v1.0"}, CURRENT_BUILD),
        ({}, {"BUILDKITE_BRANCH": None}, CURRENT_BUILD),
        ({"project_id": "engine"}, {"BUILDKITE_BRANCH": None}, "b-promoted"),
    ],
    ids=[
        "other-project",
        "own-project-own-ref",
        "own-project-by-name",
        "own-project-other-ref",
        "branch-given-as-ref",
        "tag-build-asks-for-branch",
        "tag-build-asks-for-tag",
        "tag-given-as-ref",
        "no-branch-no-tag",
        "other-project-no-branch",
    ],
)
def test_without_build_id_the_own_build_is_taken_only_for_the_own_project_and_ref(
    job, store, hook, project, env, expected
):
    ref = project.get("git_ref", FEATURE)
    owner = project.get("project_id", "widget-build")
    put_build(store, owner, ref, CURRENT_BUILD)
    put_build(store, owner, ref, "b-promoted")
    store.put_pointer(owner, ref, "linux", "b-promoted")

    outcome = download(job.with_env(**env), hook, **project)

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == expected.encode()


def test_the_own_project_and_ref_without_a_build_id_in_the_job_fails(job, store, hook):
    job = job.with_env(BUILDKITE_BRANCH="feature/widget", BUILDKITE_BUILD_ID=None).with_plugin(
        download_step()
    )

    outcome = hook("pre-command", job)

    assert outcome.code == 1
    assert "BUILDKITE_BUILD_ID" in outcome.stderr
    assert store.requests == []


def test_a_requested_ref_without_artifacts_falls_back_to_master(job, store, hook):
    put_build(store, "engine", MASTER, "b-1", b"master")

    outcome = download(job, hook, project_id="engine", build_id="b-1")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"master"
    assert list_prefixes(store)[:2] == [
        f"engine/{FEATURE}/variants/linux/builds/b-1/",
        f"engine/{MASTER}/variants/linux/builds/b-1/",
    ]


def test_the_walk_tries_the_requested_ref_then_master_then_main(job, store, hook):
    put_build(store, "engine", MAIN, "b-main", b"main")
    store.put_pointer("engine", MAIN, "linux", "b-main")

    outcome = download(job, hook, project_id="engine", build_id="LATEST_SUCCESSFUL")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"main"
    pointers = [k for k in object_reads(store) if k.endswith("LATEST_SUCCESSFUL")]
    assert pointers == [
        f"engine/{FEATURE}/variants/linux/LATEST_SUCCESSFUL",
        f"engine/{MASTER}/variants/linux/LATEST_SUCCESSFUL",
        f"engine/{MAIN}/variants/linux/LATEST_SUCCESSFUL",
    ]


def test_master_wins_over_main_when_both_have_the_build(job, store, hook):
    put_build(store, "engine", MASTER, "b-1", b"master")
    put_build(store, "engine", MAIN, "b-1", b"main")

    outcome = download(job, hook, project_id="engine", build_id="b-1")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"master"


def test_an_explicit_id_is_looked_for_under_the_same_id_on_every_ref(job, store, hook):
    put_build(store, "engine", MAIN, "b-1", b"main")
    put_build(store, "engine", MASTER, "b-2", b"master-other-build")

    outcome = download(job, hook, project_id="engine", build_id="b-1")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"main"


@pytest.mark.parametrize("requested, other", [(MASTER, MAIN), (MAIN, MASTER)])
def test_a_request_for_master_or_main_does_not_fall_back_to_the_other(
    job, store, hook, requested, other
):
    put_build(store, "engine", other, "b-1")

    outcome = download(job, hook, project_id="engine", build_id="b-1", git_ref=requested)

    assert outcome.code == 1
    assert list_prefixes(store) == [f"engine/{requested}/variants/linux/builds/b-1/"]


@pytest.mark.parametrize("value", [True, "1", "yes"])
def test_strict_ref_match_disables_the_walk(job, store, hook, value):
    put_build(store, "engine", MASTER, "b-1")

    outcome = download(
        job, hook, project_id="engine", build_id="b-1", require_strict_ref_match=value
    )

    assert outcome.code == 1
    assert list_prefixes(store) == [f"engine/{FEATURE}/variants/linux/builds/b-1/"]


def test_strict_ref_match_false_keeps_the_walk(job, store, hook):
    put_build(store, "engine", MASTER, "b-1", b"master")

    outcome = download(
        job, hook, project_id="engine", build_id="b-1", require_strict_ref_match=False
    )

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"master"


def test_an_empty_pointer_counts_as_no_pointer(job, store, hook):
    # The ref has builds, so an empty build id taken as a prefix would find objects.
    put_build(store, "engine", FEATURE, "b-old", b"feature")
    store.put_pointer("engine", FEATURE, "linux", " \n")
    put_build(store, "engine", MASTER, "b-1", b"master")
    store.put_pointer("engine", MASTER, "linux", "b-1")

    outcome = download(job, hook, project_id="engine", build_id="LATEST_SUCCESSFUL")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"master"
    assert job.files("out") == ["a.bin"]


def test_a_pointer_that_cannot_be_read_fails_the_download_instead_of_falling_back(job, store, hook):
    pointer = f"engine/{FEATURE}/variants/linux/LATEST_SUCCESSFUL"
    put_build(store, "engine", MASTER, "b-1", b"master")
    store.put_pointer("engine", MASTER, "linux", "b-1")
    store.refuse("GET", pointer, 403, "AccessDenied")

    outcome = download(job, hook, project_id="engine", build_id="LATEST_SUCCESSFUL")

    assert outcome.code == 1
    assert "AccessDenied" in outcome.stderr
    assert downloaded(job) is None
    assert object_reads(store) == [pointer]


def test_a_pointer_answered_with_a_bare_404_counts_as_no_pointer(job, store, hook):
    put_build(store, "engine", MASTER, "b-1", b"master")
    store.put_pointer("engine", MASTER, "linux", "b-1")
    store.refuse("GET", f"engine/{FEATURE}/variants/linux/LATEST_SUCCESSFUL", 404)

    outcome = download(job, hook, project_id="engine", build_id="LATEST_SUCCESSFUL")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"master"


def test_a_pointer_to_a_build_without_objects_moves_on_to_the_next_ref(job, store, hook):
    store.put_pointer("engine", FEATURE, "linux", "b-gone")
    put_build(store, "engine", MASTER, "b-1", b"master")
    store.put_pointer("engine", MASTER, "linux", "b-1")

    outcome = download(job, hook, project_id="engine", build_id="LATEST_SUCCESSFUL")

    assert outcome.code == 0, outcome.output
    assert downloaded(job) == b"master"
    assert list_prefixes(store)[0] == f"engine/{FEATURE}/variants/linux/builds/b-gone/"


@pytest.mark.parametrize("marker", ["manifest.json", ""], ids=["manifest-only", "folder-marker"])
def test_any_object_in_the_requested_build_stops_the_walk_even_without_artifacts(
    job, store, hook, marker
):
    store.put(f"engine/{FEATURE}/variants/linux/builds/b-1/{marker}", b"{}")
    put_build(store, "engine", MASTER, "b-1", b"master")

    outcome = download(job, hook, project_id="engine", build_id="b-1")

    assert outcome.code == 1
    assert downloaded(job) is None
    assert "manifest.json" in outcome.error


def test_an_unresolvable_pointer_names_every_ref_and_pointer_it_tried(job, store, hook):
    outcome = download(job, hook, project_id="engine", build_id="LATEST_SUCCESSFUL")

    assert outcome.code == 1
    for fact in [
        "project=engine",
        "variant=linux",
        f"requested_ref={FEATURE}",
        "requested_build_id=LATEST_SUCCESSFUL",
        f"s3://example-artifacts/engine/{FEATURE}/variants/linux/LATEST_SUCCESSFUL",
        f"s3://example-artifacts/engine/{MASTER}/variants/linux/LATEST_SUCCESSFUL",
        f"s3://example-artifacts/engine/{MAIN}/variants/linux/LATEST_SUCCESSFUL",
    ]:
        assert fact in outcome.error
    assert list_prefixes(store) == []


def test_an_unresolvable_build_id_names_every_prefix_it_tried(job, store, hook):
    outcome = download(job, hook, project_id="engine", build_id="b-1")

    assert outcome.code == 1
    for ref in (FEATURE, MASTER, MAIN):
        assert f"ref={ref} build=b-1" in outcome.error
        assert f"s3://example-artifacts/engine/{ref}/variants/linux/builds/b-1/" in outcome.error


def test_resolution_reads_pointers_and_lists_build_prefixes_only(job, store, hook):
    store.put_pointer("engine", FEATURE, "linux", "b-gone")
    put_build(store, "engine", MAIN, "b-main")
    store.put_pointer("engine", MAIN, "linux", "b-main")

    assert download(job, hook, project_id="engine", build_id="LATEST_SUCCESSFUL").code == 0

    assert [(q["prefix"], q.get("max-keys")) for q in listings(store)] == [
        (f"engine/{FEATURE}/variants/linux/builds/b-gone/", "1"),
        (f"engine/{MAIN}/variants/linux/builds/b-main/", "1"),
        (f"engine/{MAIN}/variants/linux/builds/b-main/", None),
    ]
    assert object_reads(store) == [
        f"engine/{FEATURE}/variants/linux/LATEST_SUCCESSFUL",
        f"engine/{MASTER}/variants/linux/LATEST_SUCCESSFUL",
        f"engine/{MAIN}/variants/linux/LATEST_SUCCESSFUL",
        f"engine/{MAIN}/variants/linux/builds/b-main/manifest.json",
        f"engine/{MAIN}/variants/linux/builds/b-main/a.bin",
    ]


def test_each_project_resolves_on_its_own(job, store, hook):
    put_build(store, "engine", MASTER, "b-1", b"engine")
    store.put_pointer("engine", MASTER, "linux", "b-1")
    put_build(store, "gizmo", FEATURE, "b-7", b"gizmo")
    config = {
        "download": {
            "projects": [
                {
                    "project_id": "engine",
                    "variant": "linux",
                    "git_ref": MASTER,
                    "files": ["*.bin"],
                    "output-dir": "engine",
                },
                {
                    "project_id": "gizmo",
                    "build_id": "b-7",
                    "variant": "linux",
                    "git_ref": FEATURE,
                    "files": ["*.bin"],
                    "output-dir": "gizmo",
                },
            ]
        }
    }

    outcome = hook("pre-command", job.with_plugin(config))

    assert outcome.code == 0, outcome.output
    assert (job.workspace / "engine" / "a.bin").read_bytes() == b"engine"
    assert (job.workspace / "gizmo" / "a.bin").read_bytes() == b"gizmo"
