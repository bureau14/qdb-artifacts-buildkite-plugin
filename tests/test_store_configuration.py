"""Where the store configuration comes from: environment, then SSM, then defaults."""

import pytest

CONFIG = "/services/buildkite/config/artifacts/object-store"
BACKEND = f"{CONFIG}/backend"
DESTINATION = f"{CONFIG}/destination"
ENDPOINT_URL = f"{CONFIG}/endpoint-url"
ACCOUNT_ID = f"{CONFIG}/r2/account-id"
ACCESS_KEY_ID = f"{CONFIG}/r2/access-key-id"
DOMAIN = f"{CONFIG}/r2/artifacts-domain"
SECRET_ACCESS_KEY = "/services/buildkite/credentials/artifacts/r2/secret-access-key"

POINTER = "widget-build/refs/heads/master/variants/v/LATEST_SUCCESSFUL"
R2_VALUES = [
    ("ARTIFACTS_R2_ACCOUNT_ID", ACCOUNT_ID),
    ("ARTIFACTS_R2_ACCESS_KEY_ID", ACCESS_KEY_ID),
    ("ARTIFACTS_R2_SECRET_ACCESS_KEY", SECRET_ACCESS_KEY),
]
NO_STORE_ENV = {
    "ARTIFACTS_BACKEND": None,
    "ARTIFACTS_DESTINATION": None,
    "ARTIFACTS_DOMAIN": None,
    "ARTIFACTS_R2_ACCOUNT_ID": None,
    "ARTIFACTS_R2_ACCESS_KEY_ID": None,
    "ARTIFACTS_R2_SECRET_ACCESS_KEY": None,
}


def promote(job, cli):
    return cli(
        job.with_options(PROMOTE_VARIANT="v", PROMOTE_GIT_REF="refs/heads/master"), "post-command"
    )


def parameters_read(store):
    return [r.body["Name"] for r in store.plugin_requests("ssm")]


def put_r2_parameters(store, job, **overrides):
    values = {
        BACKEND: "r2",
        DESTINATION: f"s3://{store.bucket}",
        DOMAIN: "artifacts.example.test",
        ENDPOINT_URL: store.endpoint,
        ACCOUNT_ID: "0123456789abcdef0123456789abcdef",
        ACCESS_KEY_ID: "ssm-r2-access-key",
        SECRET_ACCESS_KEY: "ssm-r2-secret",
        **overrides,
    }
    for name, value in values.items():
        if value is not None:
            store.put_parameter(name, value, secure=name == SECRET_ACCESS_KEY)


@pytest.mark.parametrize("backend", ["s3", "r2"])
def test_the_environment_alone_configures_the_store_without_ssm(job, store, cli, backend):
    outcome = promote(job, cli)

    assert outcome.code == 0, outcome.output
    assert store.plugin_requests("ssm") == []
    assert store.get(POINTER) == job.env["BUILDKITE_BUILD_ID"].encode()


def test_without_environment_the_s3_store_is_read_from_three_parameters(job, store, cli):
    store.put_parameter(BACKEND, "s3")
    store.put_parameter(DESTINATION, f"s3://{store.bucket}/from-ssm")

    outcome = promote(job.with_env(**NO_STORE_ENV), cli)

    assert outcome.code == 0, outcome.output
    assert parameters_read(store) == [BACKEND, DESTINATION, DOMAIN]
    assert store.stored_keys() == [f"from-ssm/{POINTER}"]


def test_the_backend_defaults_to_s3(job, store, cli):
    store.put_parameter(DESTINATION, f"s3://{store.bucket}")

    outcome = promote(job.with_env(**NO_STORE_ENV), cli)

    assert outcome.code == 0, outcome.output
    (put,) = store.plugin_requests("s3")
    assert put.access_key_id == job.env["AWS_ACCESS_KEY_ID"]


