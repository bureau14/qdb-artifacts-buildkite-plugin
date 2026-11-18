"""Creating an object only where the bucket holds none, with If-None-Match: *,
the way snapshot pointers are written.

In-process: a hook creates only snapshot pointers, held by
test_snapshot_promote, and never meets the store's other answers. The local
store honours the condition as S3 does and answers a create over an existing
key with 412.
"""

import pytest
from botocore.exceptions import ClientError

KEY = "_snapshot/v1/pointers/widget-build/v1-0123456789abcdef0123456789abcdef/linux.json"


def test_create_writes_where_the_bucket_holds_nothing_and_says_it_created(plugin_bucket, store):
    assert plugin_bucket.create(KEY, b"first", "application/json") is True

    assert store.get(KEY) == b"first"
    (put,) = store.plugin_requests("s3")
    assert (put.method, put.key, put.if_none_match) == ("PUT", KEY, "*")
    assert put.content_type == "application/json"


def test_create_leaves_an_existing_object_as_it_is_and_says_it_did_not_create(plugin_bucket, store):
    store.put(KEY, b"held")

    assert plugin_bucket.create(KEY, b"second", "application/json") is False

    assert store.get(KEY) == b"held"
    assert [r.method for r in store.plugin_requests("s3")] == ["PUT"]


def test_of_two_creates_of_one_key_only_the_first_creates(plugin_bucket, store):
    created = [plugin_bucket.create(KEY, body, "application/json") for body in (b"a", b"b")]

    assert created == [True, False]
    assert store.get(KEY) == b"a"


def test_a_store_that_answers_412_is_read_as_an_existing_object(plugin_bucket, store):
    store.refuse("PUT", KEY, 412, "PreconditionFailed")

    assert plugin_bucket.create(KEY, b"body", "application/json") is False

    assert store.stored_keys() == []


@pytest.mark.parametrize(
    "status, code, raised_code",
    [(403, "AccessDenied", "AccessDenied"), (412, "", "412")],
    # A 412 without a body is how a store answers a HEAD; S3, R2 and the local
    # store answer a create over an existing object with PreconditionFailed.
    ids=["access-denied", "412-without-a-body"],
)
def test_any_other_refusal_of_the_store_is_raised_as_it_is(
    plugin_bucket, store, status, code, raised_code
):
    store.refuse("PUT", KEY, status, code)

    with pytest.raises(ClientError) as raised:
        plugin_bucket.create(KEY, b"body", "application/json")

    assert raised.value.response["Error"]["Code"] == raised_code
    assert store.stored_keys() == []
