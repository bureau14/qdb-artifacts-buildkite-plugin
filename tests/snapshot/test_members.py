from __future__ import annotations

import pytest

from lib.snapshot.members import member


class Refused(ValueError):
    """The error a reader names."""


def test_a_member_of_its_type_is_returned():
    assert member({"n": 3}, "n", int, "doc", Refused) == 3
    assert member({"k": None}, "k", (str, type(None)), "doc", Refused) is None


@pytest.mark.parametrize(
    "d, kind, message",
    [
        ([], int, "doc: no n"),
        ({}, int, "doc: no n"),
        ({"n": "3"}, int, "doc.n: expected int, got str"),
        ({"n": True}, int, "doc.n: expected int, got bool"),
        ({"n": 3}, (str, type(None)), "doc.n: expected str or NoneType, got int"),
    ],
)
def test_any_other_is_the_readers_error_naming_where(d, kind, message):
    with pytest.raises(Refused) as raised:
        member(d, "n", kind, "doc", Refused)
    assert str(raised.value) == message