def test_without_environment_the_r2_store_is_read_from_seven_parameters(job, store, cli):
    put_r2_parameters(store, job)

    outcome = promote(job.with_env(**NO_STORE_ENV, ARTIFACTS_ENDPOINT_URL=None), cli)

    assert outcome.code == 0, outcome.output
    assert parameters_read(store) == [
        BACKEND,
        DESTINATION,
        DOMAIN,
        ENDPOINT_URL,
        ACCOUNT_ID,
        ACCESS_KEY_ID,
        SECRET_ACCESS_KEY,
    ]
    assert all(r.body["WithDecryption"] is True for r in store.plugin_requests("ssm"))
    (put,) = store.plugin_requests("s3")
    assert (put.access_key_id, put.region) == ("ssm-r2-access-key", "auto")


def test_the_environment_wins_over_ssm_and_ssm_is_then_not_read(job, store, cli):
    store.put_parameter(DESTINATION, f"s3://{store.bucket}/from-ssm")

    outcome = promote(job.with_env(ARTIFACTS_DESTINATION=f"s3://{store.bucket}/from-env"), cli)

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == [f"from-env/{POINTER}"]
    assert parameters_read(store) == []


def test_an_empty_environment_value_falls_through_to_ssm(job, store, cli):
    store.put_parameter(DESTINATION, f"s3://{store.bucket}/from-ssm")

    outcome = promote(job.with_env(ARTIFACTS_DESTINATION=""), cli)

    assert outcome.code == 0, outcome.output
    assert store.stored_keys() == [f"from-ssm/{POINTER}"]
    assert parameters_read(store) == [DESTINATION]


@pytest.mark.parametrize("variable, parameter", R2_VALUES)
@pytest.mark.parametrize("backend", ["r2"])
def test_an_empty_r2_variable_falls_through_to_ssm(job, store, cli, backend, variable, parameter):
    store.put_parameter(parameter, "value-from-ssm", secure=parameter == SECRET_ACCESS_KEY)

    outcome = promote(job.with_env(**{variable: ""}), cli)

    assert outcome.code == 0, outcome.output
    assert parameters_read(store) == [parameter]
    assert store.get(POINTER) == job.env["BUILDKITE_BUILD_ID"].encode()


def test_an_upload_allowed_to_be_empty_reads_no_store_configuration(
    job, store, hook, denying_ssm_endpoint
):
    # Every SSM call fails here, so reading the configuration would fail the hook.
    job = job.with_env(**NO_STORE_ENV, AWS_ENDPOINT_URL_SSM=denying_ssm_endpoint).with_plugin(
        {
            "upload": {
                "variant": "v",
                "git_ref": "refs/heads/master",
                "files": "missing/*",
                "allow_empty": True,
            }
        }
    )

    outcome = hook("post-command", job)

    assert outcome.code == 0, outcome.output
    assert store.requests == []


@pytest.mark.parametrize("backend", ["r2"])
def test_the_r2_endpoint_comes_from_the_environment_before_ssm(job, store, cli, backend):
    store.put_parameter(ENDPOINT_URL, "http://127.0.0.1:9/never-used")

    outcome = promote(job, cli)

    assert outcome.code == 0, outcome.output
    assert parameters_read(store) == []


def test_the_s3_endpoint_is_never_read_from_ssm(job, store, cli):
    store.put_parameter(ENDPOINT_URL, "http://127.0.0.1:9/never-used")
    # Without ARTIFACTS_ENDPOINT_URL boto3's own endpoint variable keeps the call local.
    job = job.with_env(ARTIFACTS_ENDPOINT_URL=None, AWS_ENDPOINT_URL_S3=store.endpoint)

    outcome = promote(job, cli)

    assert outcome.code == 0, outcome.output
    assert parameters_read(store) == []
    assert store.get(POINTER) == job.env["BUILDKITE_BUILD_ID"].encode()


@pytest.mark.parametrize("backend", ["r2"])
def test_the_backend_name_is_trimmed_and_lowercased(job, store, cli, backend):
    outcome = promote(job.with_env(ARTIFACTS_BACKEND=" R2 "), cli)

    assert outcome.code == 0, outcome.output
    (put,) = store.plugin_requests("s3")
    assert (put.access_key_id, put.region) == (job.env["ARTIFACTS_R2_ACCESS_KEY_ID"], "auto")


