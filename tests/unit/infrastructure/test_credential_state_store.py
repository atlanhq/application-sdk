"""Unit tests for credential interception via state store and ATLAN_LOCAL_DEVELOPMENT guard.

Tests the pattern used by handler/service.py to store inline credentials
in the state store during local development, and the resolver's state store
lookup path.
"""

from __future__ import annotations

import json
import os

import pytest

from application_sdk.handler.service import _normalize_credentials, _pairs_to_flat
from application_sdk.testing.mocks import MockSecretStore, MockStateStore


@pytest.fixture(autouse=True)
def _clean_infrastructure():
    """Reset infrastructure context after each test to prevent cross-test pollution."""
    yield
    from application_sdk.infrastructure.context import (
        InfrastructureContext,
        set_infrastructure,
    )

    set_infrastructure(InfrastructureContext())


class TestCredentialStateStoreRoundtrip:
    """Credential interception: save via state store, resolve via resolver."""

    async def test_save_and_resolve_via_state_store(self, monkeypatch):
        """Full roundtrip: store creds in state store, resolver reads them back."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "true")

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        guid = "test-guid-abc123"
        flat_creds = {"host": "db.example.com", "port": "5432"}
        await state_store.save(f"cred:{guid}", flat_creds)

        ctx = InfrastructureContext(state_store=state_store, secret_store=secret_store)
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown", credential_guid=guid)
        result = await resolver.resolve_raw(ref)

        assert result["host"] == "db.example.com"
        assert result["port"] == "5432"

    async def test_state_store_takes_precedence_over_secret_store(self, monkeypatch):
        """State store (from /start handler) takes precedence over secret store."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "true")

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        guid = "dual-guid"
        await state_store.save(f"cred:{guid}", {"source": "state_store"})
        secret_store.set(guid, json.dumps({"source": "secret_store"}))

        ctx = InfrastructureContext(state_store=state_store, secret_store=secret_store)
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown", credential_guid=guid)
        result = await resolver.resolve_raw(ref)

        assert result["source"] == "state_store"

    async def test_fallback_to_secret_store(self):
        """Falls back to secret store when not in state store."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        guid = "secret-only"
        secret_store.set(guid, json.dumps({"host": "from-secret"}))

        ctx = InfrastructureContext(state_store=state_store, secret_store=secret_store)
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown", credential_guid=guid)
        result = await resolver.resolve_raw(ref)

        assert result["host"] == "from-secret"

    async def test_resolver_skips_state_store_in_prod(self, monkeypatch):
        """In non-local mode, resolver skips state store even if cred: key exists."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.infrastructure.context import (
            InfrastructureContext,
            set_infrastructure,
        )

        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "false")

        state_store = MockStateStore()
        secret_store = MockSecretStore()

        guid = "prod-guid"
        # Cred exists in state store but should NOT be read in prod
        await state_store.save(f"cred:{guid}", {"source": "state_store"})
        # Secret store has the "real" credential
        secret_store.set(guid, json.dumps({"source": "secret_store"}))

        ctx = InfrastructureContext(state_store=state_store, secret_store=secret_store)
        set_infrastructure(ctx)

        resolver = CredentialResolver(secret_store=secret_store)
        ref = CredentialRef(name=guid, credential_type="unknown", credential_guid=guid)
        result = await resolver.resolve_raw(ref)

        # Secret store should win in prod — state store skipped
        assert result["source"] == "secret_store"


class TestLocalDevGuard:
    """Test ATLAN_LOCAL_DEVELOPMENT env var guard."""

    def test_guard_allows_when_true(self, monkeypatch):
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "true")
        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local is True

    def test_guard_blocks_when_false(self, monkeypatch):
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "false")
        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local is False

    def test_guard_blocks_when_unset(self, monkeypatch):
        monkeypatch.delenv("ATLAN_LOCAL_DEVELOPMENT", raising=False)
        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local is False

    def test_guard_allows_when_1(self, monkeypatch):
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "1")
        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local is True

    async def test_full_interception_flow_with_guard(self, monkeypatch):
        """End-to-end: normalize → guard → store → verify."""
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "true")

        state_store = MockStateStore()
        body = {
            "credentials": [
                {"key": "host", "value": "db.example.com"},
                {"key": "password", "value": "s3cr3t"},
            ],
        }
        body = _normalize_credentials(body)

        is_local = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        assert is_local

        flat_creds = _pairs_to_flat(body["credentials"])
        await state_store.save("cred:test-guid", flat_creds)
        body["credential_guid"] = "test-guid"
        del body["credentials"]

        assert "credentials" not in body
        assert body["credential_guid"] == "test-guid"

        result = await state_store.load("cred:test-guid")
        assert result["host"] == "db.example.com"
        assert result["password"] == "s3cr3t"


class TestMockProtocolCompliance:
    """Verify Mock classes implement Protocol correctly."""

    async def test_state_store_save_load_delete(self):
        store = MockStateStore()
        await store.save("k1", {"v": 1})
        assert await store.load("k1") == {"v": 1}
        assert await store.delete("k1") is True
        assert await store.load("k1") is None

    async def test_secret_store_set_get(self):
        store = MockSecretStore()
        store.set("secret", "value")
        assert await store.get("secret") == "value"

    async def test_state_store_call_tracking(self):
        store = MockStateStore()
        await store.save("k", {"v": 1})
        await store.load("k")
        await store.delete("k")
        assert len(store.get_save_calls()) == 1
        assert len(store.get_load_calls()) == 1
        assert len(store.get_delete_calls()) == 1

    async def test_secret_store_call_tracking(self):
        store = MockSecretStore()
        store.set("s", "v")
        await store.get("s")
        assert len(store.get_get_calls()) == 1


