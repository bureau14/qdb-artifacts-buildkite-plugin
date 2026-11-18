"""The library exported by lib/__init__.py, imported as "lib" from the plugin root."""

import dataclasses
import datetime

import boto3
import pytest

MASTER = "refs/heads/master"
FEATURE = "refs/heads/feature/widget"
PREFIX = f"engine/{FEATURE}/variants/linux/builds/b-9"
EXPORTED = [
    "ObjectAuth",
    "ResolvedArtifact",
    "ResolvedBuild",
    "StoreConfig",
    "aws_clients",
    "download_artifacts",
    "load_store_config",
    "resolve_artifacts",
    "resolve_object_auth",
]


def field_names(cls):
    return [f.name for f in dataclasses.fields(cls)]


def s3_config(library, store):
    return library.StoreConfig(
        backend="s3", destination=f"s3://{store.bucket}", endpoint_url=store.endpoint
    )


def put_promoted_build(store, ref=FEATURE):
    files = {
        "artifacts/widget-server.tar.zst": b"server",
        "artifacts/widget-utils.tar.zst": b"utils",
        "widget-cp313.whl": b"wheel",
        "notes.txt": b"notes",
    }
    store.put_build("engine", ref, "linux", "b-9", files)
    store.put_pointer("engine", ref, "linux", "b-9")


def resolve(library, store, **overrides):
    arguments = {
        "cfg": s3_config(library, store),
        "auth": library.ObjectAuth(),
        "project_id": "engine",
        "build_id": "LATEST_SUCCESSFUL",
        "git_ref": FEATURE,
        "variant": "linux",
        "patterns": ["artifacts/*-server.tar.zst", "*.whl"],
        "require_strict_ref_match": True,
        **overrides,
    }
    return library.resolve_artifacts(**arguments)


def test_the_package_exports_exactly_nine_names(library):
    assert sorted(library.__all__) == EXPORTED
    assert all(hasattr(library, name) for name in EXPORTED)


def test_the_configuration_types_have_their_fields_and_defaults(library):
    assert field_names(library.StoreConfig) == [
        "backend",
        "destination",
        "endpoint_url",
        "r2_account_id",
        "r2_access_key_id",
        "r2_secret_access_key",
        "artifacts_domain",
    ]
    assert field_names(library.ObjectAuth) == ["access_key_id", "secret_access_key"]
    cfg = library.StoreConfig(backend="s3", destination="s3://example-artifacts/ci")
    assert (cfg.endpoint_url, cfg.r2_account_id, cfg.artifacts_domain) == (None, None, None)
    auth = library.ObjectAuth()
    assert (auth.access_key_id, auth.secret_access_key) == (None, None)


def test_the_result_types_have_their_fields(library):
    assert field_names(library.ResolvedBuild) == [
        "requested_ref",
        "requested_build_id",
        "ref",
        "build_id",
        "prefix",
        "fallback_applied",
        "pointer_lastmod",
    ]
    assert field_names(library.ResolvedArtifact) == ["key", "rel", "size", "entry_rules"]


def test_all_four_types_are_frozen(library):
    instances = [
        library.StoreConfig(backend="s3", destination="b"),
        library.ObjectAuth(),
        library.ResolvedBuild("r", "b", "r", "b", "p", False, None),
        library.ResolvedArtifact("k", "r", 1, []),
    ]
    for instance in instances:
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(instance, field_names(type(instance))[0], "changed")


def test_resolve_with_the_outside_callers_arguments_returns_bucket_build_and_objects(
    library, library_environment, store
):
    put_promoted_build(store)

    result = resolve(library, store)

    assert isinstance(result, tuple) and len(result) == 3
    bucket, resolved, objects = result
    assert bucket == store.bucket
    assert (resolved.requested_ref, resolved.requested_build_id) == (FEATURE, "LATEST_SUCCESSFUL")
    assert (resolved.ref, resolved.build_id, resolved.prefix) == (FEATURE, "b-9", PREFIX)
    assert resolved.fallback_applied is False
    assert isinstance(resolved.pointer_lastmod, datetime.datetime)
    assert resolved.pointer_lastmod.utcoffset() == datetime.timedelta(0)
    assert [(o.key, o.rel, o.size, o.entry_rules) for o in objects] == [
        (
            f"{PREFIX}/artifacts/widget-server.tar.zst",
            "artifacts/widget-server.tar.zst",
            6,
            [(None, None)],
        ),
        (f"{PREFIX}/widget-cp313.whl", "widget-cp313.whl", 5, [(None, None)]),
    ]


def test_resolve_transfers_no_artifact(library, library_environment, store):
    put_promoted_build(store)

    resolve(library, store)

    reads = [r.path for r in store.plugin_requests("s3") if r.method == "GET"]
    assert not [p for p in reads if p.endswith((".tar.zst", ".whl"))]


