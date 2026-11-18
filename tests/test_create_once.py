"""Creating an object only where the bucket holds none, with If-None-Match: *,
the way snapshot pointers are written.

In-process: create has no caller of its own; promote's use of it is held by
test_snapshot_pointer through the hook. The local store honours the condition
as S3 does and answers a create over an existing key with 412.
"""

import importlib

import pytest
from botocore.exceptions import ClientError

KEY = "_snapshot/v1/pointers/widget-build/v1-0123456789abcdef0123456789abcdef/linux.json"


@pytest.fixture
def bucket(library, library_environment, store):
    """The plugin's interface to the bucket, with the plugin's own client."""
    objects = importlib.import_module("lib.objects")
    clients = importlib.import_module("lib.clients")
    cfg = library.StoreConfig(
        backend="s3", destination=f"s3://{store.bucket}", endpoint_url=store.endpoint
    )
    return objects.Store(clients.s3_client(cfg, library.ObjectAuth()), store.bucket)


def test_create_writes_where_the_bucket_holds_nothing_and_says_it_created(bucket, store):
    assert bucket.create(KEY, b"first", "application/json") is True

    assert store.get(KEY) == b"first"
    (put,) = store.plugin_requests("s3")
    assert (put.method, put.key, put.if_none_match) == ("PUT", KEY, "*")
    assert put.content_type == "application/json"


def test_create_leaves_an_existing_object_as_it_is_and_says_it_did_not_create(bucket, store):
    store.put(KEY, b"held")

    assert bucket.create(KEY, b"second", "application/json") is False

    assert store.get(KEY) == b"held"
    assert [r.method for r in store.plugin_requests("s3")] == ["PUT"]


def test_of_two_creates_of_one_key_only_the_first_creates(bucket, store):
    assert [bucket.create(KEY, body, "application/json") for body in (b"a", b"b")] == [True, False]

    assert store.get(KEY) == b"a"


def test_a_store_that_answers_412_is_read_as_an_existing_object(bucket, store):
    store.refuse("PUT", KEY, 412, "PreconditionFailed")

    assert bucket.create(KEY, b"body", "application/json") is False

    assert store.stored_keys() == []


def test_any_other_refusal_of_the_store_is_raised_as_it_is(bucket, store):
    store.refuse("PUT", KEY, 403, "AccessDenied")

    with pytest.raises(ClientError) as raised:
        bucket.create(KEY, b"body", "application/json")

    assert raised.value.response["Error"]["Code"] == "AccessDenied"
    assert store.stored_keys() == []