def test_an_unsupported_backend_fails(job, store, cli):
    outcome = promote(job.with_env(ARTIFACTS_BACKEND="gcs"), cli)

    assert outcome.code == 1
    for fact in ["gcs", "ARTIFACTS_BACKEND", BACKEND]:
        assert fact in outcome.error
    assert store.plugin_requests("s3") == []


def test_a_missing_destination_fails(job, store, cli):
    outcome = promote(job.with_env(**NO_STORE_ENV), cli)

    assert outcome.code == 1
    for fact in ["ARTIFACTS_DESTINATION", DESTINATION]:
        assert fact in outcome.error
    assert parameters_read(store) == [BACKEND, DESTINATION]


@pytest.mark.parametrize("variable, parameter", R2_VALUES)
@pytest.mark.parametrize("backend", ["r2"])
def test_r2_without_one_of_its_three_values_fails(job, store, cli, backend, variable, parameter):
    outcome = promote(job.with_env(**{variable: None}), cli)

    assert outcome.code == 1
    assert variable in outcome.error and parameter in outcome.error
    assert parameters_read(store)[-1] == parameter
    assert store.plugin_requests("s3") == []


@pytest.mark.parametrize("prefix", ["SET_ME", "REPLACE_", "TODO", "CHANGEME", "FIXME"])
@pytest.mark.parametrize("variable, parameter", R2_VALUES)
@pytest.mark.parametrize("backend", ["r2"])
def test_an_r2_value_that_is_still_a_placeholder_fails(
    job, store, cli, backend, variable, parameter, prefix
):
    value = f"{prefix}_IN_TFVARS"

    outcome = promote(job.with_env(**{variable: value}), cli)

    assert outcome.code == 1
    assert value in outcome.error and parameter in outcome.error
    assert store.plugin_requests("s3") == []


@pytest.mark.parametrize("backend", ["r2"])
def test_a_placeholder_read_from_ssm_fails_the_same_way(job, store, cli, backend):
    put_r2_parameters(store, job, **{ACCESS_KEY_ID: "CHANGEME"})

    outcome = promote(job.with_env(**NO_STORE_ENV), cli)

    assert outcome.code == 1
    assert "CHANGEME" in outcome.error and ACCESS_KEY_ID in outcome.error


def test_an_ssm_error_other_than_not_found_fails_with_its_code(
    job, store, cli, denying_ssm_endpoint
):
    job = job.with_env(ARTIFACTS_BACKEND=None, AWS_ENDPOINT_URL_SSM=denying_ssm_endpoint)

    outcome = promote(job, cli)

    assert outcome.code == 1
    for fact in [BACKEND, "AccessDeniedException", "denied by test"]:
        assert fact in outcome.error
    assert store.plugin_requests("s3") == []


def test_ssm_is_read_in_eu_west_1_unless_the_job_names_a_region(job, store, cli):
    store.put_parameter(DESTINATION, f"s3://{store.bucket}", region="eu-west-1")

    outcome = promote(job.with_env(ARTIFACTS_DESTINATION=None, AWS_DEFAULT_REGION=None), cli)

    assert outcome.code == 0, outcome.output
    assert {r.region for r in store.plugin_requests("ssm")} == {"eu-west-1"}


def test_each_download_project_loads_the_configuration_again(job, store, hook):
    store.put_parameter(DESTINATION, f"s3://{store.bucket}")
    for build in ("b-1", "b-2"):
        store.put_build("engine", "refs/heads/master", "v", build, {f"{build}.bin": b"x"})
    projects = [
        {
            "project_id": "engine",
            "build_id": build,
            "variant": "v",
            "git_ref": "refs/heads/master",
            "files": ["*.bin"],
        }
        for build in ("b-1", "b-2")
    ]

    outcome = hook(
        "pre-command",
        job.with_env(**NO_STORE_ENV).with_plugin({"download": {"projects": projects}}),
    )

    assert outcome.code == 0, outcome.output
    assert parameters_read(store) == [BACKEND, DESTINATION, DOMAIN] * 2