def test_entry_rules_list_every_matching_filter_in_configuration_order(
    library, library_environment, store
):
    put_promoted_build(store)

    _, _, objects = resolve(
        library,
        store,
        patterns=["*-server.tar.zst!bin/*", "*.txt", "*-server.tar.zst!lib/*"],
    )

    assert [(o.rel, o.entry_rules) for o in objects] == [
        ("artifacts/widget-server.tar.zst", [("bin/*", "bin/"), ("lib/*", "lib/")]),
        ("notes.txt", [(None, None)]),
    ]


def test_exclude_patterns_drop_matched_objects(library, library_environment, store):
    put_promoted_build(store)

    _, _, objects = resolve(library, store, patterns=["*"], exclude_patterns=["*.tar.zst"])

    assert [o.rel for o in objects] == ["notes.txt", "widget-cp313.whl"]


def test_an_explicit_build_id_resolves_without_a_pointer_time(library, library_environment, store):
    put_promoted_build(store)

    _, resolved, _ = resolve(library, store, build_id="b-9")

    assert (resolved.requested_build_id, resolved.build_id) == ("b-9", "b-9")
    assert resolved.pointer_lastmod is None


def test_without_strict_ref_match_the_library_falls_back_to_master(
    library, library_environment, store
):
    put_promoted_build(store, ref=MASTER)

    _, resolved, objects = resolve(library, store, require_strict_ref_match=False)

    assert (resolved.requested_ref, resolved.ref, resolved.fallback_applied) == (
        FEATURE,
        MASTER,
        True,
    )
    assert objects[0].key.startswith(f"engine/{MASTER}/")


def test_resolve_walks_to_master_unless_asked_for_a_strict_ref_match(
    library, library_environment, store
):
    put_promoted_build(store, ref=MASTER)

    _, resolved, _ = library.resolve_artifacts(
        cfg=s3_config(library, store),
        auth=library.ObjectAuth(),
        project_id="engine",
        build_id="LATEST_SUCCESSFUL",
        git_ref=FEATURE,
        variant="linux",
        patterns=["*.whl"],
    )

    assert (resolved.ref, resolved.fallback_applied) == (MASTER, True)


def test_an_unresolvable_build_ends_the_process_today(
    library, library_environment, store, expect_library_failure
):
    put_promoted_build(store, ref=MASTER)

    with expect_library_failure() as failure:
        resolve(library, store)

    assert "project=engine" in failure.text and FEATURE in failure.text
    assert f"ref={MASTER}" not in failure.text


def test_a_selection_that_matches_nothing_ends_the_process_today(
    library, library_environment, store, expect_library_failure
):
    put_promoted_build(store)

    with expect_library_failure() as failure:
        resolve(library, store, patterns=["*.deb"])

    assert "*.deb" in failure.text and "notes.txt" in failure.text


def test_download_creates_the_output_dir_writes_the_selection_and_returns_it(
    library, library_environment, store, tmp_path
):
    put_promoted_build(store)
    out = tmp_path / "new" / "dir"

    bucket, resolved, objects = library.download_artifacts(
        cfg=s3_config(library, store),
        auth=library.ObjectAuth(),
        project_id="engine",
        build_id="LATEST_SUCCESSFUL",
        git_ref=FEATURE,
        variant="linux",
        patterns=["artifacts/*"],
        require_strict_ref_match=True,
        output_dir=str(out),
        extract=False,
        parallel=2,
        concurrency=3,
    )

    assert (bucket, resolved.build_id) == (store.bucket, "b-9")
    assert [o.rel for o in objects] == [
        "artifacts/widget-server.tar.zst",
        "artifacts/widget-utils.tar.zst",
    ]
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()) == [
        "artifacts/widget-server.tar.zst",
        "artifacts/widget-utils.tar.zst",
    ]


def test_download_extracts_with_entry_rules_and_does_not_clean(
    library, library_environment, store, tmp_path, archive
):
    body = archive(".tar.zst", {"bin/widgetd": b"server", "lib/libwidget.so": b"lib"})
    store.put_build("engine", FEATURE, "linux", "b-9", {"widget-server.tar.zst": body})
    (tmp_path / "keep.txt").write_bytes(b"keep")

    library.download_artifacts(
        cfg=s3_config(library, store),
        auth=library.ObjectAuth(),
        project_id="engine",
        build_id="b-9",
        git_ref=FEATURE,
        variant="linux",
        patterns=["*-server.tar.zst!bin/*"],
        output_dir=str(tmp_path),
        extract=True,
    )

    assert sorted(p.name for p in tmp_path.iterdir()) == ["keep.txt", "widgetd"]


