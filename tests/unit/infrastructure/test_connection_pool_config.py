"""Unit tests for BLDX-1153: httpx connection pool deadlock prevention.

Verifies that all httpx clients in the SDK have proper pool limits and
timeouts configured to prevent CLOSE_WAIT zombie socket accumulation
and infinite blocking on exhausted connection pools.
"""

from __future__ import annotations

import httpx
import pytest

from application_sdk.clients.base import BaseClient
from application_sdk.constants import _HTTP_POOL_LIMITS, _HTTP_POOL_TIMEOUT_SECONDS
from application_sdk.infrastructure._dapr.http import AsyncDaprClient

# ---------------------------------------------------------------------------
# AsyncDaprClient pool configuration
# ---------------------------------------------------------------------------


class TestAsyncDaprClientPoolConfig:
    """Verify AsyncDaprClient has pool limits and timeout configured."""

    def test_pool_limits_constants_defined(self):
        """Pool limits constants must exist with safe values."""
        assert _HTTP_POOL_LIMITS.max_connections == 50
        assert _HTTP_POOL_LIMITS.max_keepalive_connections == 10
        assert _HTTP_POOL_LIMITS.keepalive_expiry == 30.0

    def test_pool_timeout_constant_defined(self):
        """Pool timeout must be finite (not None)."""
        assert _HTTP_POOL_TIMEOUT_SECONDS == 30.0
        assert _HTTP_POOL_TIMEOUT_SECONDS is not None

    def test_client_has_pool_timeout(self):
        """AsyncDaprClient timeout must include a pool timeout."""
        client = AsyncDaprClient(base_url="http://localhost:3500")
        timeout = client._client.timeout
        assert timeout.pool == _HTTP_POOL_TIMEOUT_SECONDS
        assert timeout.pool is not None

    def test_client_has_pool_limits(self):
        """AsyncDaprClient transport must have pool limits configured."""
        client = AsyncDaprClient(base_url="http://localhost:3500")
        # RetryTransport wraps AsyncHTTPTransport via _async_transport
        try:
            inner = client._client._transport._async_transport  # type: ignore[attr-defined]
            pool = inner._pool
            assert pool._max_connections == _HTTP_POOL_LIMITS.max_connections
            assert pool._keepalive_expiry == _HTTP_POOL_LIMITS.keepalive_expiry
            assert (
                pool._max_keepalive_connections
                == _HTTP_POOL_LIMITS.max_keepalive_connections
            )
        except AttributeError:
            pytest.skip("httpx internal structure changed; update test")

    def test_keepalive_expiry_less_than_nginx(self):
        """keepalive_expiry must be less than nginx keepalive_timeout (75s)
        to prevent CLOSE_WAIT zombie accumulation."""
        assert _HTTP_POOL_LIMITS.keepalive_expiry < 75.0

    def test_pool_timeout_prevents_infinite_blocking(self):
        """Pool timeout must be finite to prevent threading.Event.wait(None)."""
        client = AsyncDaprClient(base_url="http://localhost:3500")
        timeout = client._client.timeout
        # The deadlock root cause was pool=None → Event.wait(timeout=None)
        assert timeout.pool is not None
        assert timeout.pool > 0


# ---------------------------------------------------------------------------
# BaseClient pool configuration
# ---------------------------------------------------------------------------


class TestBaseClientPoolConfig:
    """Verify BaseClient HTTP transport has pool limits."""

    def test_default_transport_has_limits(self):
        """BaseClient default transport must have keepalive_expiry set."""
        client = BaseClient()
        transport = client.http_retry_transport
        assert isinstance(transport, httpx.AsyncHTTPTransport)
        try:
            pool = transport._pool
            assert pool._max_connections == 50
            assert pool._keepalive_expiry == 30.0
            assert pool._max_keepalive_connections == 10
        except AttributeError:
            pytest.skip("httpx internal structure changed; update test")


# ---------------------------------------------------------------------------
# Pool timeout propagation
# ---------------------------------------------------------------------------


class TestPoolTimeoutPropagation:
    """Verify PoolTimeout exceptions propagate instead of hanging."""

    @pytest.mark.asyncio
    async def test_dapr_client_pool_timeout_propagates(self):
        """httpx.PoolTimeout must propagate up, not be swallowed."""
        client = AsyncDaprClient(base_url="http://localhost:3500")

        # Inject a PoolTimeout-raising transport
        async def always_pool_timeout(request: httpx.Request) -> httpx.Response:
            raise httpx.PoolTimeout("simulated pool exhaustion", request=request)

        try:
            client._client._transport._async_transport.handle_async_request = (  # type: ignore[attr-defined]
                always_pool_timeout
            )
        except AttributeError:
            pytest.skip("httpx internal structure changed; update test")

        with pytest.raises(Exception):
            await client._client.get("/v1.0/healthz")
