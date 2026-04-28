"""BLDX-1129 follow-up unit tests for ``application_sdk.clients.redis``.

Goal: surface bug classes that escape current tests — runtime-import errors,
contract drift, swallowed exceptions, connection-leak paths — and exercise
public contracts that are still uncovered.

The existing ``test_redis_client.py`` covers the happy/sad paths for
``_acquire_lock`` / ``_release_lock`` and ``_handle_redis_error``. This file
focuses on:

* ``_parse_sentinel_hosts`` (valid multi-host / malformed / empty input)
* ``_process_lock_release_result`` (``None``, non-int, fall-through)
* ``_connect`` / ``_connect_via_sentinel`` / ``_connect_standalone`` for both
  sync and async clients (mocking the ``redis``/``redis.asyncio`` SDK).
* ``close`` behaviour: idempotency, exception swallowing.
* Lock operations against an unconnected client (early ``ClientError``).
* Symbol contracts at module load (BLDX-1129 anchor).

All tests mock ``redis``/``redis.asyncio`` — no real I/O.
"""

from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from redis.exceptions import TimeoutError as RedisTimeoutError

from application_sdk.clients.redis import (
    BaseRedisClient,
    LockReleaseResult,
    RedisClient,
    RedisClientAsync,
    _handle_redis_error,
)
from application_sdk.common.error_codes import ClientError

# ---------------------------------------------------------------------------
# Module-level symbol contract (BLDX-1129 anchor)
# ---------------------------------------------------------------------------


class TestModuleSymbolContract:
    """Module-load assertions that catch import-time contract drift."""

    def test_public_symbols_importable(self):
        """All public names used elsewhere must be importable.

        BLDX-1129 root cause was a missing/renamed symbol surfacing only at
        runtime. This test fails at collection time if any public symbol
        disappears or is renamed.
        """
        from application_sdk.clients import redis as redis_mod

        for name in (
            "BaseRedisClient",
            "RedisClient",
            "RedisClientAsync",
            "LockReleaseResult",
            "_handle_redis_error",
            "_LOCK_RELEASE_LUA_SCRIPT",
        ):
            assert hasattr(redis_mod, name), f"missing: {name}"

    def test_lua_script_has_expected_branches(self):
        """The Lua script must encode all three release outcomes."""
        from application_sdk.clients.redis import _LOCK_RELEASE_LUA_SCRIPT

        # The Python ``_process_lock_release_result`` depends on these magic
        # numbers; if the Lua changes, both must change in lock-step.
        assert "GET" in _LOCK_RELEASE_LUA_SCRIPT
        assert "DEL" in _LOCK_RELEASE_LUA_SCRIPT
        assert "-1" in _LOCK_RELEASE_LUA_SCRIPT  # already released
        assert "-2" in _LOCK_RELEASE_LUA_SCRIPT  # wrong owner


# ---------------------------------------------------------------------------
# _parse_sentinel_hosts
# ---------------------------------------------------------------------------


def _make_base_client(sentinel_hosts: str = "host1:26379,host2:26379"):
    """Build a BaseRedisClient with sentinel config — bypasses validation."""
    with (
        patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
        patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
        patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", sentinel_hosts),
    ):
        client = BaseRedisClient()
    return client


class TestParseSentinelHosts:
    """``_parse_sentinel_hosts`` covers a critical config path."""

    def test_parses_multi_host_csv(self):
        client = _make_base_client("a.example:26379,b.example:26380")
        with patch(
            "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS",
            "a.example:26379,b.example:26380",
        ):
            hosts = client._parse_sentinel_hosts()
        assert hosts == [("a.example", 26379), ("b.example", 26380)]

    def test_strips_whitespace(self):
        client = _make_base_client(" a.example:26379 , b.example:26380 ")
        with patch(
            "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS",
            " a.example:26379 , b.example:26380 ",
        ):
            hosts = client._parse_sentinel_hosts()
        assert hosts == [("a.example", 26379), ("b.example", 26380)]

    def test_invalid_port_raises(self):
        """Non-integer port must surface as a wrapped error, not silently pass."""
        client = _make_base_client("a.example:not-a-port")
        with patch(
            "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS",
            "a.example:not-a-port",
        ):
            with pytest.raises(Exception):  # rewrap-wrapped ValueError
                client._parse_sentinel_hosts()

    def test_ipv6_host_with_port(self):
        """rsplit(':', 1) supports ``host:port`` even with colons in host."""
        # 'foo:bar:26379' should split into ('foo:bar', 26379)
        client = _make_base_client("foo:bar:26379")
        with patch(
            "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", "foo:bar:26379"
        ):
            hosts = client._parse_sentinel_hosts()
        assert hosts == [("foo:bar", 26379)]


