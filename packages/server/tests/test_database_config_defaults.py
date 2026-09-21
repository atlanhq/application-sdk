"""`DatabaseConfig.defaults` has to mean what application_sdk's means.

The migration path for a connector is to lift its existing DB_CONFIG into its
server package. Same class name, same field name, different semantics is the
worst possible shape for that: application_sdk appends `defaults` to the URL as
query parameters, while this package only ever used them to fill template
placeholders -- so a lifted DB_CONFIG lost `connect_timeout`,
`application_name` and, worst, `sslmode`, with no exception and no log line.

Both meanings are now honoured, split by whether the key names a placeholder.
"""

from __future__ import annotations

import pytest
from server_sdk.clients.models import DatabaseConfig
from server_sdk.clients.sql import BaseSQLClient

TEMPLATE = "redshift+psycopg2://{username}:{password}@{host}:{port}/{database}"
CREDS = {"username": "u", "password": "p", "host": "h", "database": "d"}


def _client(**config) -> BaseSQLClient:
    cls = type("C", (BaseSQLClient,), {"DB_CONFIG": DatabaseConfig(**config)})
    c = cls()
    c.credentials = dict(CREDS)
    return c


def test_a_default_naming_a_placeholder_still_fills_it() -> None:
    url = _client(
        template=TEMPLATE, defaults={"port": 5439}
    ).get_sqlalchemy_connection_string()
    assert url == "redshift+psycopg2://u:p@h:5439/d"


def test_a_default_naming_no_placeholder_reaches_the_url() -> None:
    """The regression this file exists for: silently dropped before."""
    url = _client(
        template=TEMPLATE,
        defaults={"port": 5439, "connect_timeout": 5, "application_name": "Atlan"},
    ).get_sqlalchemy_connection_string()
    assert "connect_timeout=5" in url
    assert "application_name=Atlan" in url


def test_sslmode_is_not_silently_lost() -> None:
    """A connector putting sslmode in defaults was losing TLS enforcement."""
    url = _client(
        template=TEMPLATE, defaults={"port": 5439, "sslmode": "require"}
    ).get_sqlalchemy_connection_string()
    assert "sslmode=require" in url


def test_credentials_still_beat_a_placeholder_default() -> None:
    c = _client(template=TEMPLATE, defaults={"port": 5439})
    c.credentials["port"] = 5440
    assert ":5440/" in c.get_sqlalchemy_connection_string()


def test_parameters_pull_named_keys_from_credentials() -> None:
    c = _client(template=TEMPLATE, defaults={"port": 5439}, parameters=["ssl_mode"])
    c.credentials["extra"] = {"ssl_mode": "require"}
    assert "ssl_mode=require" in c.get_sqlalchemy_connection_string()


def test_an_absent_parameter_is_simply_omitted() -> None:
    c = _client(template=TEMPLATE, defaults={"port": 5439}, parameters=["ssl_mode"])
    assert "ssl_mode" not in c.get_sqlalchemy_connection_string()


@pytest.mark.parametrize("hostile", ["x&sslmode=disable", "a=b", "with space"])
def test_a_param_value_cannot_inject_another_param(hostile: str) -> None:
    url = _client(
        template=TEMPLATE, defaults={"port": 5439, "application_name": hostile}
    ).get_sqlalchemy_connection_string()
    # Exactly one '?' and one '&'-free tail beyond the single param we added.
    assert url.count("?") == 1
    assert url.count("&") == 0


def test_the_ported_fields_exist_at_all() -> None:
    """`parameters` and `pool_pre_ping` are in every application_sdk DB_CONFIG;
    without them a verbatim lift raises TypeError."""
    cfg = DatabaseConfig(
        template=TEMPLATE, parameters=["ssl_mode"], pool_pre_ping=False
    )
    assert cfg.parameters == ["ssl_mode"]
    assert cfg.pool_pre_ping is False
    assert DatabaseConfig(template=TEMPLATE).pool_pre_ping is True
