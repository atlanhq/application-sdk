"""Regression tests for retry policy of Dapr binding invocations."""

from __future__ import annotations

import httpx
import pytest
from httpx_retries import Retry, RetryTransport

from application_sdk.infrastructure._dapr.http import AsyncDaprClient


class TestIdempotentBindingRetries:
    async def test_get_binding_retries_connect_timeout(self):
        """A binding read retries a transient Dapr connection timeout."""
        attempts = 0

        class TimeoutThenSuccessTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise httpx.ConnectTimeout("Dapr unavailable", request=request)
                return httpx.Response(200, content=b"credential", request=request)

        client = AsyncDaprClient(base_url="http://localhost:3500", retries=1)
        client._client._transport = RetryTransport(
            transport=TimeoutThenSuccessTransport(),
            retry=Retry(total=1, backoff_factor=0),
        )

        result = await client.invoke_binding("credential-config", "get")

        assert result.data == b"credential"
        assert attempts == 2
        await client.close()

    async def test_non_get_binding_does_not_retry_connect_timeout(self):
        """A potentially mutating binding POST remains single-attempt."""
        attempts = 0

        class TimeoutTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal attempts
                attempts += 1
                raise httpx.ConnectTimeout("Dapr unavailable", request=request)

        client = AsyncDaprClient(base_url="http://localhost:3500", retries=1)
        client._client._transport = RetryTransport(
            transport=TimeoutTransport(), retry=Retry(total=1, backoff_factor=0)
        )

        with pytest.raises(httpx.ConnectTimeout):
            await client.invoke_binding("eventstore", "create")

        assert attempts == 1
        await client.close()
