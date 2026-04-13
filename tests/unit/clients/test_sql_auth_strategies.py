"""Tests for BaseSQLClient and AsyncBaseSQLClient auth strategy integration."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.clients.auth_strategies.basic import BasicAuthStrategy
from application_sdk.clients.auth_strategies.oauth import OAuthAuthStrategy
from application_sdk.clients.models import DatabaseConfig
from application_sdk.clients.sql import AsyncBaseSQLClient, BaseSQLClient
from application_sdk.common.error_codes import ClientError
from application_sdk.credentials.types import (
    BasicCredential,
    CertificateCredential,
    OAuthClientCredential,
)

# ---------------------------------------------------------------------------
# Concrete subclass fixtures
# ---------------------------------------------------------------------------


class StubSQLClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="postgresql://{username}:{password}@localhost:5432/mydb",
        required=["username", "password"],
    )
    AUTH_STRATEGIES = {
        BasicCredential: BasicAuthStrategy(),
    }


class StubMultiAuthClient(BaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="snowflake://{username}@account/db",
        required=["username"],
    )
    AUTH_STRATEGIES = {
        BasicCredential: BasicAuthStrategy(),
        OAuthClientCredential: OAuthAuthStrategy(
            url_query_params={"authenticator": "oauth"},
        ),
    }


class StubAsyncSQLClient(AsyncBaseSQLClient):
    DB_CONFIG = DatabaseConfig(
        template="postgresql+asyncpg://{username}:{password}@localhost:5432/mydb",
        required=["username", "password"],
    )
    AUTH_STRATEGIES = {
        BasicCredential: BasicAuthStrategy(),
    }


class LegacySQLClient(BaseSQLClient):
    """Client with no AUTH_STRATEGIES — uses legacy get_auth_token path."""

    DB_CONFIG = DatabaseConfig(
        template="postgresql://{username}:{password}@localhost:5432/mydb",
        required=["username", "password"],
    )


# ---------------------------------------------------------------------------
# Helper to build a mock sync engine
# ---------------------------------------------------------------------------


def _make_mock_sync_engine():
    mock_engine = MagicMock()
    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_engine.connect.return_value = mock_conn
    return mock_engine


# ---------------------------------------------------------------------------
# BaseSQLClient.load_with_credential tests
# ---------------------------------------------------------------------------


class TestLoadWithCredential:
    @pytest.mark.asyncio
    @patch("sqlalchemy.create_engine")
    async def test_basic_credential_creates_engine(self, mock_create_engine):
        mock_engine = _make_mock_sync_engine()
        mock_create_engine.return_value = mock_engine

        client = StubSQLClient()
        cred = BasicCredential(username="user", password="s3cret")
        await client.load_with_credential(cred, username="user")

        mock_create_engine.assert_called_once()
        conn_str = mock_create_engine.call_args[0][0]
        # BasicAuthStrategy URL-encodes the password
        assert "s3cret" in conn_str
        assert "user" in conn_str
        assert client.engine is mock_engine

    @pytest.mark.asyncio
    async def test_no_strategy_for_credential_raises(self):
        client = StubSQLClient()
        cred = CertificateCredential(key_data="pem-data")

        with pytest.raises(ClientError, match="No auth strategy registered"):
            await client.load_with_credential(cred)

    @pytest.mark.asyncio
    async def test_no_db_config_raises(self):
        client = BaseSQLClient()
        cred = BasicCredential(username="u", password="p")

        with pytest.raises(ValueError, match="DB_CONFIG is not configured"):
            await client.load_with_credential(cred)

    @pytest.mark.asyncio
    @patch("sqlalchemy.create_engine")
    async def test_connect_args_merged_from_strategy(self, mock_create_engine):
        """Strategy connect_args are merged with DB_CONFIG.connect_args."""
        mock_engine = _make_mock_sync_engine()
        mock_create_engine.return_value = mock_engine

        class ClientWithConnectArgs(BaseSQLClient):
            DB_CONFIG = DatabaseConfig(
                template="snowflake://{username}@account/db",
                required=["username"],
                connect_args={"timeout": 30},
            )
            AUTH_STRATEGIES = {
                BasicCredential: BasicAuthStrategy(),
            }

        client = ClientWithConnectArgs()
        cred = BasicCredential(username="user", password="pass")
        await client.load_with_credential(cred, username="user")

        call_kwargs = mock_create_engine.call_args[1]
        assert call_kwargs["connect_args"]["timeout"] == 30

    @pytest.mark.asyncio
    @patch("sqlalchemy.create_engine")
    async def test_query_params_appended(self, mock_create_engine):
        """Strategy url_query_params are appended to connection string."""
        mock_engine = _make_mock_sync_engine()
        mock_create_engine.return_value = mock_engine

        client = StubMultiAuthClient()
        cred = OAuthClientCredential(
            client_id="cid",
            client_secret="csec",
            token_url="https://auth.example.com/token",
            access_token="tok123",
        )
        await client.load_with_credential(cred, username="user")

        conn_str = mock_create_engine.call_args[0][0]
        assert "authenticator=oauth" in conn_str

    @pytest.mark.asyncio
    @patch("sqlalchemy.create_engine")
    async def test_engine_disposed_on_connection_failure(self, mock_create_engine):
        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("connection refused")
        mock_create_engine.return_value = mock_engine

        client = StubSQLClient()
        cred = BasicCredential(username="user", password="pass")

        with pytest.raises(ClientError, match="connection refused"):
            await client.load_with_credential(cred, username="user")

        mock_engine.dispose.assert_called_once()
        assert client.engine is None


# ---------------------------------------------------------------------------
# Legacy path still works
# ---------------------------------------------------------------------------


class TestLegacyLoadUnchanged:
    @pytest.mark.asyncio
    @patch("sqlalchemy.create_engine")
    async def test_legacy_load_still_works(self, mock_create_engine):
        mock_engine = _make_mock_sync_engine()
        mock_create_engine.return_value = mock_engine

        client = LegacySQLClient()
        await client.load({"username": "user", "password": "secret"})

        mock_create_engine.assert_called_once()
        conn_str = mock_create_engine.call_args[0][0]
        assert "user" in conn_str
        assert "secret" in conn_str
        assert client.engine is mock_engine


# ---------------------------------------------------------------------------
# AUTH_STRATEGIES empty by default
# ---------------------------------------------------------------------------


class TestDefaultAuthStrategiesEmpty:
    def test_base_sql_client_has_empty_strategies(self):
        assert BaseSQLClient.AUTH_STRATEGIES == {}

    def test_async_base_sql_client_inherits_empty_strategies(self):
        assert AsyncBaseSQLClient.AUTH_STRATEGIES == {}


# ---------------------------------------------------------------------------
# AsyncBaseSQLClient.load_with_credential tests
# ---------------------------------------------------------------------------


class TestAsyncLoadWithCredential:
    @pytest.mark.asyncio
    @patch("sqlalchemy.ext.asyncio.create_async_engine")
    async def test_basic_credential_creates_async_engine(
        self, mock_create_async_engine
    ):
        mock_conn = AsyncMock()
        mock_cm = AsyncMock()
        mock_cm.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_cm.__aexit__ = AsyncMock(return_value=False)

        mock_engine = MagicMock()
        mock_engine.connect.return_value = mock_cm
        mock_engine.dispose = AsyncMock()
        mock_create_async_engine.return_value = mock_engine

        client = StubAsyncSQLClient()
        cred = BasicCredential(username="user", password="s3cret")
        await client.load_with_credential(cred, username="user")

        mock_create_async_engine.assert_called_once()
        assert client.engine is mock_engine

    @pytest.mark.asyncio
    async def test_no_strategy_for_credential_raises(self):
        client = StubAsyncSQLClient()
        cred = CertificateCredential(key_data="pem-data")

        with pytest.raises(ClientError, match="No auth strategy registered"):
            await client.load_with_credential(cred)
