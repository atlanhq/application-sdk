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
    get_dapr_component_types,
    retry_past_dapr_cold_start,
    wait_for_dapr_sidecar,
)
from application_sdk.infrastructure.secrets import (
    SecretNotFoundError,
    SecretStoreUnavailableError,
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
    async def test_save_state(self, mock_client):
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=204, raise_for_status=MagicMock()
        )

        await client.save_state("mystore", "key1", {"value": 1})

        mock_http.post.assert_called_once()
        args = mock_http.post.call_args
        assert "/v1.0/state/mystore" in args[0][0]
        assert args[1]["json"] == [{"key": "key1", "value": {"value": 1}}]

    async def test_get_state_returns_data(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.return_value = MagicMock(
            status_code=200,
            content=b'{"value": 1}',
            raise_for_status=MagicMock(),
        )

        result = await client.get_state("mystore", "key1")

        assert result == b'{"value": 1}'

    async def test_get_state_returns_none_for_missing(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.return_value = MagicMock(
            status_code=204,
            content=b"",
            raise_for_status=MagicMock(),
        )

        result = await client.get_state("mystore", "missing")

        assert result is None

    async def test_delete_state(self, mock_client):
        client, mock_http = mock_client
        mock_http.delete.return_value = MagicMock(
            status_code=204, raise_for_status=MagicMock()
        )

        await client.delete_state("mystore", "key1")

        mock_http.delete.assert_called_once()


class TestAsyncDaprClientSecrets:
    async def test_get_secret(self, mock_client):
        client, mock_http = mock_client
        mock_http.get.return_value = MagicMock(
            status_code=200,
            json=MagicMock(return_value={"api_key": "secret123"}),
            raise_for_status=MagicMock(),
        )

        result = await client.get_secret("secretstore", "api_key")

        assert result == {"api_key": "secret123"}

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
    async def test_publish_event(self, mock_client):
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=204, raise_for_status=MagicMock()
        )

        await client.publish_event("pubsub", "my-topic", '{"msg": "hello"}')

        mock_http.post.assert_called_once()
        args = mock_http.post.call_args
        assert "/v1.0/publish/pubsub/my-topic" in args[0][0]

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

    async def test_invoke_binding_json_data_not_double_encoded(self, mock_client):
        """JSON bytes must be embedded as a parsed object, not a string.

        Regression test: prior to the fix, ``data.decode()`` produced a
        string which ``json=body`` then double-encoded, causing Dapr HTTP
        bindings to forward a JSON string instead of an object (422).
        """
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=200,
            content=b"",
            headers={},
            raise_for_status=MagicMock(),
        )

        json_payload = b'{"event_name": "worker_start", "nested": {"key": 1}}'
        await client.invoke_binding("eventstore", "create", data=json_payload)

        body = mock_http.post.call_args[1]["json"]
        # data must be a dict, NOT a string
        assert isinstance(body["data"], dict)
        assert body["data"]["event_name"] == "worker_start"
        assert body["data"]["nested"] == {"key": 1}

    async def test_invoke_binding_json_array_parsed_as_list(self, mock_client):
        """JSON arrays must be embedded as a parsed list, not a string."""
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=200, content=b"", headers={}, raise_for_status=MagicMock()
        )

        await client.invoke_binding("my-binding", "create", data=b"[1, 2, 3]")

        body = mock_http.post.call_args[1]["json"]
        assert isinstance(body["data"], list)
        assert body["data"] == [1, 2, 3]

    async def test_invoke_binding_json_primitive_sent_as_string(self, mock_client):
        """JSON primitives (numbers, booleans, null) stay as decoded strings."""
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=200, content=b"", headers={}, raise_for_status=MagicMock()
        )

        await client.invoke_binding("my-binding", "create", data=b"42")

        body = mock_http.post.call_args[1]["json"]
        assert body["data"] == "42"

    async def test_invoke_binding_non_json_data_falls_back_to_string(self, mock_client):
        """Non-JSON bytes should still be sent as a decoded string."""
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=200, content=b"", headers={}, raise_for_status=MagicMock()
        )

        await client.invoke_binding("my-binding", "create", data=b"plain text")

        body = mock_http.post.call_args[1]["json"]
        assert body["data"] == "plain text"

    async def test_invoke_binding_empty_bytes_omits_data_key(self, mock_client):
        """Empty bytes (falsy) should not add a data key to the body."""
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=200, content=b"", headers={}, raise_for_status=MagicMock()
        )

        await client.invoke_binding("my-binding", "create", data=b"")

        body = mock_http.post.call_args[1]["json"]
        assert "data" not in body

    async def test_invoke_binding_invalid_utf8_falls_back_to_replacement(
        self, mock_client
    ):
        """Invalid UTF-8 bytes should decode with replacement characters."""
        client, mock_http = mock_client
        mock_http.post.return_value = MagicMock(
            status_code=200, content=b"", headers={}, raise_for_status=MagicMock()
        )

        await client.invoke_binding("my-binding", "create", data=b"\xff\xfe")

        body = mock_http.post.call_args[1]["json"]
        assert isinstance(body["data"], str)
        assert "\ufffd" in body["data"]  # replacement character


