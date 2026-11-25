"""Which R2 key pair a caller gets: a caller that only reads gets the read-only pair, a caller
that writes gets the read-write pair, and a store that holds no read-only pair yet still serves
a reader with the read-write pair, which the job log says. Over every source of the pairs:
variables and SSM parameters.
"""

from __future__ import annotations

import pytest
from botocore.exceptions import ClientError

from lib import config
from lib.config import READ, WRITE, load_store_config
from lib.errors import ArtifactsError

ACCOUNT = "0123456789abcdef0123456789abcdef"
READ_WRITE = ("rw-key-id", "rw-secret")
READ_ONLY = ("ro-key-id", "ro-secret")

READ_WRITE_NAMES = (
    ("ARTIFACTS_R2_ACCESS_KEY_ID", f"{config.SSM}/r2/access-key-id"),
    ("ARTIFACTS_R2_SECRET_ACCESS_KEY", f"{config.CREDENTIALS}/r2/secret-access-key"),
)
READ_ONLY_NAMES = (
    ("ARTIFACTS_R2_READ_ONLY_ACCESS_KEY_ID", f"{config.SSM}/r2/read-only-access-key-id"),
    (
        "ARTIFACTS_R2_READ_ONLY_SECRET_ACCESS_KEY",
        f"{config.CREDENTIALS}/r2/read-only-secret-access-key",
    ),
)
SOURCES = ("variables", "ssm")


class FakeSsm:
    def __init__(self, parameters: dict[str, str]):
        self.parameters = parameters

    def get_parameter(self, Name, WithDecryption):
        if Name not in self.parameters:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": Name}}, "Get")
        return {"Parameter": {"Value": self.parameters[Name]}}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name, _ in READ_WRITE_NAMES + READ_ONLY_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ARTIFACTS_BACKEND", "r2")
    monkeypatch.setenv("ARTIFACTS_DESTINATION", "s3://example-artifacts")
    monkeypatch.setenv("ARTIFACTS_R2_ACCOUNT_ID", ACCOUNT)


def provided(monkeypatch, source: str, *held) -> FakeSsm:
    """Hand each (names, values) of ``held`` over through ``source``: as variables, or as the
    parameters of the SSM that is returned."""
    parameters = {}
    for names, values in held:
        for (variable, parameter), value in zip(names, values):
            if source == "variables":
                monkeypatch.setenv(variable, value)
            else:
                parameters[parameter] = value
    return FakeSsm(parameters)


def pair_of(cfg) -> tuple[str, str]:
    return (cfg.r2_access_key_id, cfg.r2_secret_access_key)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("access, expected", [(READ, READ_ONLY), (WRITE, READ_WRITE)])
def test_a_caller_gets_the_key_pair_of_what_it_does(monkeypatch, source, access, expected):
    ssm = provided(
        monkeypatch, source, (READ_WRITE_NAMES, READ_WRITE), (READ_ONLY_NAMES, READ_ONLY)
    )

    assert pair_of(load_store_config(ssm, access)) == expected


def test_a_caller_that_names_no_access_only_reads(monkeypatch):
    ssm = provided(monkeypatch, "ssm", (READ_WRITE_NAMES, READ_WRITE), (READ_ONLY_NAMES, READ_ONLY))

    assert pair_of(load_store_config(ssm)) == READ_ONLY


@pytest.mark.parametrize("source", SOURCES)
def test_a_reader_falls_back_to_the_read_write_pair_and_says_so(monkeypatch, capsys, source):
    ssm = provided(monkeypatch, source, (READ_WRITE_NAMES, READ_WRITE))

    cfg = load_store_config(ssm, READ)

    assert pair_of(cfg) == READ_WRITE
    assert "read-only" in capsys.readouterr().err


def test_a_writer_needs_no_read_only_pair_and_says_nothing(monkeypatch, capsys):
    ssm = provided(monkeypatch, "ssm", (READ_WRITE_NAMES, READ_WRITE))

    load_store_config(ssm, WRITE)

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("missing", [0, 1])
def test_half_a_read_only_pair_is_an_error(monkeypatch, missing):
    names = tuple(n for i, n in enumerate(READ_ONLY_NAMES) if i != missing)
    values = tuple(v for i, v in enumerate(READ_ONLY) if i != missing)
    ssm = provided(monkeypatch, "ssm", (READ_WRITE_NAMES, READ_WRITE), (names, values))

    with pytest.raises(ArtifactsError):
        load_store_config(ssm, READ)


def test_a_placeholder_read_only_pair_is_an_error(monkeypatch):
    placeholder = ("SET_ME", "SET_ME")
    ssm = provided(
        monkeypatch, "ssm", (READ_WRITE_NAMES, READ_WRITE), (READ_ONLY_NAMES, placeholder)
    )

    with pytest.raises(ArtifactsError):
        load_store_config(ssm, READ)


@pytest.mark.parametrize("access", [READ, WRITE])
def test_s3_has_no_key_pair_for_either_access(monkeypatch, access):
    monkeypatch.setenv("ARTIFACTS_BACKEND", "s3")

    cfg = load_store_config(FakeSsm({}), access)

    assert pair_of(cfg) == (None, None)
