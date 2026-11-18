"""The snapshot pointer as an object of the bucket: its JSON, the form of every
field, and how it is found and created.

In-process: a hook writes a pointer only with all its fields of their form, so
the refusals of a malformed one, and find, can only be shown here. What
promote writes through the hook is held by test_snapshot_promote.
"""

import dataclasses
import importlib
import json

import pytest
from botocore.exceptions import ClientError

KEY = "v1-0123456789abcdef0123456789abcdef"
COMMIT = "89abcdef0123456789abcdef0123456789abcdef"
BUILD_ID = "01900000-0000-7000-8000-000000000001"
BUILD_URL = "https://buildkite.example.test/example/widget-build/builds/512"
WHERE = f"_snapshot/v1/pointers/widget-build/{KEY}/linux.json"
DOCUMENT = (
    '{"schema":1,"project":"widget-build",'
    f'"key":"{KEY}","variant":"linux","git_ref":"refs/heads/master","commit":"{COMMIT}",'
    f'"build":{{"id":"{BUILD_ID}","number":512,"url":"{BUILD_URL}"}},'
    '"written_at":"2026-11-19T02:15:14.123Z"}'
)


@pytest.fixture(scope="module")
def pointer(library):
    return importlib.import_module("lib.pointer")


@pytest.fixture
def value(pointer):
    return pointer.Pointer(
        project="widget-build",
        key=KEY,
        variant="linux",
        git_ref="refs/heads/master",
        commit=COMMIT,
        build=pointer.Build(id=BUILD_ID, number=512, url=BUILD_URL),
        written_at="2026-11-19T02:15:14.123Z",
    )


def document(**changes):
    """DOCUMENT with members replaced, a member removed (None) or added."""
    fields = json.loads(DOCUMENT)
    for name, change in changes.items():
        if change is None:
            fields.pop(name)
        else:
            fields[name] = change
    return json.dumps(fields).encode()


def build_member(**changes):
    return {**json.loads(DOCUMENT)["build"], **changes}


def test_a_pointer_is_compact_json_with_its_members_in_the_order_of_the_specification(
    pointer, value
):
    assert pointer.encode(value) == DOCUMENT.encode()


def test_a_pointer_reads_back_as_the_value_it_was_written_from(pointer, value):
    assert pointer.decode(pointer.encode(value)) == value


@pytest.mark.parametrize(
    "body, named",
    [
        (b"{", "JSON"),
        (b"\xff\xfe{}", "JSON"),
        (b"[]", "members"),
        (document(schema=2), "schema"),
        (document(schema=True), "schema"),
        (document(schema="1"), "schema"),
        (document(commit=None), "members"),
        (document(extra="x"), "members"),
        (document(build=[]), "build"),
        (document(build={"id": BUILD_ID, "url": BUILD_URL}), "build"),
        (document(build=build_member(pipeline="widget-build")), "build"),
        (document(project="Widget-Build"), "project"),
        (document(project="widget/build"), "project"),
        (document(project=7), "project"),
        (document(key="v1-0123456789abcdef0123456789abcde"), "key"),
        (document(key="v1-0123456789ABCDEF0123456789ABCDEF"), "key"),
        (document(key="v2-0123456789abcdef0123456789abcdef"), "key"),
        (document(variant="linux amd64"), "variant"),
        (document(variant="-linux"), "variant"),
        (document(variant="linux/amd64"), "variant"),
        (document(git_ref="master"), "git_ref"),
        (document(git_ref="refs/heads/"), "git_ref"),
        (document(git_ref="refs/heads/a..b"), "git_ref"),
        (document(git_ref="refs/remotes/origin/master"), "git_ref"),
        (document(commit=COMMIT[:12]), "commit"),
        (document(commit=COMMIT.upper()), "commit"),
        (document(build=build_member(id="build-7")), "build.id"),
        (document(build=build_member(number=-1)), "build.number"),
        (document(build=build_member(number=True)), "build.number"),
        (document(build=build_member(number="512")), "build.number"),
        (document(build=build_member(url="http://buildkite.example.test/b/1")), "build.url"),
        (document(build=build_member(url="https://buildkite.example.test/a b")), "build.url"),
        (document(written_at="2026-11-19T02:15:14Z"), "written_at"),
        (document(written_at="2026-11-19T02:15:14.123+00:00"), "written_at"),
    ],
)
def test_a_body_that_is_not_a_pointer_of_this_schema_is_refused_by_name(
    library, pointer, body, named
):
    with pytest.raises(library.ArtifactsError) as raised:
        pointer.decode(body)

    assert named in str(raised.value)