# ---------------------------------------------------------------------------
# _process_lock_release_result — defensive branches
# ---------------------------------------------------------------------------


class TestProcessLockReleaseResult:
    """Edge cases in the Lua-result interpreter."""

    @pytest.fixture
    def base(self):
        return _make_base_client()

    def test_none_result_raises_client_error(self, base):
        """``None`` is not an int — must raise (no silent success)."""
        with pytest.raises(ClientError) as ei:
            base._process_lock_release_result(None, "resource-id")
        assert "ATLAN-CLIENT-503-00" in str(ei.value)

    def test_string_result_raises_client_error(self, base):
        """Unexpected string return must raise."""
        with pytest.raises(ClientError):
            base._process_lock_release_result("OK", "resource-id")

    def test_zero_result_falls_through_to_unknown(self, base):
        """``0`` matches no defined branch — must raise, not silently lie."""
        with pytest.raises(ClientError) as ei:
            base._process_lock_release_result(0, "resource-id")
        assert "ATLAN-CLIENT-503-00" in str(ei.value)

    def test_large_positive_is_success(self, base):
        ok, outcome = base._process_lock_release_result(2, "rid")
        assert ok is True
        assert outcome == LockReleaseResult.SUCCESS


# ---------------------------------------------------------------------------
# Sync RedisClient connect / close
# ---------------------------------------------------------------------------


class TestSyncConnect:
    """``RedisClient._connect`` and helpers."""

    @pytest.fixture
    def standalone_client(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
        ):
            yield RedisClient()

    @pytest.fixture
    def sentinel_client(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch(
                "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS",
                "a:26379,b:26379",
            ),
            patch(
                "application_sdk.clients.redis.REDIS_SENTINEL_SERVICE_NAME",
                "mymaster",
            ),
        ):
            yield RedisClient()

    def test_connect_noop_when_locking_disabled(self, standalone_client):
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", True):
            standalone_client._connect()
        assert standalone_client.redis_client is None

    def test_connect_standalone_happy_path(self, standalone_client):
        fake_redis = MagicMock()
        fake_redis.ping.return_value = True
        with (
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch(
                "application_sdk.clients.redis.redis.Redis", return_value=fake_redis
            ) as redis_ctor,
        ):
            standalone_client._connect()
        redis_ctor.assert_called_once_with(
            host="localhost", port=6379, password="secret"
        )
        fake_redis.ping.assert_called_once()
        assert standalone_client.redis_client is fake_redis

    def test_connect_via_sentinel_happy_path(self, sentinel_client):
        master = MagicMock()
        master.ping.return_value = True
        sentinel = MagicMock()
        sentinel.master_for.return_value = master
        with (
            patch(
                "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS",
                "a:26379,b:26379",
            ),
            patch(
                "application_sdk.clients.redis.REDIS_SENTINEL_SERVICE_NAME",
                "mymaster",
            ),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch(
                "application_sdk.clients.redis.redis.sentinel.Sentinel",
                return_value=sentinel,
            ) as sentinel_ctor,
        ):
            sentinel_client._connect()
        sentinel_ctor.assert_called_once()
        sentinel.master_for.assert_called_once_with("mymaster", password="secret")
        master.ping.assert_called_once()
        assert sentinel_client.redis_client is master

    def test_connect_ping_failure_wraps_to_client_error(self, standalone_client):
        fake_redis = MagicMock()
        fake_redis.ping.side_effect = RedisConnectionError("nope")
        with (
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
            patch("application_sdk.clients.redis.redis.Redis", return_value=fake_redis),
        ):
            with pytest.raises(ClientError) as ei:
                standalone_client._connect()
        assert "ATLAN-CLIENT-503-00" in str(ei.value)

    def test_connect_timeout_failure_maps_to_timeout_code(self, standalone_client):
        fake_redis = MagicMock()
        fake_redis.ping.side_effect = RedisTimeoutError("slow")
        with (
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
            patch("application_sdk.clients.redis.redis.Redis", return_value=fake_redis),
        ):
            with pytest.raises(ClientError) as ei:
                standalone_client._connect()
        assert "ATLAN-CLIENT-408-00" in str(ei.value)

    def test_connect_redis_protocol_error_maps_to_protocol_code(
        self, standalone_client
    ):
        fake_redis = MagicMock()
        fake_redis.ping.side_effect = RedisError("proto")
        with (
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
            patch("application_sdk.clients.redis.redis.Redis", return_value=fake_redis),
        ):
            with pytest.raises(ClientError) as ei:
                standalone_client._connect()
        assert "ATLAN-CLIENT-502-00" in str(ei.value)

    def test_connect_standalone_ctor_failure_wrapped(self, standalone_client):
        with (
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
            patch(
                "application_sdk.clients.redis.redis.Redis",
                side_effect=RedisConnectionError("ctor failed"),
            ),
        ):
            with pytest.raises(ClientError):
                standalone_client._connect()

    def test_connect_via_sentinel_ctor_failure_wrapped(self, sentinel_client):
        with (
            patch(
                "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS",
                "a:26379",
            ),
            patch(
                "application_sdk.clients.redis.redis.sentinel.Sentinel",
                side_effect=RedisConnectionError("sentinel down"),
            ),
        ):
            with pytest.raises(ClientError):
                sentinel_client._connect()


