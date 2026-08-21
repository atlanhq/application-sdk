"""Credential resolution for BaseSQLClient.

The setup form nests connector-specific fields (``database``, ``warehouse``,
``role``…) under ``extra``, and heracles forwards that shape verbatim on
``/workflows/v1/auth``. A client that reads only the top level rejects a valid
credential with "Missing required credential field(s): database" while
``extra["database"]`` holds the value — which is exactly what reached a live
tenant, so these tests pin the resolution order rather than the happy path.
"""

from __future__ import annotations

import json

import pytest
from server_sdk.clients.models import DatabaseConfig
from server_sdk.clients.sql import BaseSQLClient
from server_sdk.errors.leaves import InvalidInputError


class _Redshift(BaseSQLClient):
    """Mirrors the real redshift server client's config."""

    DB_CONFIG = DatabaseConfig(
        template="redshift+psycopg2://{username}:{password}@{host}:{port}/{database}",
        required=["host", "port", "database", "username", "password"],
        defaults={"port": 5439},
    )


def _client(credentials: dict) -> _Redshift:
    client = _Redshift()
    client.credentials = credentials
    return client


BASE = {
    "host": "h.redshift.amazonaws.com",
    "authType": "basic",
    "username": "admin",
    "password": "pw",
}


def test_required_field_resolves_from_extra():
    """The exact payload the connector setup form submits."""
    url = _client(
        {**BASE, "extra": {"database": "dev", "deployment_type": "provisioned"}}
    ).get_sqlalchemy_connection_string()
    assert url.endswith("/dev")


def test_top_level_still_works():
    url = _client({**BASE, "database": "dev"}).get_sqlalchemy_connection_string()
    assert url.endswith("/dev")


def test_top_level_wins_over_extra():
    url = _client(
        {**BASE, "database": "top", "extra": {"database": "nested"}}
    ).get_sqlalchemy_connection_string()
    assert url.endswith("/top")


def test_empty_top_level_falls_through_to_extra():
    """A blank top-level field must not mask a real value in extra."""
    url = _client(
        {**BASE, "database": "", "extra": {"database": "dev"}}
    ).get_sqlalchemy_connection_string()
    assert url.endswith("/dev")


def test_extra_as_json_string_is_parsed():
    """Some callers send extra as a JSON-encoded string."""
    url = _client(
        {**BASE, "extra": json.dumps({"database": "dev"})}
    ).get_sqlalchemy_connection_string()
    assert url.endswith("/dev")


def test_genuinely_missing_field_still_raises():
    with pytest.raises(InvalidInputError) as exc:
        _client(
            {**BASE, "extra": {"deployment_type": "provisioned"}}
        ).get_sqlalchemy_connection_string()
    assert "database" in str(exc.value)


def test_defaults_still_apply():
    url = _client(
        {**BASE, "extra": {"database": "dev"}}
    ).get_sqlalchemy_connection_string()
    assert ":5439/" in url


def test_password_is_url_encoded_from_either_level():
    """Special characters survive; the userinfo encoding is unchanged."""
    url = _client(
        {**BASE, "password": "p@ss w/ord", "extra": {"database": "dev"}}
    ).get_sqlalchemy_connection_string()
    assert "p%40ss%20w%2Ford" in url