def test_find_says_none_when_no_build_holds_the_variant(pointer, plugin_bucket, store):
    assert pointer.find(plugin_bucket, "", "widget-build", KEY, "linux") is None

    (get,) = store.plugin_requests("s3")
    assert (get.method, get.key) == ("GET", WHERE)


def test_find_reads_the_pointer_of_the_variant(pointer, plugin_bucket, store, value):
    store.put(f"ci/{WHERE}", DOCUMENT.encode())

    assert pointer.find(plugin_bucket, "ci", "widget-build", KEY, "linux") == value


def test_find_refuses_a_pointer_that_names_another_snapshot_than_its_key(
    library, pointer, plugin_bucket, store
):
    store.put(WHERE, document(variant="windows"))

    with pytest.raises(library.ArtifactsError) as raised:
        pointer.find(plugin_bucket, "", "widget-build", KEY, "linux")

    assert WHERE in str(raised.value) and "windows" in str(raised.value)


def test_find_names_the_key_of_a_pointer_that_does_not_read(library, pointer, plugin_bucket, store):
    store.put(WHERE, document(commit="HEAD"))

    with pytest.raises(library.ArtifactsError) as raised:
        pointer.find(plugin_bucket, "", "widget-build", KEY, "linux")

    assert WHERE in str(raised.value) and "commit" in str(raised.value)


def test_find_raises_a_refused_read_as_the_store_refused_it(pointer, plugin_bucket, store):
    store.refuse("GET", WHERE, 403, "AccessDenied")

    with pytest.raises(ClientError) as raised:
        pointer.find(plugin_bucket, "", "widget-build", KEY, "linux")

    assert raised.value.response["Error"]["Code"] == "AccessDenied"


def test_create_writes_the_pointer_once_and_returns_it(pointer, plugin_bucket, store, value):
    assert pointer.create(plugin_bucket, "ci", value) == value

    assert store.get(f"ci/{WHERE}") == DOCUMENT.encode()
    (put,) = store.plugin_requests("s3")
    assert (put.method, put.key, put.if_none_match) == ("PUT", f"ci/{WHERE}", "*")
    assert put.content_type == "application/json"


def test_create_returns_the_pointer_another_build_holds_and_leaves_it(
    pointer, plugin_bucket, store, value
):
    held = document(
        build=build_member(id="01900000-0000-7000-8000-0000000000ff", number=511),
        commit="f" * 40,
    )
    store.put(WHERE, held)

    holder = pointer.create(plugin_bucket, "", value)

    assert (holder.build.number, holder.commit) == (511, "f" * 40)
    assert store.get(WHERE) == held


@pytest.mark.parametrize(
    "changes, named",
    [
        ({"variant": "linux amd64"}, "variant"),
        ({"project": "Widget"}, "project"),
        ({"git_ref": "master"}, "git_ref"),
        ({"commit": ""}, "commit"),
    ],
)
def test_create_refuses_a_pointer_of_the_wrong_form_before_any_request(
    library, pointer, plugin_bucket, store, value, changes, named
):
    with pytest.raises(library.ArtifactsError) as raised:
        pointer.create(plugin_bucket, "", dataclasses.replace(value, **changes))

    assert named in str(raised.value)
    assert store.plugin_requests() == []
