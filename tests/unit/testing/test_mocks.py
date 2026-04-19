"""Tests for application_sdk.testing.mocks call-tracking."""

from __future__ import annotations

import pytest

from application_sdk.infrastructure.bindings import BindingResponse
from application_sdk.testing.mocks import (
    MockBinding,
    MockHeartbeatController,
    MockPubSub,
    MockSecretStore,
    MockStateStore,
)


class TestMockStateStore:
    @pytest.mark.asyncio
    async def test_save_tracked(self) -> None:
        store = MockStateStore()
        await store.save("k", {"x": 1})
        assert store.get_save_calls() == [("k", {"x": 1})]

    @pytest.mark.asyncio
    async def test_load_tracked(self) -> None:
        store = MockStateStore()
        await store.save("k", {"x": 1})
        await store.load("k")
        assert store.get_load_calls() == ["k"]

    @pytest.mark.asyncio
    async def test_delete_tracked(self) -> None:
        store = MockStateStore()
        await store.save("k", {"x": 1})
        await store.delete("k")
        assert store.get_delete_calls() == ["k"]

    @pytest.mark.asyncio
    async def test_list_keys_tracked(self) -> None:
        store = MockStateStore()
        await store.list_keys("pref")
        assert store.get_list_keys_calls() == ["pref"]

    @pytest.mark.asyncio
    async def test_reset_calls_preserves_data(self) -> None:
        store = MockStateStore()
        await store.save("k", {"x": 1})
        store.reset_calls()
        assert store.get_save_calls() == []
        result = await store.load("k")
        assert result == {"x": 1}

    @pytest.mark.asyncio
    async def test_reset_clears_data(self) -> None:
        store = MockStateStore()
        await store.save("k", {"x": 1})
        store.reset()
        assert store.get_save_calls() == []
        result = await store.load("k")
        assert result is None


class TestMockSecretStore:
    @pytest.mark.asyncio
    async def test_get_tracked(self) -> None:
        store = MockSecretStore({"pw": "secret"})
        await store.get("pw")
        assert store.get_get_calls() == ["pw"]

    @pytest.mark.asyncio
    async def test_get_optional_tracked(self) -> None:
        store = MockSecretStore()
        await store.get_optional("missing")
        assert store.get_get_optional_calls() == ["missing"]

    @pytest.mark.asyncio
    async def test_reset_calls_preserves_secrets(self) -> None:
        store = MockSecretStore({"pw": "secret"})
        await store.get("pw")
        store.reset_calls()
        assert store.get_get_calls() == []
        assert await store.get("pw") == "secret"

    @pytest.mark.asyncio
    async def test_reset_clears_secrets(self) -> None:
        from application_sdk.infrastructure.secrets import SecretNotFoundError

        store = MockSecretStore({"pw": "secret"})
        store.reset()
        with pytest.raises(SecretNotFoundError):
            await store.get("pw")


class TestMockPubSub:
    @pytest.mark.asyncio
    async def test_publish_tracked(self) -> None:
        pubsub = MockPubSub()
        await pubsub.publish("events", {"key": "val"})
        calls = pubsub.get_publish_calls()
        assert len(calls) == 1
        assert calls[0][0] == "events"
        assert calls[0][1] == {"key": "val"}

    @pytest.mark.asyncio
    async def test_publish_filtered_by_topic(self) -> None:
        pubsub = MockPubSub()
        await pubsub.publish("topic-a", {"a": 1})
        await pubsub.publish("topic-b", {"b": 2})
        assert len(pubsub.get_publish_calls("topic-a")) == 1
        assert len(pubsub.get_publish_calls("topic-b")) == 1

    @pytest.mark.asyncio
    async def test_reset_calls_preserves_published(self) -> None:
        pubsub = MockPubSub()
        await pubsub.publish("t", {"x": 1})
        pubsub.reset_calls()
        assert pubsub.get_publish_calls() == []
        assert len(pubsub.get_published()) == 1

    @pytest.mark.asyncio
    async def test_reset_clears_all(self) -> None:
        pubsub = MockPubSub()
        await pubsub.publish("t", {"x": 1})
        pubsub.reset()
        assert pubsub.get_publish_calls() == []
        assert pubsub.get_published() == []


class TestMockBinding:
    @pytest.mark.asyncio
    async def test_invoke_tracked(self) -> None:
        binding = MockBinding()
        await binding.invoke("get", b"data", {"k": "v"})
        calls = binding.get_invoke_calls()
        assert len(calls) == 1
        assert calls[0] == ("get", b"data", {"k": "v"})

    @pytest.mark.asyncio
    async def test_invoke_filtered_by_operation(self) -> None:
        binding = MockBinding()
        await binding.invoke("get")
        await binding.invoke("put", b"x")
        assert len(binding.get_invoke_calls("get")) == 1
        assert len(binding.get_invoke_calls("put")) == 1

    @pytest.mark.asyncio
    async def test_configured_response_returned(self) -> None:
        binding = MockBinding()
        binding.set_response("get", BindingResponse(data=b"result"))
        resp = await binding.invoke("get")
        assert resp.data == b"result"

    @pytest.mark.asyncio
    async def test_reset_calls_preserves_responses(self) -> None:
        binding = MockBinding()
        binding.set_response("get", BindingResponse(data=b"x"))
        await binding.invoke("get")
        binding.reset_calls()
        assert binding.get_invoke_calls() == []
        resp = await binding.invoke("get")
        assert resp.data == b"x"

    @pytest.mark.asyncio
    async def test_reset_clears_responses(self) -> None:
        binding = MockBinding()
        binding.set_response("get", BindingResponse(data=b"x"))
        binding.reset()
        resp = await binding.invoke("get")
        assert resp.data is None


class TestMockHeartbeatController:
    def test_heartbeat_tracked(self) -> None:
        ctrl = MockHeartbeatController()
        ctrl.heartbeat(1, "progress")
        calls = ctrl.get_heartbeat_calls()
        assert calls == [(1, "progress")]

    def test_heartbeat_keepalive_tracked(self) -> None:
        ctrl = MockHeartbeatController()
        ctrl.heartbeat(42)
        ctrl.heartbeat_keepalive()
        calls = ctrl.get_heartbeat_calls()
        assert len(calls) == 2
        assert calls[1] == (42,)

    def test_get_last_heartbeat_details(self) -> None:
        ctrl = MockHeartbeatController()
        ctrl.heartbeat(10, 100)
        assert ctrl.get_last_heartbeat_details() == (10, 100)

    def test_reset_calls(self) -> None:
        ctrl = MockHeartbeatController()
        ctrl.heartbeat(1)
        ctrl.reset_calls()
        assert ctrl.get_heartbeat_calls() == []
