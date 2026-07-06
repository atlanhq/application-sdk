"""Unit tests for Dapr wrapper classes."""

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import orjson
import pytest

from application_sdk.infrastructure._dapr._dapr_errors import (
    DaprListKeysUnsupportedError,
)
from application_sdk.infrastructure._dapr.client import (
    DaprBinding,
    DaprPubSub,
    DaprSecretStore,
    DaprStateStore,
)
from application_sdk.infrastructure._dapr.http import AsyncDaprClient, BindingResult
from application_sdk.infrastructure.bindings import BindingError, BindingResponse
from application_sdk.infrastructure.pubsub import PubSubError
from application_sdk.infrastructure.secrets import (
    SecretNotFoundError,
    SecretStoreError,
    SecretStoreUnavailableError,
)
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
            store_name="mystate", key="k1", value=value
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
        with pytest.raises(DaprListKeysUnsupportedError):
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

    async def test_get_multi_key_returns_serialized_json(self):
        """Multi-key response (name not in dict) is serialized as JSON."""
        self.client.get_secret.return_value = {"user": "u", "pass": "p"}
        import orjson

        result = await self.store.get("some-secret")
        assert orjson.loads(result) == {"user": "u", "pass": "p"}

    async def test_get_raises_not_found_on_empty(self):
        self.client.get_secret.return_value = {}
        with pytest.raises(SecretNotFoundError):
            await self.store.get("missing")

    async def test_get_wraps_exception(self):
        self.client.get_secret.side_effect = RuntimeError("boom")
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("x")
        # A generic error is a plain store error, NOT the "unreachable" subtype.
        assert not isinstance(exc.value, SecretStoreUnavailableError)

    async def test_transport_error_raises_unavailable(self):
        """A cold-start ConnectError → SecretStoreUnavailableError (retryable)."""
        self.client.get_secret.side_effect = httpx.ConnectError(
            "All connection attempts failed"
        )
        with pytest.raises(SecretStoreUnavailableError) as exc:
            await self.store.get("x")
        assert isinstance(exc.value.cause, httpx.ConnectError)

    async def test_read_error_raises_unavailable(self):
        """Chris #1: a reset-mid-handshake surfaces as ReadError (a TransportError,
        not ConnectError) — it MUST still be classified unavailable, not fail-fast."""
        self.client.get_secret.side_effect = httpx.ReadError("connection reset")
        with pytest.raises(SecretStoreUnavailableError):
            await self.store.get("x")

    async def test_5xx_without_not_configured_code_is_not_unavailable(self):
        """A bare 5xx is NOT enough to infer "cold sidecar": verified against a
        live Dapr sidecar, a *genuinely missing* secret key also returns 500
        with errorCode=ERR_SECRET_GET — identical to a still-cold component.
        Treating this as retryable would retry a permanently-missing secret
        for the full cold-start budget and then misreport it as a platform
        outage instead of not-found."""
        req = httpx.Request("GET", "http://localhost/secret")
        resp = httpx.Response(
            500,
            request=req,
            json={
                "errorCode": "ERR_SECRET_GET",
                "message": "failed getting secret with key x from secret store "
                "mysecrets: secret x not found",
            },
        )
        self.client.get_secret.side_effect = httpx.HTTPStatusError(
            "internal error", request=req, response=resp
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("x")
        assert not isinstance(exc.value, SecretStoreUnavailableError)
        assert exc.value.effective_retryable is False

    async def test_5xx_without_json_body_is_not_unavailable(self):
        """No parseable error body → unknown errorCode → treated the same as
        a definitive rejection, not optimistically as a cold start."""
        req = httpx.Request("GET", "http://localhost/secret")
        resp = httpx.Response(503, request=req)
        self.client.get_secret.side_effect = httpx.HTTPStatusError(
            "service unavailable", request=req, response=resp
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("x")
        assert not isinstance(exc.value, SecretStoreUnavailableError)

    async def test_5xx_store_not_configured_raises_unavailable(self):
        """The one Dapr secrets-API error code that unambiguously means "not
        ready yet" (no secret store component registered at all) — still
        cold-start-shaped, still retried."""
        req = httpx.Request("GET", "http://localhost/secret")
        resp = httpx.Response(
            500,
            request=req,
            json={
                "errorCode": "ERR_SECRET_STORES_NOT_CONFIGURED",
                "message": "secret store is not configured",
            },
        )
        self.client.get_secret.side_effect = httpx.HTTPStatusError(
            "internal error", request=req, response=resp
        )
        with pytest.raises(SecretStoreUnavailableError):
            await self.store.get("x")

    async def test_4xx_status_raises_plain_store_error(self):
        """A 4xx is a definitive rejection (bad binding/auth/path) → fail fast."""
        req = httpx.Request("GET", "http://localhost/secret")
        resp = httpx.Response(403, request=req)
        self.client.get_secret.side_effect = httpx.HTTPStatusError(
            "forbidden", request=req, response=resp
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("x")
        assert not isinstance(exc.value, SecretStoreUnavailableError)
        # A 4xx would fail identically on every retry — marked explicitly
        # non-retryable rather than inheriting the optimistic default.
        assert exc.value.effective_retryable is False

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
            data=orjson.dumps(data).decode(),
            metadata={},
        )

    async def test_publish_with_metadata(self):
        data = {"event": "created"}
        meta = {"ttl": "60"}
        await self.pubsub.publish("orders", data, metadata=meta)
        self.client.publish_event.assert_awaited_once_with(
            pubsub_name="mypubsub",
            topic="orders",
            data=orjson.dumps(data).decode(),
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


# ---------------------------------------------------------------------------
# DaprPubSub.subscribe / DaprSubscription
# ---------------------------------------------------------------------------


class TestDaprSubscription:
    def setup_method(self):
        self.client = MagicMock(spec=AsyncDaprClient)
        self.pubsub = DaprPubSub(self.client, pubsub_name="mypubsub")

    async def test_subscribe_returns_subscription(self):
        from application_sdk.infrastructure._dapr.client import DaprSubscription

        handler = AsyncMock()
        sub = await self.pubsub.subscribe("my-topic", handler)
        assert isinstance(sub, DaprSubscription)
        assert sub.topic == "my-topic"
        assert sub.is_active is True

    async def test_unsubscribe_deactivates(self):
        handler = AsyncMock()
        sub = await self.pubsub.subscribe("my-topic", handler)
        assert sub.is_active is True
        await sub.unsubscribe()
        assert sub.is_active is False

    async def test_subscribe_multiple_topics(self):
        sub1 = await self.pubsub.subscribe("topic-a", AsyncMock())
        sub2 = await self.pubsub.subscribe("topic-b", AsyncMock())
        assert sub1.topic == "topic-a"
        assert sub2.topic == "topic-b"
        await sub1.unsubscribe()
        assert sub1.is_active is False
        assert sub2.is_active is True


# ---------------------------------------------------------------------------
# is_dapr_component_registered
# ---------------------------------------------------------------------------


class TestIsDaprComponentRegistered:
    async def test_returns_true_for_registered_component(self):
        from unittest.mock import patch

        from application_sdk.infrastructure._dapr.client import (
            is_dapr_component_registered,
        )

        mock_client = AsyncMock()
        mock_client.get_metadata.return_value = {
            "components": [
                {"name": "statestore", "type": "state.in-memory"},
                {"name": "pubsub", "type": "pubsub.in-memory"},
            ]
        }
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "application_sdk.infrastructure._dapr.client.AsyncDaprClient",
            return_value=mock_client,
        ):
            assert await is_dapr_component_registered("statestore") is True

    async def test_returns_false_for_unknown_component(self):
        from unittest.mock import patch

        from application_sdk.infrastructure._dapr.client import (
            is_dapr_component_registered,
        )

        mock_client = AsyncMock()
        mock_client.get_metadata.return_value = {
            "components": [{"name": "statestore", "type": "state.in-memory"}]
        }
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "application_sdk.infrastructure._dapr.client.AsyncDaprClient",
            return_value=mock_client,
        ):
            assert await is_dapr_component_registered("nonexistent") is False

    async def test_returns_false_on_metadata_failure(self):
        from unittest.mock import patch

        from application_sdk.infrastructure._dapr.client import (
            is_dapr_component_registered,
        )

        mock_client = AsyncMock()
        mock_client.get_metadata.side_effect = RuntimeError("connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "application_sdk.infrastructure._dapr.client.AsyncDaprClient",
            return_value=mock_client,
        ):
            assert await is_dapr_component_registered("statestore") is False
