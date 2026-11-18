"""The plan as a JSON document: its exact form, its limit, its reading, its use."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest
from plans import DUCK, PLAN_ID, PYTHON, QDB, REST, T0, URLS, plan, sha

from lib.forms import iso_utc
from lib.snapshot.agent import META_DATA_LIMIT
from lib.snapshot.plan import (
    Node,
    Plan,
    PlanError,
    RootInfo,
    decode_plan,
    dependencies_first,
    encode_plan,
    planned_node,
)


def small(root_key: str | None = "v1-" + "b" * 32) -> Plan:
    """qdb-api-rest on master with its dependency quasardb-build."""
    return Plan(
        id=PLAN_ID,
        context="master",
        rebuild="none",
        created_at="2025-11-17T12:00:00.000Z",
        root=RootInfo(REST, 7, "https://ci.example/qdb-api-rest/7"),
        nodes={
            REST: Node(URLS[REST], "refs/heads/master", "2" * 40, root_key, "master", (QDB,)),
            QDB: Node(URLS[QDB], "refs/heads/master", "1" * 40, "v1-" + "a" * 32, "master"),
        },
    )


def test_iso_utc():
    assert iso_utc(T0) == "2025-11-17T12:00:00.000Z"
    assert iso_utc(T0 + 0.25) == "2025-11-17T12:00:00.250Z"


def test_the_document_is_compact_json_with_sorted_keys():
    rest = (
        '"qdb-api-rest":{"commit":"' + "2" * 40 + '","deps":["quasardb-build"],'
        '"key":"v1-' + "b" * 32 + '","label":"master","ref":"refs/heads/master",'
        '"repository":"https://github.com/bureau14/qdb-api-rest.git"}'
    )
    qdb = (
        '"quasardb-build":{"commit":"' + "1" * 40 + '","deps":[],"key":"v1-' + "a" * 32 + '",'
        '"label":"master","ref":"refs/heads/master",'
        '"repository":"https://github.com/bureau14/quasardb.git"}'
    )
    assert encode_plan(small()) == (
        '{"context":"master","created_at":"2025-11-17T12:00:00.000Z",'
        f'"id":"{PLAN_ID}",'
        f'"nodes":{{{rest},{qdb}}},'
        '"rebuild":"none",'
        '"root":{"build_number":7,"build_url":"https://ci.example/qdb-api-rest/7",'
        '"project":"qdb-api-rest"},"schema":2}'
    )


def test_a_root_without_a_snapshot_has_a_null_key():
    assert '"key":null' in encode_plan(small(root_key=None))
    assert decode_plan(encode_plan(small(root_key=None))).nodes[REST].key is None


@pytest.mark.parametrize("root_has_snapshot", [True, False])
@pytest.mark.parametrize("rebuild", ["none", "one", "all"])
def test_a_plan_reads_back_as_it_was_written(root_has_snapshot, rebuild):
    p = plan(rebuild=rebuild, root_has_snapshot=root_has_snapshot)
    assert decode_plan(encode_plan(p)) == p


def test_a_plan_of_the_limit_is_written_and_one_byte_more_is_refused():
    p = small()
    room = META_DATA_LIMIT - len(encode_plan(p).encode())
    url = p.root.build_url + "x" * room
    at_limit = replace(p, root=replace(p.root, build_url=url))
    assert len(encode_plan(at_limit).encode()) == META_DATA_LIMIT
    over = replace(p, root=replace(p.root, build_url=url + "x"))
    with pytest.raises(PlanError, match=rf"the plan is {META_DATA_LIMIT + 1} bytes, more than"):
        encode_plan(over)


def test_the_limit_counts_bytes_not_characters():
    p = small()
    room = META_DATA_LIMIT - len(encode_plan(p).encode())
    two_bytes = chr(0xE9)
    url = p.root.build_url + two_bytes * (room // 2 + 1)
    over = replace(p, root=replace(p.root, build_url=url))
    with pytest.raises(PlanError, match="bytes, more than"):
        encode_plan(over)


def document(**changes):
    d = json.loads(encode_plan(small()))
    d.update(changes)
    return d


def with_node(name, **changes):
    d = json.loads(encode_plan(small()))
    d["nodes"][name].update(changes)
    return d


@pytest.mark.parametrize(
    "d, detail",
    [
        ([], "not a plan of schema 2"),
        (document(schema=1), "not a plan of schema 2"),
        (document(schema=2.0), "not a plan of schema 2"),
        (document(schema="2"), "not a plan of schema 2"),
        (document(rebuild="some"), "plan.rebuild"),
        (document(id=7), r"plan.id: expected str, got int"),
        ({k: v for k, v in document().items() if k != "context"}, "plan: no context"),
        (document(root={"project": REST, "build_number": "7", "build_url": ""}), "build_number"),
        (document(root={"project": REST, "build_number": True, "build_url": ""}), "build_number"),
        (with_node(REST, deps=[1]), "deps: 1 is not a pipeline slug"),
        (with_node(REST, deps=["nope"]), "depends on nope, not in the plan"),
        (with_node(QDB, key=None), "quasardb-build has no key, and only the root may lack one"),
        (with_node(QDB, key=7), "key: expected str or NoneType"),
        (with_node(QDB, commit=None), "commit: expected str"),
        (
            document(
                root={"project": DUCK, "build_number": 7, "build_url": "https://ci.example/7"}
            ),
            "is not a node",
        ),
    ],
)
def test_a_document_that_is_not_a_plan_is_refused(d, detail):
    with pytest.raises(PlanError, match=detail):
        decode_plan(json.dumps(d))


def with_nodes(nodes):
    d = json.loads(encode_plan(small()))
    d["nodes"] = nodes
    return d


# A chain node puts the label, the commit and the project of a node into a
# trigger step it uploads; meta-data can be written by any job of the root build.
@pytest.mark.parametrize(
    "d, detail",
    [
        (
            with_node(QDB, label="${BUILDKITE_AGENT_ACCESS_TOKEN}\nx: y"),
            r"nodes.quasardb-build.label",
        ),
        (with_node(QDB, commit="--upload-pack=x"), r"nodes.quasardb-build.commit"),
        (with_node(QDB, commit="A" * 40), r"nodes.quasardb-build.commit"),
        (with_node(QDB, ref="-x"), r"nodes.quasardb-build.ref"),
        (with_node(QDB, ref="refs/heads/a..b"), r"nodes.quasardb-build.ref"),
        (with_node(QDB, label="snapshot/a b"), r"nodes.quasardb-build.label"),
        (with_node(QDB, repository="ext::sh -c x"), r"nodes.quasardb-build.repository"),
        (with_node(QDB, repository="file:///srv/q.git"), r"nodes.quasardb-build.repository"),
        (with_node(QDB, key="v1-../../" + "a" * 26), r"nodes.quasardb-build.key"),
        (with_node(QDB, key="v2-" + "a" * 32), r"nodes.quasardb-build.key"),
        (with_node(REST, deps=["$x"]), r"nodes.qdb-api-rest.deps"),
        (document(context="x/../y"), r"plan.context"),
        (document(context="HEAD"), r"plan.context"),
        (document(id="$(touch x)"), r"plan.id"),
        (document(root={"project": "$x", "build_number": 7, "build_url": "https://x/7"}), "root"),
        (document(root={"project": REST, "build_number": 7, "build_url": "x\ny"}), "build_url"),
    ],
)
def test_every_value_of_a_plan_is_checked(d, detail):
    with pytest.raises(PlanError, match=detail) as e:
        decode_plan(json.dumps(d))
    assert all(" " <= c <= "~" for c in str(e.value)), str(e.value)


@pytest.mark.parametrize(
    "branch", ["feature", "fix+1", "x/${BUILDKITE_AGENT_ACCESS_TOKEN}", "caf" + chr(0xE9), "a'b"]
)
def test_a_plan_on_any_branch_git_accepts_reads_back_with_its_snapshot_labels(branch):
    p = small()
    nodes = {
        REST: replace(p.nodes[REST], ref="refs/heads/" + branch, label=branch),
        QDB: replace(p.nodes[QDB], label="snapshot/" + branch),
    }
    on_branch = replace(p, context=branch, nodes=nodes)
    assert decode_plan(encode_plan(on_branch)) == on_branch


def test_a_node_named_other_than_a_pipeline_is_refused():
    d = with_nodes({**document()["nodes"], "$(x)\n": document()["nodes"][QDB]})
    with pytest.raises(PlanError, match="not a pipeline slug") as e:
        decode_plan(json.dumps(d))
    assert "\n" not in str(e.value)


def test_text_that_is_not_json_is_refused():
    with pytest.raises(PlanError, match="not JSON"):
        decode_plan("{")


def test_json_nested_deeper_than_the_parser_goes_is_refused():
    # Python 3.9's parser gives up (RecursionError); newer ones read a list.
    with pytest.raises(PlanError, match="^plan: not "):
        decode_plan("[" * 100_000 + "]" * 100_000)


def test_a_build_of_the_chain_finds_its_node_at_its_commit():
    p = decode_plan(encode_plan(plan()))
    assert planned_node(p, REST, sha(REST)) == p.nodes[REST]
    with pytest.raises(PlanError, match="plans qdb-api-rest at"):
        planned_node(p, REST, "0" * 40)
    with pytest.raises(PlanError, match="has no node unknown"):
        planned_node(p, "unknown", "0" * 40)


def test_dependencies_first():
    p = plan()
    assert dependencies_first(p, REST) == [QDB, DUCK, REST]
    assert dependencies_first(p, PYTHON) == [QDB, DUCK, REST, PYTHON]
    assert dependencies_first(p, QDB) == [QDB]


def test_dependencies_first_refuses_a_cycle_in_a_plan_read_from_elsewhere():
    d = with_node(QDB, deps=[REST])
    with pytest.raises(PlanError, match="cycle"):
        dependencies_first(decode_plan(json.dumps(d)), REST)
