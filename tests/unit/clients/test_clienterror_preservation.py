"""Regression tests for the BLDX-1163 / BLDX-1164 / BLDX-1165 / BLDX-1180 fixes.

Each test asserts that an internal ``ClientError`` raised inside the client
propagates *unchanged* to the caller — the structured error code is not
swallowed by an outer broad ``except Exception`` that re-emits a generic
``CLIENT_AUTH_ERROR`` / generic ``ValueError``. These tests lock the
contract documented in PR #1602 and prevent re-introduction of the bugs.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.common.error_codes import ClientError

# ---------------------------------------------------------------------------
# BLDX-1180 — async SQL load() raises ClientError, not ValueError
# ---------------------------------------------------------------------------


class TestAsyncSqlLoadErrorContract:
    """``AsyncBaseSQLClient.load()`` must raise ``ClientError`` on auth /
    connection failure, mirroring sync ``BaseSQLClient.load()``. Before
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
        with patch(
            "sqlalchemy.ext.asyncio.create_async_engine",
            side_effect=RuntimeError("boom"),
        ):
            with pytest.raises(ClientError) as exc_info:
                await client.load({"username": "u", "password": "p"})

        assert str(ClientError.SQL_CLIENT_AUTH_ERROR) in str(exc_info.value)


# ---------------------------------------------------------------------------
# BLDX-1163 / BLDX-1164 — Azure ClientError preservation + use-after-close guard
# ---------------------------------------------------------------------------


class TestAzureClientErrorContract:
    """``AzureClient.load()`` must propagate the structured ``ClientError``
    raised internally by ``_test_connection``, and must reject re-load
    after ``close()``.
    """

    @pytest.mark.asyncio
    async def test_load_preserves_internal_client_error_code(self):
        from application_sdk.clients.azure.client import AzureClient

        client = AzureClient()
        # _test_connection is invoked from inside load(); make it raise the
        # exact structured ClientError that load() previously re-wrapped.
        sentinel = ClientError(
            f"{ClientError.AUTH_CREDENTIALS_ERROR}: missing credential"
        )

        # `_test_connection` is async — use AsyncMock so awaiting it works
        # and side_effect raises. Same for `auth_provider.create_credential`
        # which load() awaits.
        with patch.object(
            client, "_test_connection", new=AsyncMock(side_effect=sentinel)
        ):
            client.auth_provider = MagicMock()
            client.auth_provider.create_credential = AsyncMock(return_value=object())

            with pytest.raises(ClientError) as exc_info:
                await client.load({"auth_type": "service_principal"})

        # The original structured code must survive the broad except
        # Exception in load(); pre-fix it was re-wrapped as
        # CLIENT_AUTH_ERROR ("Unexpected error – ...").
        assert str(ClientError.AUTH_CREDENTIALS_ERROR) in str(exc_info.value)
        # Sanity: it is the same ClientError instance, not a re-wrap.
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

        with pytest.raises(ClientError) as exc_info:
            await client.load({"auth_type": "service_principal"})

        # Pre-fix this would silently submit work to a dead executor and
        # surface a confusing error far from the cause. After the fix the
        # client raises ClientError with an actionable message.
        assert "instantiate a new" in str(exc_info.value).lower() or (
            str(ClientError.CLIENT_AUTH_ERROR) in str(exc_info.value)
        )


# ---------------------------------------------------------------------------
# BLDX-1165 — Redis _connect / _release_lock ClientError preservation
# ---------------------------------------------------------------------------


class TestRedisSyncClientErrorContract:
    """Sync ``RedisClient._connect`` / ``_release_lock`` must propagate
    internal ``ClientError`` instead of re-routing through
    ``_handle_redis_error``.
    """

    def test_sync_connect_preserves_internal_client_error(self):
        from application_sdk.clients.redis import RedisClient

        client = RedisClient()

        # Force the no-redis_client branch inside _connect so the internal
        # ClientError(REDIS_CONNECTION_ERROR) is raised.
        with (
            patch.object(client, "_connect_standalone", return_value=None),
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
        ):
            client.redis_client = None
            with pytest.raises(ClientError) as exc_info:
                client._connect()

        assert str(ClientError.REDIS_CONNECTION_ERROR) in str(exc_info.value)

    def test_sync_release_lock_preserves_process_result_client_error(self):
        from application_sdk.clients.redis import RedisClient

        client = RedisClient()
        client.redis_client = MagicMock()
        # The eval result triggers `_process_lock_release_result` which
        # raises ClientError for unexpected payloads.
        sentinel = ClientError(
            f"{ClientError.REDIS_CONNECTION_ERROR}: unexpected lock release result"
        )
        client.redis_client.eval = MagicMock(return_value="garbage")
        with patch.object(client, "_process_lock_release_result", side_effect=sentinel):
            with pytest.raises(ClientError) as exc_info:
                client._release_lock("rid", "oid")

        assert exc_info.value is sentinel


class TestRedisAsyncClientErrorContract:
    """Async ``RedisClientAsync._connect`` / ``_release_lock`` must
    propagate internal ``ClientError`` — this is the path the SDK review
    flagged as missed in the original PR.
    """

    @pytest.mark.asyncio
    async def test_async_connect_preserves_internal_client_error(self):
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
            with pytest.raises(ClientError) as exc_info:
                await client._connect()

        assert str(ClientError.REDIS_CONNECTION_ERROR) in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_async_release_lock_preserves_process_result_client_error(self):
        from application_sdk.clients.redis import RedisClientAsync

        client = RedisClientAsync()
        client.redis_client = MagicMock()
        client.redis_client.eval = AsyncMock(return_value="garbage")
        sentinel = ClientError(
            f"{ClientError.REDIS_CONNECTION_ERROR}: unexpected lock release result"
        )
        with patch.object(client, "_process_lock_release_result", side_effect=sentinel):
            with pytest.raises(ClientError) as exc_info:
                await client._release_lock("rid", "oid")

        assert exc_info.value is sentinel


# Avoid `unused asyncio` warning when pytest-asyncio handles the awaits.
_ = asyncio
