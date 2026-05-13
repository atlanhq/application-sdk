"""Regression tests for the BLDX-1163 / BLDX-1164 / BLDX-1165 / BLDX-1180 fixes.

Each test asserts that an internal ``AppError`` raised inside the client
propagates *unchanged* to the caller — the structured error code is not
swallowed by an outer broad ``except Exception`` that re-emits a generic
auth error. These tests lock the contract documented in PR #1602 and
prevent re-introduction of the bugs.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.clients._redis_errors import RedisConnectionError
from application_sdk.clients.azure._azure_errors import (
    AzureAuthError,
    AzureNoCredentialError,
)
from application_sdk.errors import AppError

# ---------------------------------------------------------------------------
# BLDX-1180 — async SQL load() raises AppError, not ValueError
# ---------------------------------------------------------------------------


class TestAsyncSqlLoadErrorContract:
    """``AsyncBaseSQLClient.load()`` must raise ``SqlClientAuthFailedError`` on
    auth / connection failure, mirroring sync ``BaseSQLClient.load()``. Before
    BLDX-1180 it raised generic ``ValueError(str(e))``.
    """

    @pytest.mark.asyncio
    async def test_async_load_failure_raises_client_error_not_value_error(self):
        from application_sdk.clients._sql_errors import SqlClientAuthFailedError
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

        assert "SQL client authentication failed" in str(exc_info.value)


# ---------------------------------------------------------------------------
# BLDX-1163 / BLDX-1164 — Azure AppError preservation + use-after-close guard
# ---------------------------------------------------------------------------


class TestAzureClientErrorContract:
    """``AzureClient.load()`` must propagate the structured ``AppError``
    raised internally by ``_test_connection``, and must reject re-load
    after ``close()``.
    """

    @pytest.mark.asyncio
    async def test_load_preserves_internal_app_error(self):
        from application_sdk.clients.azure.client import AzureClient

        client = AzureClient()
        # _test_connection is invoked from inside load(); make it raise a
        # typed AppError subclass. load() has `except AppError: raise` so
        # the sentinel must propagate unchanged.
        sentinel = AzureAuthError(
            message="missing credential",
            auth_method="azure_service_principal",
        )

        # `_test_connection` is async — use AsyncMock so awaiting it works
        # and side_effect raises. Same for `auth_provider.create_credential`
        # which load() awaits.
        with patch.object(
            client, "_test_connection", new=AsyncMock(side_effect=sentinel)
        ):
            client.auth_provider = MagicMock()
            client.auth_provider.create_credential = AsyncMock(return_value=object())

            with pytest.raises(AzureAuthError) as exc_info:
                await client.load({"auth_type": "service_principal"})

        # The original structured error must survive the broad except
        # Exception in load(); pre-fix it was re-wrapped as
        # CLIENT_AUTH_ERROR ("Unexpected error – ...").
        assert "missing credential" in str(exc_info.value)
        # Sanity: it is the same AppError instance, not a re-wrap.
        assert exc_info.value is sentinel

    @pytest.mark.asyncio
    async def test_load_after_close_raises_app_error_not_silent_dead_executor(
        self,
    ):
        from application_sdk.clients.azure.client import AzureClient

        client = AzureClient()

        # Simulate: client was closed (executor shut down + nulled).
        # Close should be idempotent and set _executor = None.
        if isinstance(client._executor, ThreadPoolExecutor):
            client._executor.shutdown(wait=False)
        client._executor = None

        with pytest.raises(AzureNoCredentialError) as exc_info:
            await client.load({"auth_type": "service_principal"})

        # Pre-fix this would silently submit work to a dead executor and
        # surface a confusing error far from the cause. After the fix the
        # client raises AzureNoCredentialError with an actionable message.
        assert "instantiate a new" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# BLDX-1165 — Redis _connect / _release_lock AppError preservation
# ---------------------------------------------------------------------------


class TestRedisSyncClientErrorContract:
    """Sync ``RedisClient._connect`` / ``_release_lock`` must propagate
    internal ``AppError`` instead of re-routing through
    ``_handle_redis_error``.
    """

    def test_sync_connect_preserves_internal_app_error(self):
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

        assert "Redis connection failed" in str(exc_info.value)

    def test_sync_release_lock_preserves_process_result_app_error(self):
        from application_sdk.clients.redis import RedisClient

        client = RedisClient()
        client.redis_client = MagicMock()
        # The eval result triggers `_process_lock_release_result` which
        # raises AppError subclasses for unexpected payloads.
        sentinel = RedisConnectionError(
            message="Redis connection failed: unexpected lock release result",
            service="redis",
        )
        client.redis_client.eval = MagicMock(return_value="garbage")
        with patch.object(client, "_process_lock_release_result", side_effect=sentinel):
            with pytest.raises(RedisConnectionError) as exc_info:
                client._release_lock("rid", "oid")

        assert exc_info.value is sentinel


class TestRedisAsyncClientErrorContract:
    """Async ``RedisClientAsync._connect`` / ``_release_lock`` must
    propagate internal ``AppError`` — this is the path the SDK review
    flagged as missed in the original PR.
    """

    @pytest.mark.asyncio
    async def test_async_connect_preserves_internal_app_error(self):
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

        assert "Redis connection failed" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_async_release_lock_preserves_process_result_app_error(self):
        from application_sdk.clients.redis import RedisClientAsync

        client = RedisClientAsync()
        client.redis_client = MagicMock()
        client.redis_client.eval = AsyncMock(return_value="garbage")
        sentinel = RedisConnectionError(
            message="Redis connection failed: unexpected lock release result",
            service="redis",
        )
        with patch.object(client, "_process_lock_release_result", side_effect=sentinel):
            with pytest.raises(RedisConnectionError) as exc_info:
                await client._release_lock("rid", "oid")

        assert exc_info.value is sentinel


# Avoid `unused asyncio` warning when pytest-asyncio handles the awaits.
_ = asyncio
# Avoid unused import warning — AppError is imported for type documentation.
_ = AppError
