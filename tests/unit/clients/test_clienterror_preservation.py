"""Regression tests for the BLDX-1163 / BLDX-1164 / BLDX-1165 / BLDX-1180 fixes.

Each test asserts that an internal error raised inside the client propagates
*unchanged* to the caller — the structured error code is not swallowed by an
outer broad ``except Exception`` that re-emits a generic error. These tests
lock the contract documented in PR #1602 and prevent re-introduction of the bugs.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.clients.azure.azure_errors import AzureClientAuthError
from application_sdk.clients.redis_errors import (
    RedisConnectionError,
    RedisProtocolError,
)
from application_sdk.clients.sql_errors import SqlClientAuthFailedError
from application_sdk.errors.leaves import AuthError

# ---------------------------------------------------------------------------
# BLDX-1180 — async SQL load() raises SqlClientAuthFailedError, not ValueError
# ---------------------------------------------------------------------------


class TestAsyncSqlLoadErrorContract:
    """``AsyncBaseSQLClient.load()`` must raise ``SqlClientAuthFailedError`` on
    auth / connection failure, mirroring sync ``BaseSQLClient.load()``. Before
    BLDX-1180 it raised generic ``ValueError(str(e))``.
    """

    @pytest.mark.asyncio
    async def test_async_load_failure_raises_client_error_not_value_error(self):
        from application_sdk.clients.models import DatabaseConfig
        from application_sdk.clients.sql import AsyncBaseSQLClient

        client = AsyncBaseSQLClient()
        # AsyncBaseSQLClient.load requires DB_CONFIG before it reaches the
        # engine-creation try block; configure the same way other tests do.
        client.DB_CONFIG = DatabaseConfig(
            template="test://{username}:{password}@{host}:{port}/{database}",
            required=["username", "password", "host", "port", "database"],
            connect_args={},
        )
        client.get_sqlalchemy_connection_string = lambda: "test://x"  # type: ignore[method-assign]

        # `create_async_engine` is imported inline inside `load()` (PLC0415
        # exception); patch at the source module so the inline import picks
        # up the mock.
        with (
            patch(
                "sqlalchemy.ext.asyncio.create_async_engine",
                side_effect=RuntimeError("boom"),
            ),
            pytest.raises(SqlClientAuthFailedError) as exc_info,
        ):
            await client.load({"username": "u", "password": "p"})

        assert exc_info.value.code == "AUTH_SQL_CLIENT_FAILED"


# ---------------------------------------------------------------------------
# HYP-1883 — ERROR_MAP client-boundary error typing
# ---------------------------------------------------------------------------


@dataclass(kw_only=True)
class _BadCredentialsError(AuthError):
    code: ClassVar[str] = "AUTH_SOURCE_BAD_CREDENTIALS"
    message: str = "The source rejected the credentials"


def _configured_async_client(cls):
    from application_sdk.clients.models import DatabaseConfig

    client = cls()
    client.DB_CONFIG = DatabaseConfig(
        template="test://{username}:{password}@{host}:{port}/{database}",
        required=["username", "password", "host", "port", "database"],
        connect_args={},
    )
    client.get_sqlalchemy_connection_string = lambda: "test://x"  # type: ignore[method-assign]
    return client


class TestErrorMapBoundaryTyping:
    """A SQL client subclass that declares ``ERROR_MAP`` gets its driver errors
    typed at the client boundary; the default (empty map) is unchanged."""

    @pytest.mark.asyncio
    async def test_declared_errno_raises_typed_leaf_chaining_driver_error(self):
        from application_sdk.clients.sql import AsyncBaseSQLClient

        class _TypedClient(AsyncBaseSQLClient):
            ERROR_MAP = {1045: _BadCredentialsError}

        client = _configured_async_client(_TypedClient)
        driver_error = Exception(1045, "Access denied for user")

        with (
            patch(
                "sqlalchemy.ext.asyncio.create_async_engine",
                side_effect=driver_error,
            ),
            pytest.raises(_BadCredentialsError) as exc_info,
        ):
            await client.load({"username": "u", "password": "p"})

        # The typed leaf replaces the generic wrapper, and the raw driver error
        # is preserved on the cause chain (`raise typed from exc`).
        assert exc_info.value.code == "AUTH_SOURCE_BAD_CREDENTIALS"
        assert exc_info.value.__cause__ is driver_error

    @pytest.mark.asyncio
    async def test_unmapped_errno_falls_through_to_generic_wrapper(self):
        from application_sdk.clients.sql import AsyncBaseSQLClient

        class _TypedClient(AsyncBaseSQLClient):
            ERROR_MAP = {1045: _BadCredentialsError}

        client = _configured_async_client(_TypedClient)

        with (
            patch(
                "sqlalchemy.ext.asyncio.create_async_engine",
                side_effect=Exception(9999, "some unmapped driver failure"),
            ),
            pytest.raises(SqlClientAuthFailedError) as exc_info,
        ):
            await client.load({"username": "u", "password": "p"})

        assert exc_info.value.code == "AUTH_SQL_CLIENT_FAILED"

    @pytest.mark.asyncio
    async def test_default_empty_map_is_byte_identical(self):
        # The stock client declares no ERROR_MAP: even an errno that a subclass
        # *could* map falls through to the generic wrapper — no classification.
        from application_sdk.clients.sql import AsyncBaseSQLClient

        client = _configured_async_client(AsyncBaseSQLClient)

        with (
            patch(
                "sqlalchemy.ext.asyncio.create_async_engine",
                side_effect=Exception(1045, "Access denied for user"),
            ),
            pytest.raises(SqlClientAuthFailedError) as exc_info,
        ):
            await client.load({"username": "u", "password": "p"})

        assert exc_info.value.code == "AUTH_SQL_CLIENT_FAILED"


# ---------------------------------------------------------------------------
# BLDX-1163 / BLDX-1164 — Azure typed-error preservation + use-after-close guard
# ---------------------------------------------------------------------------


class TestAzureClientErrorContract:
    """``AzureClient.load()`` must propagate the structured ``AzureClientAuthError``
    raised internally by ``_test_connection``, and must reject re-load
    after ``close()``.
    """

    @pytest.mark.asyncio
    async def test_load_preserves_internal_client_error_code(self):
        from application_sdk.clients.azure.client import AzureClient

        client = AzureClient()
        # _test_connection raises AzureClientAuthError internally; the
        # `except AppError: raise` guard in load() must pass it through unchanged.
        sentinel = AzureClientAuthError(message="missing credential")

        with patch.object(
            client, "_test_connection", new=AsyncMock(side_effect=sentinel)
        ):
            client.auth_provider = MagicMock()
            client.auth_provider.create_credential = AsyncMock(return_value=object())

            with pytest.raises(AzureClientAuthError) as exc_info:
                await client.load({"auth_type": "service_principal"})

        # The original structured code must survive the broad except
        # Exception in load(); it is re-raised via `except AppError: raise`.
        assert exc_info.value.code == "AUTH_AZURE_CLIENT"
        # Sanity: it is the same AzureClientAuthError instance, not a re-wrap.
        assert exc_info.value is sentinel

    @pytest.mark.asyncio
    async def test_load_after_close_raises_client_error_not_silent_dead_executor(
        self,
    ):
        from application_sdk.clients.azure.client import AzureClient

        client = AzureClient()

        # Simulate: client was closed (executor shut down + nulled).
        # Close should be idempotent and set _executor = None.
        if isinstance(client._executor, ThreadPoolExecutor):
            client._executor.shutdown(wait=False)
        client._executor = None

        with pytest.raises(AzureClientAuthError) as exc_info:
            await client.load({"auth_type": "service_principal"})

        # Pre-fix this would silently submit work to a dead executor and
        # surface a confusing error far from the cause. After the fix the
        # client raises AzureClientAuthError with an actionable message.
        assert "instantiate a new" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# BLDX-1165 — Redis _connect / _release_lock typed error preservation
# ---------------------------------------------------------------------------


class TestRedisSyncClientErrorContract:
    """Sync ``RedisClient._connect`` / ``_release_lock`` must propagate
    internal typed errors (``RedisConnectionError``, ``RedisProtocolError``)
    instead of re-routing through ``_handle_redis_error``.
    """

    def test_sync_connect_preserves_internal_connection_error(self):
        from application_sdk.clients.redis import RedisClient

        client = RedisClient()

        # Force the no-redis_client branch inside _connect so the internal
        # RedisConnectionError is raised.
        with (
            patch.object(client, "_connect_standalone", return_value=None),
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
        ):
            client.redis_client = None
            with pytest.raises(RedisConnectionError) as exc_info:
                client._connect()

        assert exc_info.value.code == "DEPENDENCY_UNAVAILABLE_REDIS"

    def test_sync_release_lock_preserves_process_result_error(self):
        from application_sdk.clients.redis import RedisClient

        client = RedisClient()
        client.redis_client = MagicMock()
        # The eval result triggers `_process_lock_release_result` which
        # raises RedisProtocolError for unexpected payloads. The
        # `except AppError: raise` guard must pass it through unchanged.
        sentinel = RedisProtocolError(message="unexpected lock release result")
        client.redis_client.eval = MagicMock(return_value="garbage")
        with patch.object(client, "_process_lock_release_result", side_effect=sentinel):
            with pytest.raises(RedisProtocolError) as exc_info:
                client._release_lock("rid", "oid")

        assert exc_info.value is sentinel


class TestRedisAsyncClientErrorContract:
    """Async ``RedisClientAsync._connect`` / ``_release_lock`` must
    propagate internal typed errors — this is the path the SDK review
    flagged as missed in the original PR.
    """

    @pytest.mark.asyncio
    async def test_async_connect_preserves_internal_connection_error(self):
        from application_sdk.clients.redis import RedisClientAsync

        client = RedisClientAsync()

        async def _noop():
            return None

        with (
            patch.object(client, "_connect_standalone", side_effect=_noop),
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
        ):
            client.redis_client = None
            with pytest.raises(RedisConnectionError) as exc_info:
                await client._connect()

        assert exc_info.value.code == "DEPENDENCY_UNAVAILABLE_REDIS"

    @pytest.mark.asyncio
    async def test_async_release_lock_preserves_process_result_error(self):
        from application_sdk.clients.redis import RedisClientAsync

        client = RedisClientAsync()
        client.redis_client = MagicMock()
        client.redis_client.eval = AsyncMock(return_value="garbage")
        sentinel = RedisProtocolError(message="unexpected lock release result")
        with patch.object(client, "_process_lock_release_result", side_effect=sentinel):
            with pytest.raises(RedisProtocolError) as exc_info:
                await client._release_lock("rid", "oid")

        assert exc_info.value is sentinel


# Avoid `unused asyncio` warning when pytest-asyncio handles the awaits.
_ = asyncio
