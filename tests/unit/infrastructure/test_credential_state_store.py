"""Unit tests for credential infrastructure and mock protocol compliance.

Tests mock implementations and Dapr startup requirements.
"""

from __future__ import annotations

import pytest

from application_sdk.testing.mocks import MockSecretStore, MockStateStore


@pytest.fixture(autouse=True)
def _clean_infrastructure():
    """Reset infrastructure context after each test to prevent cross-test pollution."""
    yield
    from application_sdk.infrastructure.context import clear_infrastructure

    clear_infrastructure()


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


class TestDaprRequiredStartup:
    """Verify main.py requires Dapr sidecar."""

    async def test_no_dapr_raises_runtime_error(self, monkeypatch):
        """_create_infrastructure raises when DAPR_HTTP_PORT not set."""
        import pytest

        from application_sdk.errors import PreconditionError

        monkeypatch.delenv("DAPR_HTTP_PORT", raising=False)

        from application_sdk.main import _create_infrastructure

        with pytest.raises(PreconditionError, match="Dapr sidecar not detected"):
            await _create_infrastructure()

    async def test_error_message_includes_poe_command(self, monkeypatch):
        """Error message tells developer how to fix it."""
        from application_sdk.errors import PreconditionError

        monkeypatch.delenv("DAPR_HTTP_PORT", raising=False)

        from application_sdk.main import _create_infrastructure

        try:
            await _create_infrastructure()
        except PreconditionError as e:
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