def test_download_writes_archives_unextracted_into_the_working_directory_by_default(
    library, library_environment, store, tmp_path, monkeypatch, archive
):
    body = archive(".tar.zst", {"bin/widgetd": b"server"})
    store.put_build("engine", FEATURE, "linux", "b-9", {"widget-server.tar.zst": body})
    working_directory = tmp_path / "cwd"
    working_directory.mkdir()
    monkeypatch.chdir(working_directory)

    library.download_artifacts(
        cfg=s3_config(library, store),
        auth=library.ObjectAuth(),
        project_id="engine",
        build_id="b-9",
        git_ref=FEATURE,
        variant="linux",
        patterns=["*.tar.zst"],
    )

    assert [p.name for p in working_directory.iterdir()] == ["widget-server.tar.zst"]
    assert (working_directory / "widget-server.tar.zst").read_bytes() == body


@pytest.mark.parametrize("backend", ["s3", "r2"])
def test_load_store_config_reads_the_environment_with_a_given_ssm_client(
    library, library_environment, store, backend
):
    ssm = boto3.client("ssm")

    cfg = library.load_store_config(ssm)

    env = library_environment
    assert (cfg.backend, cfg.destination, cfg.endpoint_url, cfg.artifacts_domain) == (
        backend,
        env["ARTIFACTS_DESTINATION"],
        env["ARTIFACTS_ENDPOINT_URL"],
        env["ARTIFACTS_DOMAIN"],
    )
    r2_values = (cfg.r2_account_id, cfg.r2_access_key_id, cfg.r2_secret_access_key)
    if backend == "r2":
        assert r2_values == (
            env["ARTIFACTS_R2_ACCOUNT_ID"],
            env["ARTIFACTS_R2_ACCESS_KEY_ID"],
            env["ARTIFACTS_R2_SECRET_ACCESS_KEY"],
        )
    else:
        assert r2_values == (None, None, None)


@pytest.mark.parametrize("backend", ["r2"])
def test_the_r2_endpoint_defaults_to_the_account_endpoint(
    library, library_environment, store, monkeypatch, backend
):
    monkeypatch.delenv("ARTIFACTS_ENDPOINT_URL")

    cfg = library.load_store_config(boto3.client("ssm"))

    account = library_environment["ARTIFACTS_R2_ACCOUNT_ID"]
    assert cfg.endpoint_url == f"https://{account}.r2.cloudflarestorage.com"


def test_placeholders_are_refused_only_in_r2_values(
    library, library_environment, store, monkeypatch
):
    monkeypatch.setenv("ARTIFACTS_DESTINATION", "TODO")
    monkeypatch.setenv("ARTIFACTS_DOMAIN", "CHANGEME")

    cfg = library.load_store_config(boto3.client("ssm"))

    assert (cfg.destination, cfg.artifacts_domain) == ("TODO", "CHANGEME")


def test_a_configuration_failure_ends_the_process_today(
    library, library_environment, store, monkeypatch, expect_library_failure
):
    monkeypatch.setenv("ARTIFACTS_BACKEND", "gcs")

    with expect_library_failure() as failure:
        library.load_store_config(boto3.client("ssm"))

    assert "gcs" in failure.text


@pytest.mark.parametrize("permission", ["object-read-only", "object-read-write"])
@pytest.mark.parametrize("backend", ["s3", "r2"])
def test_object_auth_is_the_r2_key_pair_or_empty_whatever_the_permission(
    library, library_environment, store, backend, permission
):
    ssm = boto3.client("ssm")
    cfg = library.load_store_config(ssm)

    auth = library.resolve_object_auth(ssm, cfg, permission=permission)

    if backend == "r2":
        assert (auth.access_key_id, auth.secret_access_key) == (
            library_environment["ARTIFACTS_R2_ACCESS_KEY_ID"],
            library_environment["ARTIFACTS_R2_SECRET_ACCESS_KEY"],
        )
    else:
        assert auth == library.ObjectAuth()


def test_aws_clients_returns_an_s3_and_an_ssm_client(library, library_environment, store):
    s3, ssm = library.aws_clients()

    assert s3.meta.service_model.service_name == "s3"
    assert ssm.meta.service_model.service_name == "ssm"
    assert ssm.meta.region_name == library_environment["AWS_DEFAULT_REGION"]
    assert (
        library.load_store_config(ssm).destination == library_environment["ARTIFACTS_DESTINATION"]
    )


def test_aws_clients_reads_ssm_in_eu_west_1_without_a_region(
    library, library_environment, monkeypatch
):
    monkeypatch.delenv("AWS_DEFAULT_REGION")

    _, ssm = library.aws_clients()

    assert ssm.meta.region_name == "eu-west-1"
