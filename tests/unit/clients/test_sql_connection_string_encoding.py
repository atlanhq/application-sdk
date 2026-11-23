"""Connection-form values cannot add URL structure to the SQLAlchemy URL.

``get_sqlalchemy_connection_string`` fills ``DB_CONFIG.template`` with values
from the connection form. Before FND-5212 only the password was encoded, so a
``database`` or ``host`` value carrying ``?`` and ``&`` added driver
parameters and turned a pinned ``sslmode`` default into a two-value tuple.

Every assertion goes through ``sqlalchemy.engine.make_url``, the parser
``create_engine`` uses, so it checks what the driver would receive.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.engine import make_url

from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import BaseSQLClient
from application_sdk.clients.sql_errors import SqlCredentialsParseError

_DEFAULTS = {"sslmode": "require"}


def _client(template: str, required: list[str]) -> BaseSQLClient:
    client = BaseSQLClient()
    client.DB_CONFIG = DatabaseConfig(
        template=template, required=required, defaults=dict(_DEFAULTS)
    )
    return client


def _pg_client() -> BaseSQLClient:
    return _client(
        "postgresql+psycopg://{username}:{password}@{host}:{port}/{database}",
        ["username", "password", "host", "port", "database"],
    )


def _credentials(**overrides: Any) -> dict[str, Any]:
    creds: dict[str, Any] = {
        "username": "user",
        "password": "pass",
        "host": "db.example.com",
        "port": 5432,
        "database": "db",
        "authType": "basic",
    }
    creds.update(overrides)
    return creds


def _build(client: BaseSQLClient, **overrides: Any) -> str:
    client.credentials = _credentials(**overrides)
    return client.get_sqlalchemy_connection_string()


@pytest.mark.parametrize(
    "database",
    [
        "?sslmode=disable&passfile=/x",
        "db?sslmode=disable",
        "db&sslmode=disable",
        "db#frag",
        "a/b",
        "100% db",
        "база данных",
    ],
)
def test_database_value_cannot_add_driver_parameters(database: str) -> None:
    url = make_url(_build(_pg_client(), database=database))

    assert url.database == database
    assert dict(url.query) == _DEFAULTS


@pytest.mark.parametrize(
    "username",
    ["user@corp.example", "a:b", "a/b?sslmode=disable&x=1", "first last", "u%40x"],
)
def test_username_round_trips_and_adds_nothing(username: str) -> None:
    url = make_url(_build(_pg_client(), username=username))

    assert url.username == username
    assert url.host == "db.example.com"
    assert url.port == 5432
    assert dict(url.query) == _DEFAULTS


def test_query_position_field_round_trips() -> None:
    """A field the template puts in the query keeps its value and adds none."""
    client = _client(
        "awsathena+rest://{username}:{password}@{host}:443/"
        "?s3_staging_dir={s3_output_location}",
        ["username", "password", "host", "s3_output_location"],
    )
    location = "s3://bucket/path/?sslmode=disable&work_group=x"

    url = make_url(_build(client, s3_output_location=location))

    assert dict(url.query) == {"s3_staging_dir": location, **_DEFAULTS}


@pytest.mark.parametrize(
    ("host", "parsed"),
    [
        ("localhost", "localhost"),
        ("db.example.com", "db.example.com"),
        ("db.example.com.", "db.example.com."),
        ("db_host.internal", "db_host.internal"),
        ("10.0.0.1", "10.0.0.1"),
        ("[::1]", "::1"),
        ("[2001:db8::1]", "2001:db8::1"),
    ],
)
def test_valid_host_is_accepted(host: str, parsed: str) -> None:
    url = make_url(_build(_pg_client(), host=host))

    assert url.host == parsed
    assert url.port == 5432
    assert url.database == "db"
    assert dict(url.query) == _DEFAULTS


@pytest.mark.parametrize(
    "host",
    [
        "evil/x?sslmode=disable",
        "db.example.com?sslmode=disable",
        "db.example.com/other",
        "db.example.com:6543",
        "postgres://db.example.com",
        "user@db.example.com",
        "db.example.com#x",
        "db example.com",
        " db.example.com",
        "::1",
        "[::1%eth0]",
        "[not-an-ip]",
        "[::1]:5432",
        "a" * 254,
    ],
)
def test_host_with_url_structure_is_rejected(host: str) -> None:
    client = _pg_client()
    client.credentials = _credentials(host=host)

    with pytest.raises(SqlCredentialsParseError) as excinfo:
        client.get_sqlalchemy_connection_string()

    assert excinfo.value.field == "host"
    assert host.strip() not in str(excinfo.value)


@pytest.mark.parametrize(("port", "parsed"), [(5432, 5432), ("5432", 5432), (1, 1)])
def test_valid_port_is_accepted(port: object, parsed: int) -> None:
    assert make_url(_build(_pg_client(), port=port)).port == parsed


@pytest.mark.parametrize(
    "port",
    [
        "5432/x?sslmode=disable",
        "5432?sslmode=disable",
        "abc",
        "-1",
        " 5432",
        "٥٤٣٢",
        True,
        70000,
        5432.0,
    ],
)
def test_non_numeric_or_out_of_range_port_is_rejected(port: object) -> None:
    client = _pg_client()
    client.credentials = _credentials(port=port)

    with pytest.raises(SqlCredentialsParseError) as excinfo:
        client.get_sqlalchemy_connection_string()

    assert excinfo.value.field == "port"


def test_zero_port_is_rejected() -> None:
    """``0`` is falsy, so it falls through to ``extra``; set it there too."""
    client = _pg_client()
    client.credentials = _credentials(port=0, extra={"port": 0})

    with pytest.raises(SqlCredentialsParseError):
        client.get_sqlalchemy_connection_string()


def test_host_and_port_outside_the_template_are_not_validated() -> None:
    """A template that builds its target elsewhere keeps working unchanged."""
    client = _client(
        "oracle+oracledb://{username}:{password}@",
        ["host", "port", "username", "password"],
    )

    conn_str = _build(client, host="(DESCRIPTION=(ADDRESS=x))", port="n/a")

    assert conn_str == "oracle+oracledb://user:pass@?sslmode=require"