class TestSyncClose:
    """``RedisClient.close`` — must always null the handle (no leaks)."""

    def _client(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
        ):
            return RedisClient()

    def test_close_when_unconnected_is_noop(self):
        client = self._client()
        assert client.redis_client is None
        client.close()  # must not raise
        assert client.redis_client is None

    def test_close_swallows_exception_and_clears_handle(self):
        """Exception during close must be swallowed AND handle reset.

        A leak here would mean the next ``__enter__`` reuses a dead client.
        """
        client = self._client()
        client.redis_client = Mock()
        client.redis_client.close.side_effect = RuntimeError("boom")
        client.close()
        assert client.redis_client is None

    def test_close_normal_path(self):
        client = self._client()
        handle = Mock()
        client.redis_client = handle
        client.close()
        handle.close.assert_called_once()
        assert client.redis_client is None


class TestSyncContextManagerCleanupOnError:
    """``__exit__`` must always call ``close`` even when body raised."""

    def test_exit_calls_close_even_on_exception(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
        ):
            client = RedisClient()
        with (
            patch.object(client, "_connect"),
            patch.object(client, "close") as mock_close,
        ):
            try:
                with client:
                    raise RuntimeError("body raised")
            except RuntimeError:
                pass
            mock_close.assert_called_once()


# ---------------------------------------------------------------------------
# Async RedisClient connect / close
# ---------------------------------------------------------------------------


class TestAsyncConnect:
    """``RedisClientAsync._connect`` and helpers."""

    @pytest.fixture
    def standalone_client(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
        ):
            yield RedisClientAsync()

    @pytest.fixture
    def sentinel_client(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch(
                "application_sdk.clients.redis.REDIS_SENTINEL_HOSTS",
                "a:26379",
            ),
            patch(
                "application_sdk.clients.redis.REDIS_SENTINEL_SERVICE_NAME",
                "mymaster",
            ),
        ):
            yield RedisClientAsync()

    async def test_connect_noop_when_locking_disabled(self, standalone_client):
        with patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", True):
            await standalone_client._connect()
        assert standalone_client.redis_client is None

    async def test_connect_standalone_happy_path(self, standalone_client):
        fake_redis = AsyncMock()
        with (
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch(
                "application_sdk.clients.redis.async_redis.Redis",
                return_value=fake_redis,
            ) as redis_ctor,
        ):
            await standalone_client._connect()
        redis_ctor.assert_called_once_with(
            host="localhost", port=6379, password="secret"
        )
        fake_redis.ping.assert_awaited_once()
        assert standalone_client.redis_client is fake_redis

    async def test_connect_via_sentinel_happy_path(self, sentinel_client):
        master = AsyncMock()
        sentinel = MagicMock()
        sentinel.master_for.return_value = master
        with (
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", "a:26379"),
            patch(
                "application_sdk.clients.redis.REDIS_SENTINEL_SERVICE_NAME",
                "mymaster",
            ),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch(
                "application_sdk.clients.redis.async_redis.sentinel.Sentinel",
                return_value=sentinel,
            ),
        ):
            await sentinel_client._connect()
        sentinel.master_for.assert_called_once_with("mymaster", password="secret")
        master.ping.assert_awaited_once()
        assert sentinel_client.redis_client is master

    async def test_connect_ping_failure_wraps_to_client_error(self, standalone_client):
        fake_redis = AsyncMock()
        fake_redis.ping.side_effect = RedisConnectionError("nope")
        with (
            patch("application_sdk.clients.redis.REDIS_SENTINEL_HOSTS", ""),
            patch(
                "application_sdk.clients.redis.async_redis.Redis",
                return_value=fake_redis,
            ),
        ):
            with pytest.raises(ClientError) as ei:
                await standalone_client._connect()
        assert "ATLAN-CLIENT-503-00" in str(ei.value)