class TestAsyncDaprClientMetadata:
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

    def test_retry_covers_connection_errors_with_widened_budget(self):
        """Connection-level failures (not just HTTP 5xx) must be retried, over a
        window wide enough to bridge a cold-starting sidecar.

        A Dapr call issued while daprd isn't yet accepting connections raises
        ConnectError; if that isn't retried (or the budget is ~1.5s) the call
        fails with "All connection attempts failed" instead of riding out the
        startup. Guards against a regression to 5xx-only / tiny-budget retries.
        """
        import httpx

        from application_sdk.infrastructure._dapr.http import _DEFAULT_RETRY_TOTAL

        retry = AsyncDaprClient(
            base_url="http://localhost:3500"
        )._client._transport.retry
        # Read errors on GET (the SDR secret-fetch codepath)...
        assert retry.is_retryable_exception(httpx.ConnectError("x")) is True
        assert retry.is_retryable_exception(httpx.ReadError("x")) is True
        # ...AND write/close errors on POST/DELETE, so the explicit list stays at
        # parity with httpx-retries' prior implicit default (regression guard for
        # a narrower leaf-class list that would drop these).
        assert retry.is_retryable_exception(httpx.WriteError("x")) is True
        assert retry.is_retryable_exception(httpx.WriteTimeout("x")) is True
        assert retry.is_retryable_exception(httpx.CloseError("x")) is True
        assert _DEFAULT_RETRY_TOTAL >= 5
        assert retry.backoff_factor >= 1.0


class TestSidecarWaitTimeout:
    """The startup readiness gate must wait long enough for a cold-starting
    sidecar (the old 10s let CI proceed before daprd was up)."""

    def test_default_timeout_raised_and_env_configurable(self, monkeypatch):
        import importlib

        import application_sdk.infrastructure._dapr.http as http_mod

        # Default is generous enough for a CI cold start.
        assert http_mod._DEFAULT_SIDECAR_WAIT_TIMEOUT >= 60.0

        # ...and overridable via env. Reload under the patched env, then restore
        # so the module globals don't leak into other tests.
        monkeypatch.setenv("ATLAN_DAPR_SIDECAR_WAIT_TIMEOUT", "123")
        try:
            reloaded = importlib.reload(http_mod)
            assert reloaded._DEFAULT_SIDECAR_WAIT_TIMEOUT == 123.0
        finally:
            monkeypatch.delenv("ATLAN_DAPR_SIDECAR_WAIT_TIMEOUT", raising=False)
            importlib.reload(http_mod)


