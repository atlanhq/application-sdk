"""Unit tests for the async AsyncDaprClient."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application_sdk.infrastructure._dapr.http import (
    BINDING_PATH,
    METADATA_PATH,
    PUBLISH_PATH,
    SECRET_STORE_BULK_PATH,
    SECRET_STORE_PATH,
    STATE_KEY_PATH,
    STATE_PATH,
    AsyncDaprClient,
    BindingResult,
)


@pytest.fixture
def mock_client():
    """Create an AsyncDaprClient with a mocked httpx.AsyncClient."""
    with (
        patch(
            "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
        ) as mock_cls,
        patch("application_sdk.infrastructure._dapr.http.RetryTransport"),
        patch("application_sdk.infrastructure._dapr.http.httpx.AsyncHTTPTransport"),
    ):
        mock_http = AsyncMock()
        mock_cls.return_value = mock_http
        client = AsyncDaprClient(base_url="http://localhost:3500")
        yield client, mock_http


class TestAsyncDaprClientState:
    @pytest.mark.asyncio
    async def test_save_state(self, mock_client):
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=204, raise_for_status=MagicMock()
        )

        await client.save_state("mystore", "key1", '{"value": 1}')

        mock_http.post.assert_called_once()
        args = mock_http.post.call_args
        assert "/v1.0/state/mystore" in args[0][0]
        assert args[1]["json"] == [{"key": "key1", "value": '{"value": 1}'}]

    @pytest.mark.asyncio
    async def test_get_state_returns_data(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.return_value = MagicMock(
            status_code=200,
            content=b'{"value": 1}',
            raise_for_status=MagicMock(),
        )

        result = await client.get_state("mystore", "key1")

        assert result == b'{"value": 1}'

    @pytest.mark.asyncio
    async def test_get_state_returns_none_for_missing(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.return_value = MagicMock(
            status_code=204,
            content=b"",
            raise_for_status=MagicMock(),
        )

        result = await client.get_state("mystore", "missing")

        assert result is None

    @pytest.mark.asyncio
    async def test_delete_state(self, mock_client):
        client, mock_http = mock_client
        mock_http.delete.return_value = MagicMock(
            status_code=204, raise_for_status=MagicMock()
        )

        await client.delete_state("mystore", "key1")

        mock_http.delete.assert_called_once()


class TestAsyncDaprClientSecrets:
    @pytest.mark.asyncio
    async def test_get_secret(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"api_key": "secret123"}),
            raise_for_status=MagicMock(),
        )

        result = await client.get_secret("secretstore", "api_key")

        assert result == {"api_key": "secret123"}

    @pytest.mark.asyncio
    async def test_get_bulk_secret(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value={"key1": {"key1": "val1"}, "key2": {"key2": "val2"}}
            ),
            raise_for_status=MagicMock(),
        )

        result = await client.get_bulk_secret("secretstore")

        assert "key1" in result
        assert "key2" in result


class TestAsyncDaprClientPubSub:
    @pytest.mark.asyncio
    async def test_publish_event(self, mock_client):
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=204, raise_for_status=MagicMock()
        )

        await client.publish_event("pubsub", "my-topic", '{"msg": "hello"}')

        mock_http.post.assert_called_once()
        args = mock_http.post.call_args
        assert "/v1.0/publish/pubsub/my-topic" in args[0][0]

    @pytest.mark.asyncio
    async def test_publish_event_with_metadata(self, mock_client):
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=204, raise_for_status=MagicMock()
        )

        await client.publish_event(
            "pubsub", "topic", "{}", metadata={"rawPayload": "true"}
        )

        headers = mock_http.post.call_args[1]["headers"]
        assert headers["metadata.rawPayload"] == "true"


class TestAsyncDaprClientBinding:
    @pytest.mark.asyncio
    async def test_invoke_binding(self, mock_client):
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=200,
            content=b"result data",
            headers={"x-custom": "value"},
            raise_for_status=MagicMock(),
        )

        result = await client.invoke_binding(
            "my-binding", "create", data=b"payload", metadata={"key": "val"}
        )

        assert isinstance(result, BindingResult)
        assert result.data == b"result data"
        mock_http.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_invoke_binding_no_data(self, mock_client):
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=200,
            content=b"",
            headers={},
            raise_for_status=MagicMock(),
        )

        result = await client.invoke_binding("my-binding", "list")

        assert result.data is None


class TestAsyncDaprClientMetadata:
    @pytest.mark.asyncio
    async def test_get_metadata(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(
                return_value={
                    "id": "my-app",
                    "registeredComponents": [
                        {"name": "statestore", "type": "state.redis", "version": "v1"}
                    ],
                }
            ),
            raise_for_status=MagicMock(),
        )

        result = await client.get_metadata()

        assert result["id"] == "my-app"
        assert len(result["registeredComponents"]) == 1
        assert result["registeredComponents"][0]["name"] == "statestore"


class TestApiPathConstants:
    """Verify API path constants are well-formed."""

    def test_state_path(self):
        assert STATE_PATH.format(store_name="mystore") == "/v1.0/state/mystore"

    def test_state_key_path(self):
        result = STATE_KEY_PATH.format(store_name="mystore", key="k1")
        assert result == "/v1.0/state/mystore/k1"

    def test_secret_path(self):
        result = SECRET_STORE_PATH.format(store_name="secretstore", key="api_key")
        assert result == "/v1.0/secrets/secretstore/api_key"

    def test_secret_bulk_path(self):
        result = SECRET_STORE_BULK_PATH.format(store_name="secretstore")
        assert result == "/v1.0/secrets/secretstore/bulk"

    def test_publish_path(self):
        result = PUBLISH_PATH.format(pubsub_name="pubsub", topic="orders")
        assert result == "/v1.0/publish/pubsub/orders"

    def test_binding_path(self):
        result = BINDING_PATH.format(binding_name="eventstore")
        assert result == "/v1.0/bindings/eventstore"

    def test_metadata_path(self):
        assert METADATA_PATH == "/v1.0/metadata"


class TestBindingResultModel:
    """Verify BindingResult pydantic model."""

    def test_default_values(self):
        result = BindingResult()
        assert result.data is None
        assert result.metadata == {}

    def test_with_data(self):
        result = BindingResult(data=b"hello", metadata={"key": "val"})
        assert result.data == b"hello"
        assert result.metadata == {"key": "val"}

    def test_serialization(self):
        result = BindingResult(data=b"test", metadata={"a": "b"})
        d = result.model_dump()
        assert d["metadata"] == {"a": "b"}


class TestRetryConfiguration:
    """Verify retry transport is properly configured."""

    def test_default_retry_transport(self):
        """Client should use RetryTransport by default."""
        from httpx_retries import RetryTransport

        client = AsyncDaprClient(base_url="http://localhost:3500")
        assert isinstance(client._client._transport, RetryTransport)

    def test_custom_retry_count(self):
        """Client should accept custom retry count."""
        from httpx_retries import RetryTransport

        client = AsyncDaprClient(base_url="http://localhost:3500", retries=5)
        assert isinstance(client._client._transport, RetryTransport)

    def test_zero_retries(self):
        """Client with retries=0 still uses RetryTransport (0 retries = no retry)."""
        from httpx_retries import RetryTransport

        client = AsyncDaprClient(base_url="http://localhost:3500", retries=0)
        assert isinstance(client._client._transport, RetryTransport)
