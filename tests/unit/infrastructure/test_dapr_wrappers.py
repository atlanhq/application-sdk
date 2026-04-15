"""Unit tests for Dapr wrapper classes."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from application_sdk.infrastructure._dapr.client import (
    DaprBinding,
    DaprPubSub,
    DaprSecretStore,
    DaprStateStore,
)
from application_sdk.infrastructure._dapr.http import AsyncDaprClient, BindingResult
from application_sdk.infrastructure.bindings import BindingError, BindingResponse
from application_sdk.infrastructure.pubsub import PubSubError
from application_sdk.infrastructure.secrets import SecretNotFoundError, SecretStoreError
from application_sdk.infrastructure.state import StateStoreError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_client() -> MagicMock:
    client = MagicMock(spec=AsyncDaprClient)
    client.save_state = AsyncMock()
    client.get_state = AsyncMock()
    client.delete_state = AsyncMock()
    client.get_secret = AsyncMock()
    client.get_bulk_secret = AsyncMock()
    client.publish_event = AsyncMock()
    client.invoke_binding = AsyncMock()
    return client


# ---------------------------------------------------------------------------
# DaprStateStore
# ---------------------------------------------------------------------------


class TestDaprStateStore:
    def setup_method(self):
        self.client = _make_client()
        self.store = DaprStateStore(self.client, store_name="mystate")

    async def test_save(self):
        value = {"foo": "bar"}
        await self.store.save("k1", value)
        self.client.save_state.assert_awaited_once_with(
            store_name="mystate", key="k1", value=json.dumps(value)
        )

    async def test_save_error(self):
        self.client.save_state.side_effect = RuntimeError("boom")
        with pytest.raises(StateStoreError):
            await self.store.save("k1", {"x": 1})

    async def test_load_returns_data(self):
        self.client.get_state.return_value = json.dumps({"a": 1})
        result = await self.store.load("k1")
        assert result == {"a": 1}
        self.client.get_state.assert_awaited_once_with(store_name="mystate", key="k1")

    async def test_load_returns_none_when_empty(self):
        self.client.get_state.return_value = None
        assert await self.store.load("k1") is None

    async def test_load_returns_none_when_falsy(self):
        self.client.get_state.return_value = ""
        assert await self.store.load("k1") is None

    async def test_load_error(self):
        self.client.get_state.side_effect = RuntimeError("boom")
        with pytest.raises(StateStoreError):
            await self.store.load("k1")

    async def test_delete(self):
        result = await self.store.delete("k1")
        assert result is True
        self.client.delete_state.assert_awaited_once_with(
            store_name="mystate", key="k1"
        )

    async def test_delete_error(self):
        self.client.delete_state.side_effect = RuntimeError("boom")
        with pytest.raises(StateStoreError):
            await self.store.delete("k1")

    async def test_list_keys_raises_not_implemented(self):
        with pytest.raises(NotImplementedError):
            await self.store.list_keys()


# ---------------------------------------------------------------------------
# DaprSecretStore
# ---------------------------------------------------------------------------


class TestDaprSecretStore:
    def setup_method(self):
        self.client = _make_client()
        self.store = DaprSecretStore(self.client, store_name="mysecrets")

    async def test_get_returns_value(self):
        self.client.get_secret.return_value = {"db_pass": "s3cret"}
        result = await self.store.get("db_pass")
        assert result == "s3cret"
        self.client.get_secret.assert_awaited_once_with(
            store_name="mysecrets", key="db_pass"
        )

    async def test_get_raises_not_found(self):
        self.client.get_secret.return_value = {"other_key": "val"}
        with pytest.raises(SecretNotFoundError):
            await self.store.get("missing")

    async def test_get_wraps_exception(self):
        self.client.get_secret.side_effect = RuntimeError("boom")
        with pytest.raises(SecretStoreError):
            await self.store.get("x")

    async def test_get_optional_returns_value(self):
        self.client.get_secret.return_value = {"token": "abc"}
        assert await self.store.get_optional("token") == "abc"

    async def test_get_optional_returns_none_when_missing(self):
        self.client.get_secret.return_value = {}
        assert await self.store.get_optional("nope") is None

    async def test_get_bulk(self):
        self.client.get_bulk_secret.return_value = {
            "a": {"a": "val_a"},
            "b": {"b": "val_b"},
            "c": {"c": "val_c"},
        }
        result = await self.store.get_bulk(["a", "c", "missing"])
        assert result == {"a": "val_a", "c": "val_c"}
        self.client.get_bulk_secret.assert_awaited_once_with(store_name="mysecrets")

    async def test_get_bulk_error(self):
        self.client.get_bulk_secret.side_effect = RuntimeError("boom")
        with pytest.raises(SecretStoreError):
            await self.store.get_bulk(["a"])

    async def test_list_names(self):
        self.client.get_bulk_secret.return_value = {
            "secret1": {"secret1": "v1"},
            "secret2": {"secret2": "v2"},
        }
        names = await self.store.list_names()
        assert sorted(names) == ["secret1", "secret2"]

    async def test_list_names_error(self):
        self.client.get_bulk_secret.side_effect = RuntimeError("boom")
        with pytest.raises(SecretStoreError):
            await self.store.list_names()


# ---------------------------------------------------------------------------
# DaprPubSub
# ---------------------------------------------------------------------------


class TestDaprPubSub:
    def setup_method(self):
        self.client = _make_client()
        self.pubsub = DaprPubSub(self.client, pubsub_name="mypubsub")

    async def test_publish(self):
        data = {"event": "created"}
        await self.pubsub.publish("orders", data)
        self.client.publish_event.assert_awaited_once_with(
            pubsub_name="mypubsub",
            topic="orders",
            data=json.dumps(data),
            metadata={},
        )

    async def test_publish_with_metadata(self):
        data = {"event": "created"}
        meta = {"ttl": "60"}
        await self.pubsub.publish("orders", data, metadata=meta)
        self.client.publish_event.assert_awaited_once_with(
            pubsub_name="mypubsub",
            topic="orders",
            data=json.dumps(data),
            metadata=meta,
        )

    async def test_publish_error(self):
        self.client.publish_event.side_effect = RuntimeError("boom")
        with pytest.raises(PubSubError):
            await self.pubsub.publish("t", {"x": 1})


# ---------------------------------------------------------------------------
# DaprBinding
# ---------------------------------------------------------------------------


class TestDaprBinding:
    def setup_method(self):
        self.client = _make_client()
        self.binding = DaprBinding(self.client, binding_name="s3")

    def test_name_property(self):
        assert self.binding.name == "s3"

    async def test_invoke_with_defaults(self):
        self.client.invoke_binding.return_value = BindingResult(
            data=b"hello", metadata={"k": "v"}
        )
        resp = await self.binding.invoke("get")
        assert isinstance(resp, BindingResponse)
        assert resp.data == b"hello"
        assert resp.metadata == {"k": "v"}
        self.client.invoke_binding.assert_awaited_once_with(
            binding_name="s3",
            operation="get",
            data=b"",
            metadata={},
        )

    async def test_invoke_with_data_and_metadata(self):
        self.client.invoke_binding.return_value = BindingResult(data=b"ok", metadata={})
        resp = await self.binding.invoke(
            "create", data=b"payload", metadata={"key": "val"}
        )
        assert resp.data == b"ok"
        self.client.invoke_binding.assert_awaited_once_with(
            binding_name="s3",
            operation="create",
            data=b"payload",
            metadata={"key": "val"},
        )

    async def test_invoke_none_data_response(self):
        self.client.invoke_binding.return_value = BindingResult(data=None, metadata={})
        resp = await self.binding.invoke("delete")
        assert resp.data is None
        assert resp.metadata == {}

    async def test_invoke_error(self):
        self.client.invoke_binding.side_effect = RuntimeError("boom")
        with pytest.raises(BindingError):
            await self.binding.invoke("get")
