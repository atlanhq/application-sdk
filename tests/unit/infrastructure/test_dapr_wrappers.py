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
    SecretStoreAccessDeniedError,
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

    async def test_5xx_with_get_error_code_is_not_found(self):
        """A bare 5xx is NOT enough to infer "cold sidecar": verified against a
        live Dapr sidecar, a *genuinely missing* secret key also returns 500
        with errorCode=ERR_SECRET_GET — identical to a still-cold component.
        Treating this as retryable would retry a permanently-missing secret
        for the full cold-start budget and then misreport it as a platform
        outage instead of not-found.

        Classified as SecretNotFoundError specifically (not a generic
        SecretStoreError) so every existing `except SecretNotFoundError`
        call site in credentials/agent.py, credentials/resolver.py, and
        infrastructure/_dapr/credential_vault.py — all written against a
        backend-opaque "absent secret" contract, none of which know about
        Dapr error codes — treats a genuinely-missing key as "absent"
        without needing their own copy of this classification. Regression
        coverage for a bug where those call sites' `except SecretNotFoundError`
        went dead once this case stopped raising that type, breaking their
        documented not-found contract for the common "no secret bundle"
        case (see the AGENT-mode multi-key and single-key tests in
        tests/unit/infrastructure/test_credential_vault.py and
        tests/unit/credentials/test_agent.py)."""
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
        with pytest.raises(SecretNotFoundError) as exc:
            await self.store.get("x")
        assert not isinstance(exc.value, SecretStoreUnavailableError)
        assert exc.value.effective_retryable is False

    def _dapr_error(self, status: int, code: str, message: str) -> None:
        req = httpx.Request("GET", "http://localhost/secret")
        resp = httpx.Response(
            status, request=req, json={"errorCode": code, "message": message}
        )
        self.client.get_secret.side_effect = httpx.HTTPStatusError(
            "dapr error", request=req, response=resp
        )

    @pytest.mark.parametrize(
        "backend_error",
        [
            # secretstores.local.file
            "secret db/creds not found",
            # Kubernetes
            'secrets "db/creds" not found',
            # AWS Secrets Manager (aws-sdk-go-v2 wording)
            "couldn't get secret: operation error Secrets Manager: "
            "GetSecretValue, https response error StatusCode: 400, RequestID: "
            "00000000-0000-0000-0000-000000000000, ResourceNotFoundException: "
            "Secrets Manager can't find the specified secret.",
            # Azure Key Vault
            "SecretNotFound: A secret with (name/id) example was not found",
            # GCP Secret Manager
            "failed to access secret version: rpc error: code = NotFound desc "
            "= Secret [projects/123/secrets/db/creds] not found or has no versions.",
            # HashiCorp Vault
            "getSecret db/creds failed secret key or version not exist",
        ],
    )
    async def test_err_secret_get_with_not_found_shape_is_not_found(
        self, backend_error: str
    ):
        """FND-4077: ``ERR_SECRET_GET`` is "absent" only with a not-found message."""
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key db/creds from secret store "
            f"secretstore: {backend_error}",
        )
        with pytest.raises(SecretNotFoundError) as exc:
            await self.store.get("db/creds")
        assert exc.value.effective_retryable is False

    @pytest.mark.parametrize(
        "backend_error",
        [
            pytest.param(
                "couldn't get secret: operation error Secrets Manager: "
                "GetSecretValue, https response error StatusCode: 400, "
                "RequestID: 00000000-0000-0000-0000-000000000000, api error "
                "ThrottlingException: Rate exceeded",
                id="throttled",
            ),
            pytest.param(
                "couldn't get secret: operation error Secrets Manager: "
                "GetSecretValue, https response error StatusCode: 400, "
                "RequestID: 00000000-0000-0000-0000-000000000000, api error "
                "AccessDeniedException: User: arn:aws:sts::000000000000:"
                "assumed-role/example-role/example is not authorized to "
                "perform: secretsmanager:GetSecretValue on resource: db/creds",
                id="access-denied",
            ),
            pytest.param(
                "couldn't get secret: operation error Secrets Manager: "
                "GetSecretValue, https response error StatusCode: 400, "
                "RequestID: 00000000-0000-0000-0000-000000000000, api error "
                "DecryptionFailure: Access to KMS is not allowed",
                id="kms",
            ),
            pytest.param(
                "couldn't get secret: operation error Secrets Manager: "
                "GetSecretValue, KMS key not found",
                id="other-resource-not-found",
            ),
        ],
    )
    async def test_err_secret_get_store_failure_is_retryable_store_error(
        self, backend_error: str
    ):
        """FND-4077: a throttled / denied / KMS ``ERR_SECRET_GET`` is the store
        failing, not the key being absent — a retryable ``SecretStoreError``
        carrying the cause, never ``SecretNotFoundError``."""
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key db/creds from secret store "
            f"secretstore: {backend_error}",
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("db/creds")
        assert not isinstance(exc.value, SecretNotFoundError)
        assert not isinstance(exc.value, SecretStoreUnavailableError)
        assert exc.value.effective_retryable is True
        assert isinstance(exc.value.cause, httpx.HTTPStatusError)
        # The backend's own wording reaches the message for diagnosis.
        assert backend_error.split(": ")[-1] in exc.value.message

    @pytest.mark.parametrize(
        "name,reason",
        [
            ("arn:aws:iam::123456789012:role/example-role", "may not contain '/'"),
            ("example%name", "may not contain '%'"),
        ],
    )
    async def test_k8s_invalid_resource_name_is_not_found(self, name: str, reason: str):
        """A name the store rejects as invalid cannot exist there: a literal,
        not a ref, so it classifies as not-found."""
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            f"failed getting secret with key {name} from secret store "
            f'glue-secretstore: invalid resource name "{name}": [{reason}]',
        )
        with pytest.raises(SecretNotFoundError):
            await self.store.get(name)
        assert await self.store.get_optional(name) is None

    @pytest.mark.parametrize(
        "backend_error",
        [
            pytest.param("ThrottlingException: Rate exceeded", id="throttled"),
            pytest.param("invalid credentials", id="invalid-credentials"),
            pytest.param('invalid resource name "db/creds"', id="no-may-not-reason"),
        ],
    )
    async def test_invalid_wording_without_name_reason_stays_store_failure(
        self, backend_error: str
    ):
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key db/creds from secret store "
            f"secretstore: {backend_error}",
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("db/creds")
        assert not isinstance(exc.value, SecretNotFoundError)

    async def test_aws_access_denied_is_access_denied_subtype(self):
        """An AWS ``AccessDeniedException`` is the store denying the read:
        the distinct subtype, still retryable (retry semantics unchanged)."""
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key db/creds from secret store "
            "secretstore: couldn't get secret: operation error Secrets Manager: "
            "GetSecretValue, https response error StatusCode: 400, RequestID: "
            "00000000-0000-0000-0000-000000000000, api error "
            "AccessDeniedException: User: arn:aws:sts::123456789012:"
            "assumed-role/example-sdr-role/i-0123456789abcdef0 is not "
            "authorized to perform: secretsmanager:GetSecretValue on resource: "
            "db/creds because no identity-based policy allows the "
            "secretsmanager:GetSecretValue action",
        )
        with pytest.raises(SecretStoreAccessDeniedError) as exc:
            await self.store.get("db/creds")
        assert exc.value.effective_retryable is True
        assert isinstance(exc.value.cause, httpx.HTTPStatusError)

    async def test_throttling_is_not_access_denied_subtype(self):
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key db/creds from secret store "
            "secretstore: couldn't get secret: api error ThrottlingException: "
            "Rate exceeded",
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("db/creds")
        assert not isinstance(exc.value, SecretStoreAccessDeniedError)

    async def test_not_found_is_not_access_denied_subtype(self):
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key db/creds from secret store "
            "secretstore: couldn't get secret: ResourceNotFoundException: "
            "Secrets Manager can't find the specified secret.",
        )
        with pytest.raises(SecretNotFoundError) as exc:
            await self.store.get("db/creds")
        assert not isinstance(exc.value, SecretStoreAccessDeniedError)

    async def test_dapr_wrapper_words_never_count_as_absence(self):
        """Only the backend's own error is matched: Dapr's
        ``failed getting secret with key … from secret store …:`` wrapper says
        "secret" too, and must not combine with a later "not found" about
        another resource."""
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key db/creds from secret store "
            "secretstore: KMS key not found",
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("db/creds")
        assert not isinstance(exc.value, SecretNotFoundError)

    async def test_key_containing_not_found_does_not_mask_a_store_failure(self):
        """The key name is removed before matching, so a key that itself says
        "not found" can't make a throttled lookup read as an absence."""
        self._dapr_error(
            500,
            "ERR_SECRET_GET",
            "failed getting secret with key not found from secret store "
            "secretstore: couldn't get secret: ThrottlingException: Rate exceeded",
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("not found")
        assert not isinstance(exc.value, SecretNotFoundError)

    async def test_403_permission_denied_is_non_retryable_rejection(self):
        """Dapr's secret-scoping denial is the store refusing the request, not
        the key being absent: a definitive, non-retryable rejection. Collapsing
        it to not-found would let single-key resolution skip the field and send
        the ref-key as its value."""
        self._dapr_error(
            403,
            "ERR_PERMISSION_DENIED",
            'access denied by policy to get "svc_reader" from "secretstore"',
        )
        with pytest.raises(SecretStoreError) as exc:
            await self.store.get("svc_reader")
        assert not isinstance(exc.value, SecretNotFoundError)
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

    async def test_invoke_error_surfaces_dapr_error_body(self):
        """A Dapr 500 carries the real cause only in its JSON body; the
        BindingError message must carry it too, since the sidecar logs it
        only at debug level."""
        req = httpx.Request("POST", "http://localhost:3500/v1.0/bindings/eventstore")
        resp = httpx.Response(
            500,
            request=req,
            json={
                "errorCode": "ERR_INVOKE_OUTPUT_BINDING",
                "message": "error invoking output binding eventstore: "
                "received status code 403",
            },
        )
        self.client.invoke_binding.side_effect = httpx.HTTPStatusError(
            "internal error", request=req, response=resp
        )
        with pytest.raises(BindingError) as exc:
            await self.binding.invoke("create")
        text = str(exc.value)
        assert "ERR_INVOKE_OUTPUT_BINDING" in text
        assert "received status code 403" in text
        assert exc.value.binding_name == "s3"
        assert exc.value.operation == "create"
        assert isinstance(exc.value.cause, httpx.HTTPStatusError)

    async def test_invoke_error_surfaces_non_json_body_truncated(self):
        """A proxy in front of the sidecar may answer with HTML; keep a bounded
        slice of it rather than dropping it."""
        req = httpx.Request("POST", "http://localhost:3500/v1.0/bindings/eventstore")
        resp = httpx.Response(502, request=req, text="<html>" + "x" * 2000)
        self.client.invoke_binding.side_effect = httpx.HTTPStatusError(
            "bad gateway", request=req, response=resp
        )
        with pytest.raises(BindingError) as exc:
            await self.binding.invoke("create")
        text = str(exc.value)
        assert "<html>" in text
        assert "x" * 600 not in text

    async def test_invoke_error_without_body_keeps_plain_message(self):
        req = httpx.Request("POST", "http://localhost:3500/v1.0/bindings/eventstore")
        resp = httpx.Response(500, request=req)
        self.client.invoke_binding.side_effect = httpx.HTTPStatusError(
            "internal error", request=req, response=resp
        )
        with pytest.raises(BindingError) as exc:
            await self.binding.invoke("create")
        assert str(exc.value).count("Failed to invoke binding") == 1
        assert "dapr errorCode" not in str(exc.value)


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