class TestWaitForDaprSidecar:
    # The implementation early-returns when DEPLOYMENT_NAME == LOCAL_ENVIRONMENT
    # (skips polling in local dev). Override to "deployed" so the poll loop runs.
    _DEPLOYED = patch(
        "application_sdk.infrastructure._dapr.http.DEPLOYMENT_NAME", "deployed"
    )

    async def test_ready_immediately(self):
        """Returns as soon as sidecar responds 204 on first poll."""
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_get = AsyncMock(return_value=mock_response)
        with (
            self._DEPLOYED,
            patch(
                "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
            ) as mock_cls,
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=mock_get)
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await wait_for_dapr_sidecar(timeout=5.0, interval=0.01)
        mock_get.assert_called_once()

    async def test_ready_after_connection_errors(self):
        """Polls through connection errors (daprd still cold-booting) and
        returns once daprd finally answers 204."""
        import httpx as _httpx

        ready = MagicMock()
        ready.status_code = 204
        mock_get = AsyncMock(
            side_effect=[
                _httpx.ConnectError("refused"),
                _httpx.ConnectError("refused"),
                ready,
            ]
        )
        with (
            self._DEPLOYED,
            patch(
                "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
            ) as mock_cls,
            patch("application_sdk.infrastructure._dapr.http.logger"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=mock_get)
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await wait_for_dapr_sidecar(timeout=5.0, interval=0.01)
        assert mock_get.call_count == 3

    async def test_returns_on_first_non_204(self):
        """Returns on the first poll once daprd's HTTP API answers at all —
        a non-204 (components still initializing) must NOT keep it waiting,
        since per-component readiness is the per-call retry budget's job."""
        not_ready = MagicMock()
        not_ready.status_code = 503
        mock_get = AsyncMock(return_value=not_ready)
        with (
            self._DEPLOYED,
            patch(
                "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
            ) as mock_cls,
            patch("application_sdk.infrastructure._dapr.http.logger") as mock_logger,
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=mock_get)
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await wait_for_dapr_sidecar(timeout=5.0, interval=0.01)
        mock_get.assert_called_once()
        # The "proceeding without full-component wait" decision is INFO (not
        # DEBUG) so it survives the prod INFO floor, and it carries the status
        # code — not WARNING, since a reachable-but-not-204 sidecar is an
        # expected steady state, not an anomaly.
        mock_logger.info.assert_called_once()
        assert 503 in mock_logger.info.call_args[0]
        mock_logger.warning.assert_not_called()

    async def test_timeout_logs_warning(self):
        """Logs a warning and returns when daprd never accepts connections
        (only the connection-error path can reach the deadline now)."""
        import httpx as _httpx

        mock_get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
        with (
            self._DEPLOYED,
            patch(
                "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
            ) as mock_cls,
            patch("application_sdk.infrastructure._dapr.http.logger") as mock_logger,
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=mock_get)
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await wait_for_dapr_sidecar(timeout=0.05, interval=0.01)
        mock_logger.warning.assert_called_once()
        assert "not reachable" in mock_logger.warning.call_args[0][0]
        # The terminal give-up must carry the last connection error so a
        # genuinely unreachable/misconfigured sidecar is diagnosable.
        assert isinstance(
            mock_logger.warning.call_args.kwargs.get("exc_info"), _httpx.ConnectError
        )

    async def test_connection_error_does_not_crash(self):
        """Poll loop survives connection errors and eventually times out cleanly."""
        import httpx as _httpx

        mock_get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
        with (
            self._DEPLOYED,
            patch(
                "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
            ) as mock_cls,
            patch("application_sdk.infrastructure._dapr.http.logger"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=mock_get)
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await wait_for_dapr_sidecar(timeout=0.05, interval=0.01)

    async def test_ready_arms_shared_cold_start_gate(self, monkeypatch):
        """A successful health probe arms the same gate
        retry_past_dapr_cold_start consults — a worker that already confirmed
        readiness at startup shouldn't pay a second, uninformed wait on its
        first real Dapr call."""
        monkeypatch.setattr(
            "application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready",
            False,
        )
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_get = AsyncMock(return_value=mock_response)
        with (
            self._DEPLOYED,
            patch(
                "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
            ) as mock_cls,
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=mock_get)
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await wait_for_dapr_sidecar(timeout=5.0, interval=0.01)

        import application_sdk.infrastructure._dapr.http as http_mod

        assert http_mod._dapr_sidecar_confirmed_ready is True

    async def test_timeout_does_not_arm_shared_cold_start_gate(self, monkeypatch):
        """Giving up on the health probe (daprd never reachable) must NOT arm
        the shared gate — a later real Dapr call still needs its own retry
        budget in case the sidecar needed a little longer than the probe
        waited."""
        import httpx as _httpx

        monkeypatch.setattr(
            "application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready",
            False,
        )
        mock_get = AsyncMock(side_effect=_httpx.ConnectError("refused"))
        with (
            self._DEPLOYED,
            patch(
                "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
            ) as mock_cls,
            patch("application_sdk.infrastructure._dapr.http.logger"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=mock_get)
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await wait_for_dapr_sidecar(timeout=0.05, interval=0.01)

        import application_sdk.infrastructure._dapr.http as http_mod

        assert http_mod._dapr_sidecar_confirmed_ready is False

    async def test_non_204_does_not_arm_shared_cold_start_gate(self, monkeypatch):
        """Returning early on a reachable-but-not-204 sidecar must NOT arm the
        gate — components may still be initializing, so the first real Dapr
        call still needs its own retry budget."""
        monkeypatch.setattr(
            "application_sdk.infrastructure._dapr.http._dapr_sidecar_confirmed_ready",
            False,
        )
        not_ready = MagicMock()
        not_ready.status_code = 503
        mock_get = AsyncMock(return_value=not_ready)
        with (
            self._DEPLOYED,
            patch(
                "application_sdk.infrastructure._dapr.http.httpx.AsyncClient"
            ) as mock_cls,
            patch("application_sdk.infrastructure._dapr.http.logger"),
        ):
            mock_cls.return_value.__aenter__ = AsyncMock(
                return_value=MagicMock(get=mock_get)
            )
            mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            await wait_for_dapr_sidecar(timeout=5.0, interval=0.01)

        import application_sdk.infrastructure._dapr.http as http_mod

        assert http_mod._dapr_sidecar_confirmed_ready is False


class TestRetryPastDaprColdStart:
    """Tests for retry_past_dapr_cold_start — the shared cold-start retry
    engine used by every Dapr-backed call site that opts in (secret fetch,
    credential-vault config fetch, named-credential resolver path)."""

    async def test_retries_transient_failure_then_succeeds(
        self, fast_dapr_cold_start_retry
    ) -> None:
        calls = {"n": 0}

        async def call() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise SecretStoreUnavailableError("p")
            return "value"

        result = await retry_past_dapr_cold_start(
            call, description="test call", component="test-component"
        )

        assert result == "value"
        assert calls["n"] == 3

    async def test_non_transient_exception_arms_gate_and_fails_fast(
        self, monkeypatch
    ) -> None:
        monkeypatch.setattr(
            "application_sdk.infrastructure._dapr.http.DAPR_COLD_START_MAX_WAIT_SECONDS",
            30.0,
        )
        calls = {"n": 0}

        async def call() -> str:
            calls["n"] += 1
            raise SecretNotFoundError("p")

        with pytest.raises(SecretNotFoundError):
            await retry_past_dapr_cold_start(
                call, description="test call", component="test-component"
            )
        assert calls["n"] == 1

        # The gate is now armed for this component — a later transient
        # failure on the SAME component is not retried.
        async def transient_call() -> str:
            calls["n"] += 1
            raise SecretStoreUnavailableError("p")

        with pytest.raises(SecretStoreUnavailableError):
            await retry_past_dapr_cold_start(
                transient_call, description="test call", component="test-component"
            )
        assert calls["n"] == 2

    async def test_different_component_does_not_share_gate(
        self, fast_dapr_cold_start_retry
    ) -> None:
        """A definitive answer on one component must not arm the gate for a
        different component — the cross-component leak fixed alongside the
        introduction of ``component``."""
        calls = {"n": 0}

        async def call() -> str:
            calls["n"] += 1
            raise SecretNotFoundError("p")

        with pytest.raises(SecretNotFoundError):
            await retry_past_dapr_cold_start(
                call, description="test call", component="component-a"
            )
        assert calls["n"] == 1

        # A different component still gets a full retry budget.
        async def transient_call() -> str:
            calls["n"] += 1
            if calls["n"] < 3:
                raise SecretStoreUnavailableError("p")
            return "value"

        result = await retry_past_dapr_cold_start(
            transient_call,
            description="test call",
            component="component-b",
        )
        assert result == "value"
        assert calls["n"] == 3

    async def test_gives_up_at_deadline(
        self, deterministic_dapr_cold_start_deadline
    ) -> None:
        calls = {"n": 0}

        async def call() -> str:
            calls["n"] += 1
            raise SecretStoreUnavailableError("p")

        with pytest.raises(SecretStoreUnavailableError):
            await retry_past_dapr_cold_start(
                call, description="test call", component="test-component"
            )
        assert calls["n"] == 2


class TestGetDaprComponentTypes:
    """Tests for get_dapr_component_types() — the worker_start binding-type reader."""

    @staticmethod
    def _patch_metadata(meta):
        """Patch AsyncDaprClient so `async with` yields a client returning `meta`."""
        fake = AsyncMock()
        fake.get_metadata = AsyncMock(return_value=meta)
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=fake)
        cm.__aexit__ = AsyncMock(return_value=None)
        return patch(
            "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
            return_value=cm,
        )

    async def test_projects_components_to_types(self):
        meta = {
            "components": [
                {"name": "objectstore", "type": "bindings.aws.s3"},
                {"name": "secretstore", "type": "secretstores.kubernetes"},
            ]
        }
        with self._patch_metadata(meta):
            result = await get_dapr_component_types()
        assert result == {
            "objectstore": "bindings.aws.s3",
            "secretstore": "secretstores.kubernetes",
        }

    async def test_reads_registered_components_key(self):
        # Older Dapr sidecars emit "registeredComponents" rather than "components".
        meta = {
            "registeredComponents": [
                {"name": "objectstore", "type": "bindings.localstorage"},
            ]
        }
        with self._patch_metadata(meta):
            result = await get_dapr_component_types()
        assert result == {"objectstore": "bindings.localstorage"}

    async def test_skips_unnamed_components(self):
        meta = {"components": [{"type": "bindings.aws.s3"}]}
        with self._patch_metadata(meta):
            result = await get_dapr_component_types()
        assert result == {}

    async def test_returns_empty_on_sidecar_error(self):
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(side_effect=RuntimeError("sidecar unreachable"))
        cm.__aexit__ = AsyncMock(return_value=None)
        with patch(
            "application_sdk.infrastructure._dapr.http.AsyncDaprClient",
            return_value=cm,
        ):
            result = await get_dapr_component_types()
        assert result == {}