class TestAsyncClose:
    """``RedisClientAsync.close`` — uses ``aclose`` (avoids deprecation)."""

    def _client(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
        ):
            return RedisClientAsync()

    async def test_close_when_unconnected_is_noop(self):
        client = self._client()
        await client.close()
        assert client.redis_client is None

    async def test_close_calls_aclose_not_close(self):
        client = self._client()
        handle = AsyncMock()
        client.redis_client = handle
        await client.close()
        handle.aclose.assert_awaited_once()
        # close() / aclose() distinction matters in newer redis-py
        handle.close.assert_not_called()
        assert client.redis_client is None

    async def test_close_swallows_exception_and_clears_handle(self):
        client = self._client()
        handle = AsyncMock()
        handle.aclose.side_effect = RuntimeError("boom")
        client.redis_client = handle
        await client.close()  # must not raise
        assert client.redis_client is None


class TestAsyncContextManagerCleanupOnError:
    async def test_aexit_calls_close_even_on_exception(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
        ):
            client = RedisClientAsync()
        with (
            patch.object(client, "_connect", new=AsyncMock()),
            patch.object(client, "close", new=AsyncMock()) as mock_close,
        ):
            try:
                async with client:
                    raise RuntimeError("body raised")
            except RuntimeError:
                pass
            mock_close.assert_awaited_once()


# ---------------------------------------------------------------------------
# Lock operations on an unconnected client
# ---------------------------------------------------------------------------


class TestUnconnectedLockOperations:
    """Lock APIs must fail loudly (not crash with ``AttributeError``)."""

    def test_sync_acquire_unconnected_raises_client_error(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
        ):
            client = RedisClient()
        with pytest.raises(ClientError) as ei:
            client._acquire_lock("rid")
        assert "ATLAN-CLIENT-503-00" in str(ei.value)

    def test_sync_release_unconnected_raises_client_error(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
        ):
            client = RedisClient()
        with pytest.raises(ClientError) as ei:
            client._release_lock("rid")
        assert "ATLAN-CLIENT-503-00" in str(ei.value)

    async def test_async_acquire_unconnected_raises_client_error(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
        ):
            client = RedisClientAsync()
        with pytest.raises(ClientError):
            await client._acquire_lock("rid")

    async def test_async_release_unconnected_raises_client_error(self):
        with (
            patch("application_sdk.clients.redis.IS_LOCKING_DISABLED", False),
            patch("application_sdk.clients.redis.REDIS_PASSWORD", "secret"),
            patch("application_sdk.clients.redis.REDIS_HOST", "localhost"),
            patch("application_sdk.clients.redis.REDIS_PORT", "6379"),
        ):
            client = RedisClientAsync()
        with pytest.raises(ClientError):
            await client._release_lock("rid")


# ---------------------------------------------------------------------------
# _handle_redis_error preserves chained cause
# ---------------------------------------------------------------------------


class TestHandleRedisErrorChaining:
    """``raise X from e`` must preserve the original exception for tracing."""

    def test_chain_preserved_for_connection_error(self):
        original = RedisConnectionError("low-level")
        with pytest.raises(ClientError) as ei:
            _handle_redis_error(original)
        assert ei.value.__cause__ is original

    def test_chain_preserved_for_generic_error(self):
        original = RuntimeError("weird")
        with pytest.raises(ClientError) as ei:
            _handle_redis_error(original)
        assert ei.value.__cause__ is original


# ---------------------------------------------------------------------------
# BLDX-1129 SKIPs — bug shapes (NOT modifying source)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="BLDX-1129: _connect re-wraps its own ClientError via `except Exception` "
    "→ _handle_redis_error, masking the original error code path. "
    "Bug filed as TBD."
)
def test_bug_connect_rewraps_internal_client_error_skip():
    """When ``_connect`` raises ``ClientError(REDIS_CONNECTION_ERROR)``
    because ``self.redis_client`` is falsy after a connect helper, the broad
    ``except (... Exception)`` two lines later catches it and re-routes
    through ``_handle_redis_error``, which falls into the generic branch and
    re-raises a fresh ``ClientError`` with the same code but a wrapped cause —
    making the chain confusing for callers and obscuring the true control flow.
    """
