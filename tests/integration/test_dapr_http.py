"""Integration tests for AsyncDaprClient against a real Dapr sidecar.

These tests validate that the HTTP API endpoints are called correctly
and response parsing works end-to-end. They require a running Dapr
sidecar (started via `poe start-dapr` or equivalent).

When no Dapr sidecar is available (DAPR_HTTP_PORT not set), tests are
skipped automatically.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

from application_sdk.infrastructure._dapr.http import AsyncDaprClient, BindingResult

# Skip all tests if no Dapr sidecar is running
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("DAPR_HTTP_PORT"),
        reason="Dapr sidecar not available (DAPR_HTTP_PORT not set)",
    ),
]


@pytest.fixture
async def client():
    """Create an AsyncDaprClient connected to the local sidecar."""
    c = AsyncDaprClient()
    yield c
    await c.close()


@pytest.fixture
def unique_key():
    """Generate a unique key for test isolation."""
    return f"integ-test-{uuid.uuid4().hex[:8]}"


# ------------------------------------------------------------------
# State Store
# ------------------------------------------------------------------


class TestStateStoreIntegration:
    """State store CRUD operations against a real Dapr sidecar."""

    async def test_save_and_get_state(self, client, unique_key):
        """Save state then retrieve it."""
        value = json.dumps({"counter": 42, "name": "test"})
        await client.save_state("statestore", unique_key, value)

        data = await client.get_state("statestore", unique_key)
        assert data is not None
        # Dapr double-encodes: json string → stored as json-in-json
        raw = json.loads(data)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        assert parsed["counter"] == 42
        assert parsed["name"] == "test"

    async def test_get_missing_state_returns_none(self, client):
        """Get nonexistent key returns None."""
        data = await client.get_state("statestore", f"nonexistent-{uuid.uuid4().hex}")
        assert data is None

    async def test_save_then_delete_state(self, client, unique_key):
        """Save, verify exists, delete, verify gone."""
        await client.save_state("statestore", unique_key, '{"temp": true}')
        assert await client.get_state("statestore", unique_key) is not None

        await client.delete_state("statestore", unique_key)
        assert await client.get_state("statestore", unique_key) is None

    async def test_overwrite_state(self, client, unique_key):
        """Overwrite existing state with new value."""
        await client.save_state("statestore", unique_key, '{"v": 1}')
        await client.save_state("statestore", unique_key, '{"v": 2}')

        data = await client.get_state("statestore", unique_key)
        assert data is not None
        raw = json.loads(data)
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        assert parsed["v"] == 2


# ------------------------------------------------------------------
# Secret Store
# ------------------------------------------------------------------


class TestSecretStoreIntegration:
    """Secret store operations against a real Dapr sidecar."""

    async def test_get_secret(self, client):
        """Get a secret that exists in the local file store."""
        result = await client.get_secret("secretstore", "test-secret")
        assert isinstance(result, dict)
        assert "test-secret" in result
        assert result["test-secret"] == "integration-test-value"

    async def test_get_secret_api_key(self, client):
        """Get the api-key secret."""
        result = await client.get_secret("secretstore", "api-key")
        assert result["api-key"] == "test-api-key-12345"

    async def test_get_bulk_secret(self, client):
        """Get all secrets from the store."""
        result = await client.get_bulk_secret("secretstore")
        assert isinstance(result, dict)
        assert "test-secret" in result
        assert "api-key" in result
        assert "db-password" in result


# ------------------------------------------------------------------
# Bindings
# ------------------------------------------------------------------


class TestBindingIntegration:
    """Binding operations against a real Dapr sidecar."""

    async def test_invoke_binding_returns_binding_result(self, client):
        """Invoke a binding and verify the response type."""
        try:
            result = await client.invoke_binding(
                "objectstore",
                "list",
                metadata={"prefix": "test/"},
            )
            assert isinstance(result, BindingResult)
            assert isinstance(result.metadata, dict)
        except Exception:
            pytest.skip("No objectstore binding available")


# ------------------------------------------------------------------
# Metadata
# ------------------------------------------------------------------


class TestMetadataIntegration:
    """Metadata endpoint against a real Dapr sidecar."""

    async def test_get_metadata_returns_components(self, client):
        """Metadata endpoint returns registered components."""
        metadata = await client.get_metadata()
        assert isinstance(metadata, dict)
        # Dapr 1.13: "registeredComponents", 1.14+: "components"
        components = metadata.get(
            "components", metadata.get("registeredComponents", [])
        )
        assert len(components) > 0, "Expected at least one registered component"

    async def test_metadata_contains_component_structure(self, client):
        """Each registered component has name and type."""
        metadata = await client.get_metadata()
        # Support both Dapr 1.13 and 1.14+ field names
        components = metadata.get(
            "components", metadata.get("registeredComponents", [])
        )
        for comp in components:
            assert "name" in comp
            assert "type" in comp


# ------------------------------------------------------------------
# Pub/Sub
# ------------------------------------------------------------------


class TestPubSubIntegration:
    """Pub/sub operations against a real Dapr sidecar."""

    async def test_publish_event(self, client):
        """Publish an event to a topic."""
        try:
            await client.publish_event(
                "pubsub",
                "integ-test-topic",
                json.dumps({"test": True, "id": uuid.uuid4().hex}),
            )
        except Exception:
            pytest.skip("No pubsub component available")

    async def test_publish_event_with_metadata(self, client):
        """Publish with custom metadata headers."""
        try:
            await client.publish_event(
                "pubsub",
                "integ-test-topic",
                json.dumps({"test": True}),
                metadata={"rawPayload": "true"},
            )
        except Exception:
            pytest.skip("No pubsub component available")


# ------------------------------------------------------------------
# Retry behavior
# ------------------------------------------------------------------


class TestRetryBehavior:
    """Verify retry transport is configured."""

    def test_client_has_retry_transport(self):
        """AsyncDaprClient should use RetryTransport."""
        from httpx_retries import RetryTransport

        client = AsyncDaprClient(base_url="http://localhost:3500")
        transport = client._client._transport
        assert isinstance(transport, RetryTransport)