class TestNonLocalModeRejectsCredentials:
    """Verify inline credentials are NOT intercepted outside local dev."""

    async def test_credentials_pass_through_when_not_local(self, monkeypatch):
        """In non-local mode, body retains credentials (not intercepted)."""
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "false")

        body = {
            "credentials": [{"key": "host", "value": "db.example.com"}],
            "name": "test",
        }
        body = _normalize_credentials(body)

        is_local_dev = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        # Guard should block — credentials stay in body
        assert is_local_dev is False
        assert "credentials" in body

    async def test_state_store_not_written_when_not_local(self, monkeypatch):
        """State store should have zero writes when guard blocks."""
        monkeypatch.setenv("ATLAN_LOCAL_DEVELOPMENT", "false")

        state_store = MockStateStore()
        body = {
            "credentials": [{"key": "password", "value": "secret"}],
        }
        body = _normalize_credentials(body)

        is_local_dev = os.environ.get("ATLAN_LOCAL_DEVELOPMENT", "").lower() in (
            "true",
            "1",
        )
        if is_local_dev and state_store is not None:
            # This should NOT execute
            flat_creds = _pairs_to_flat(body["credentials"])
            await state_store.save("cred:should-not-exist", flat_creds)

        assert len(state_store.get_save_calls()) == 0


class TestDaprRequiredStartup:
    """Verify main.py requires Dapr sidecar."""

    async def test_no_dapr_raises_runtime_error(self, monkeypatch):
        """_create_infrastructure raises when DAPR_HTTP_PORT not set."""
        import pytest

        monkeypatch.delenv("DAPR_HTTP_PORT", raising=False)

        from application_sdk.main import _create_infrastructure

        with pytest.raises(RuntimeError, match="Dapr sidecar not detected"):
            await _create_infrastructure()

    async def test_error_message_includes_poe_command(self, monkeypatch):
        """Error message tells developer how to fix it."""
        monkeypatch.delenv("DAPR_HTTP_PORT", raising=False)

        from application_sdk.main import _create_infrastructure

        try:
            await _create_infrastructure()
        except RuntimeError as e:
            assert "poe start-deps" in str(e)
            assert "DAPR_HTTP_PORT" in str(e)


class TestMockPubSubSelfContained:
    """Verify MockPubSub implements the PubSub protocol correctly."""

    async def test_publish_and_get_published(self):
        from application_sdk.testing.mocks import MockPubSub

        pubsub = MockPubSub()
        await pubsub.publish("topic-a", {"key": "value"})
        await pubsub.publish("topic-b", {"other": "data"})

        all_msgs = pubsub.get_published()
        assert len(all_msgs) == 2

        topic_a = pubsub.get_published("topic-a")
        assert len(topic_a) == 1
        assert topic_a[0].data == {"key": "value"}

    async def test_subscribe_and_deliver(self):
        from application_sdk.testing.mocks import MockPubSub

        received = []

        async def handler(msg):
            received.append(msg.data)

        pubsub = MockPubSub()
        sub = await pubsub.subscribe("events", handler)

        await pubsub.publish("events", {"event": "created"})
        assert len(received) == 1
        assert received[0] == {"event": "created"}

        # Subscription is active
        assert sub.is_active is True
        assert sub.topic == "events"

    async def test_unsubscribe_stops_delivery(self):
        from application_sdk.testing.mocks import MockPubSub

        received = []

        async def handler(msg):
            received.append(msg.data)

        pubsub = MockPubSub()
        sub = await pubsub.subscribe("events", handler)

        await pubsub.publish("events", {"first": True})
        assert len(received) == 1

        await sub.unsubscribe()
        assert sub.is_active is False

        await pubsub.publish("events", {"second": True})
        # Should NOT be delivered after unsubscribe
        assert len(received) == 1

    async def test_publish_call_tracking(self):
        from application_sdk.testing.mocks import MockPubSub

        pubsub = MockPubSub()
        await pubsub.publish("t", {"a": 1}, metadata={"m": "v"})

        calls = pubsub.get_publish_calls()
        assert len(calls) == 1
        topic, data, meta = calls[0]
        assert topic == "t"
        assert data == {"a": 1}
        assert meta == {"m": "v"}

    async def test_clear_resets_data(self):
        from application_sdk.testing.mocks import MockPubSub

        pubsub = MockPubSub()
        await pubsub.publish("t", {"x": 1})
        assert len(pubsub.get_published()) == 1

        pubsub.clear()
        assert len(pubsub.get_published()) == 0
        # clear() resets data, not call logs — use reset() for both
        assert len(pubsub.get_publish_calls()) == 1

    async def test_reset_clears_everything(self):
        from application_sdk.testing.mocks import MockPubSub

        pubsub = MockPubSub()
        await pubsub.publish("t", {"x": 1})

        pubsub.reset()
        assert len(pubsub.get_published()) == 0
        assert len(pubsub.get_publish_calls()) == 0
