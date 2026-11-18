"""Where the objects of snapshot dependencies live: under _snapshot/v1/ beside the
projects, never inside a build.

In-process: no hook writes a failure record. The pointer's key as promote
writes it is held by test_snapshot_pointer through the hook.
"""

import importlib

import pytest

KEY = "v1-0123456789abcdef0123456789abcdef"
BUILD = "01900000-0000-7000-8000-000000000001"


@pytest.fixture(scope="module")
def layout(library):
    return importlib.import_module("lib.layout")


@pytest.mark.parametrize("prefix, root", [("", ""), ("ci/artifacts", "ci/artifacts/")])
def test_a_variant_of_a_snapshot_has_one_pointer_object(layout, prefix, root):
    assert (
        layout.snapshot_pointer(prefix, "widget-build", KEY, "linux")
        == f"{root}_snapshot/v1/pointers/widget-build/{KEY}/linux.json"
    )


@pytest.mark.parametrize("prefix, root", [("", ""), ("ci/artifacts", "ci/artifacts/")])
def test_the_failure_records_of_a_key_are_one_object_per_build_under_one_prefix(
    layout, prefix, root
):
    records = layout.failure_records(prefix, "widget-build", KEY)

    assert records == f"{root}_snapshot/v1/failures/widget-build/{KEY}/"
    assert layout.failure_record(prefix, "widget-build", KEY, BUILD) == f"{records}{BUILD}.json"


def test_snapshot_objects_sit_beside_the_projects_never_inside_a_build(layout):
    build = layout.build_prefix("ci", "widget-build", "refs/heads/master", "linux", BUILD)
    keys = [
        layout.snapshot_pointer("ci", "widget-build", KEY, "linux"),
        layout.failure_records("ci", "widget-build", KEY),
        layout.failure_record("ci", "widget-build", KEY, BUILD),
    ]

    for key in keys:
        assert key.startswith("ci/_snapshot/v1/")
        assert not key.startswith(layout.key_join("ci", "widget-build") + "/")
        assert not key.startswith(build)
