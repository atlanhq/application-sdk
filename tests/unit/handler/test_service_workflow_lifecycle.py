"""Handler service tests: the Temporal client, stop, status, result and upload endpoints."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from application_sdk.handler.service import (
    create_app_handler_service,
)
from tests.unit.handler._service_support import (
    _AlphaInput,
    _AlphaOutput,
    _BetaInput,
    _BetaOutput,
    _make_client,
    _RoutingInput,
    _RoutingOutput,
    _TestHandler,
)

# ---------------------------------------------------------------------------
# BLDX-1129: tests targeting the bug class — runtime-only inline imports,
# swallowed exceptions, contract drift between handler service and its
# injected dependencies (Temporal, object store, etc.).
# ---------------------------------------------------------------------------


@pytest.fixture
def reset_temporal_client_singleton():
    """Reset the module-level _temporal_client cache before and after a test.

    _get_temporal_client() caches a Temporal client globally; tests that
    exercise its real body (rather than patching it out) must reset this
    or they will leak state into other tests.
    """
    from application_sdk.handler import service as svc_module

    svc_module._temporal_client = None
    svc_module._handler_auth_manager = None
    yield
    svc_module._temporal_client = None
    svc_module._handler_auth_manager = None


class TestGetTemporalClientInlineImports:
    """BLDX-1129 regression: exercise the real _get_temporal_client body so a
    rename of create_temporal_client / TemporalAuthConfig / TemporalAuthManager
    in application_sdk.execution surfaces here, not in production.
    """

    async def test_no_auth_uses_create_temporal_client(
        self, reset_temporal_client_singleton
    ) -> None:
        from unittest.mock import AsyncMock

        from application_sdk.handler import service as svc_module

        svc_module._workflow_config = svc_module.WorkflowClientConfig(
            host="temporal:7233",
            namespace="default",
            auth_enabled=False,
        )

        fake_client = object()
        with patch(
            "application_sdk.execution.create_temporal_client",
            new=AsyncMock(return_value=fake_client),
        ) as mock_create:
            result = await svc_module._get_temporal_client()

        assert result is fake_client
        # Must call with the configured host, namespace, and api_key=None when no auth
        kwargs = mock_create.call_args.kwargs
        assert mock_create.call_args.args[0] == "temporal:7233"
        assert mock_create.call_args.args[1] == "default"
        assert kwargs["api_key"] is None
        assert kwargs["tls_enabled"] is False
        assert kwargs["enable_prometheus"] is True
        assert kwargs["prometheus_bind_address"] == ""

    async def test_temporal_client_uses_metrics_runtime_config(
        self, reset_temporal_client_singleton
    ) -> None:
        from unittest.mock import AsyncMock

        from application_sdk.handler import service as svc_module

        svc_module._workflow_config = svc_module.WorkflowClientConfig(
            host="temporal:7233",
            namespace="default",
            enable_temporal_core_metrics=False,
            prometheus_bind_address="127.0.0.1:9999",
        )

        fake_client = object()
        with patch(
            "application_sdk.execution.create_temporal_client",
            new=AsyncMock(return_value=fake_client),
        ) as mock_create:
            result = await svc_module._get_temporal_client()

        assert result is fake_client
        kwargs = mock_create.call_args.kwargs
        assert kwargs["enable_prometheus"] is False
        assert kwargs["prometheus_bind_address"] == "127.0.0.1:9999"

    async def test_returns_cached_client_on_second_call(
        self, reset_temporal_client_singleton
    ) -> None:
        from unittest.mock import AsyncMock

        from application_sdk.handler import service as svc_module

        svc_module._workflow_config = svc_module.WorkflowClientConfig(
            host="temporal:7233", namespace="default"
        )
        sentinel = object()
        with patch(
            "application_sdk.execution.create_temporal_client",
            new=AsyncMock(return_value=sentinel),
        ) as mock_create:
            first = await svc_module._get_temporal_client()
            second = await svc_module._get_temporal_client()

        assert first is second is sentinel
        # Cached: only one underlying create
        assert mock_create.call_count == 1

    async def test_auth_enabled_uses_temporal_auth_manager(
        self, reset_temporal_client_singleton
    ) -> None:
        """When auth_enabled=True, the inline-imported TemporalAuthConfig and
        TemporalAuthManager must be wired up and the api_key threaded through.
        Catches BLDX-1129-class breakage if either symbol is renamed in
        application_sdk.execution.
        """
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler import service as svc_module

        svc_module._workflow_config = svc_module.WorkflowClientConfig(
            host="t:7233",
            namespace="default",
            auth_enabled=True,
            auth_client_id="cid",
            auth_client_secret="csec",
            auth_token_url="https://idp/token",
            auth_base_url="https://idp",
            auth_scopes="a,b",
        )

        fake_auth_manager = MagicMock()
        fake_auth_manager.acquire_initial_token = AsyncMock(return_value="api-key-xyz")
        fake_auth_manager.start_background_refresh = MagicMock()

        fake_client = object()
        with (
            patch(
                "application_sdk.execution.TemporalAuthManager",
                return_value=fake_auth_manager,
            ) as mock_mgr_cls,
            patch("application_sdk.execution.TemporalAuthConfig") as mock_cfg_cls,
            patch(
                "application_sdk.execution.create_temporal_client",
                new=AsyncMock(return_value=fake_client),
            ) as mock_create,
        ):
            result = await svc_module._get_temporal_client()

        assert result is fake_client
        mock_cfg_cls.assert_called_once()
        mock_mgr_cls.assert_called_once()
        # api_key from auth manager must be passed through to create_temporal_client
        assert mock_create.call_args.kwargs["api_key"] == "api-key-xyz"
        # Background token refresh must be started after client connect
        fake_auth_manager.start_background_refresh.assert_called_once_with(fake_client)


class TestGetWorkflowResultHelper:
    """Tests for _get_workflow_result coverage (lines 198-215)."""

    async def test_typed_dict_result_returned_as_dict(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.service import _get_workflow_result

        mock_handle = MagicMock()
        mock_handle.result = AsyncMock(return_value={"foo": "bar"})
        mock_client = MagicMock()
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        result = await _get_workflow_result(
            mock_client, workflow_id="wf-1", output_type=None
        )
        assert result == {"foo": "bar"}

    async def test_pydantic_result_is_model_dumped(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from pydantic import BaseModel

        from application_sdk.handler.service import _get_workflow_result

        class _Out(BaseModel):
            x: int = 1
            y: str = "ok"

        mock_handle = MagicMock()
        mock_handle.result = AsyncMock(return_value=_Out(x=42, y="hello"))
        mock_client = MagicMock()
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        result = await _get_workflow_result(
            mock_client, workflow_id="wf-2", output_type=_Out
        )
        assert result == {"x": 42, "y": "hello"}

    async def test_unknown_result_type_returns_empty_dict(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.service import _get_workflow_result

        mock_handle = MagicMock()
        mock_handle.result = AsyncMock(return_value="not-a-dict-or-model")
        mock_client = MagicMock()
        mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

        result = await _get_workflow_result(
            mock_client, workflow_id="wf-3", output_type=None
        )
        assert result == {}

    async def test_typed_failure_falls_back_to_untyped(self) -> None:
        """If typed deserialization fails, fall back to untyped result.
        Verifies the swallowed-but-logged exception path still returns data.
        """
        from unittest.mock import AsyncMock, MagicMock

        from application_sdk.handler.service import _get_workflow_result

        # First call (typed) raises; second (untyped) succeeds
        first_handle = MagicMock()
        first_handle.result = AsyncMock(side_effect=RuntimeError("type mismatch"))
        second_handle = MagicMock()
        second_handle.result = AsyncMock(return_value={"recovered": True})

        mock_client = MagicMock()
        mock_client.get_workflow_handle = MagicMock(
            side_effect=[first_handle, second_handle]
        )

        result = await _get_workflow_result(
            mock_client, workflow_id="wf-4", output_type=int
        )
        assert result == {"recovered": True}
        # Two handles fetched: one typed, one untyped fallback
        assert mock_client.get_workflow_handle.call_count == 2


class TestStopWorkflowEndpoint:
    """Tests for POST /workflows/v1/stop/{workflow_id}/{run_id:path}."""

    def _make_routed_client(self):
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _StopApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        svc = create_app_handler_service(
            _TestHandler(),
            app_name="stop-test",
            app_class=_StopApp,
            temporal_host="temporal:7233",
        )
        return svc

    def test_stop_success_returns_200(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._make_routed_client()
            mock_handle = MagicMock()
            mock_handle.terminate = AsyncMock(return_value=None)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.post("/workflows/v1/stop/wf-1/run-1")
            assert response.status_code == 200
            body = response.json()
            assert body["success"] is True
            mock_handle.terminate.assert_awaited_once()
        finally:
            from application_sdk.app.registry import AppRegistry, TaskRegistry

            AppRegistry.reset()
            TaskRegistry.reset()

    def test_stop_not_found_returns_404(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._make_routed_client()
            mock_handle = MagicMock()
            import temporalio.service

            mock_handle.terminate = AsyncMock(
                side_effect=temporalio.service.RPCError(
                    "wording can change",
                    status=temporalio.service.RPCStatusCode.NOT_FOUND,
                    raw_grpc_status=None,
                )
            )
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.post("/workflows/v1/stop/wf-x/run-x")
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()
        finally:
            from application_sdk.app.registry import AppRegistry, TaskRegistry

            AppRegistry.reset()
            TaskRegistry.reset()

    def test_stop_generic_error_returns_500_without_leaking_details(self) -> None:
        """Generic Temporal errors → 500 with a generic message, no stack/internals."""
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._make_routed_client()
            mock_handle = MagicMock()
            mock_handle.terminate = AsyncMock(
                side_effect=RuntimeError("connection refused: 10.0.0.42:7233")
            )
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.post("/workflows/v1/stop/wf-1/run-1")
            assert response.status_code == 500
            body_str = str(response.json())
            assert "10.0.0.42" not in body_str
            assert "RuntimeError" not in body_str
        finally:
            from application_sdk.app.registry import AppRegistry, TaskRegistry

            AppRegistry.reset()
            TaskRegistry.reset()


class TestGetWorkflowStatusEndpoint:
    """Tests for GET /workflows/v1/status/{workflow_id}/{run_id:path}."""

    def _setup(self):
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _StatusApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        return create_app_handler_service(
            _TestHandler(),
            app_name="status-test",
            app_class=_StatusApp,
            temporal_host="temporal:7233",
        )

    def _teardown(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def test_status_running_returns_status_payload(self) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            now = datetime.now(UTC)
            desc = MagicMock()
            desc.status.name = "RUNNING"
            desc.execution_time = now - timedelta(seconds=10)
            desc.close_time = None
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/status/wf-1/run-1")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "RUNNING"
            assert data["execution_duration_seconds"] >= 10
        finally:
            self._teardown()

    def test_status_completed_uses_close_time_for_duration(self) -> None:
        from datetime import UTC, datetime, timedelta
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            start = datetime(2024, 1, 1, tzinfo=UTC)
            desc = MagicMock()
            desc.status.name = "COMPLETED"
            desc.execution_time = start
            desc.close_time = start + timedelta(seconds=42)
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/status/wf-1/run-1")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["execution_duration_seconds"] == 42
        finally:
            self._teardown()

    def test_status_unknown_when_status_falsy(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status = None
            desc.execution_time = None
            desc.close_time = None
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/status/wf-1/run-1")
            assert response.status_code == 200
            assert response.json()["data"]["status"] == "UNKNOWN"
            assert response.json()["data"]["execution_duration_seconds"] == 0
        finally:
            self._teardown()

    def test_status_not_found_returns_404(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        import temporalio.service

        try:
            svc = self._setup()
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(
                side_effect=temporalio.service.RPCError(
                    "wording can change",
                    status=temporalio.service.RPCStatusCode.NOT_FOUND,
                    raw_grpc_status=None,
                )
            )
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.get("/workflows/v1/status/wf-x/run-x")
            assert response.status_code == 404
        finally:
            self._teardown()

    def test_status_generic_error_returns_500(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(side_effect=RuntimeError("boom"))
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.get("/workflows/v1/status/wf-1/run-1")
            assert response.status_code == 500
            assert "boom" not in str(response.json())
        finally:
            self._teardown()


class TestGetResultEndpoint:
    """Tests for GET /workflows/v1/result/{workflow_id}."""

    def _setup(self):
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _ResApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        return create_app_handler_service(
            _TestHandler(),
            app_name="res-test",
            app_class=_ResApp,
            temporal_host="temporal:7233",
        )

    def _teardown(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def test_result_running_returns_running_status(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status.name = "RUNNING"
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-r")
            assert response.status_code == 200
            assert response.json()["data"]["status"] == "running"
        finally:
            self._teardown()

    def test_result_completed_returns_result_data(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status.name = "COMPLETED"
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    new=AsyncMock(return_value={"a": 1}),
                ),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-c")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "completed"
            assert data["result"] == {"a": 1}
        finally:
            self._teardown()

    @pytest.mark.parametrize(
        "temporal_status", ["FAILED", "TERMINATED", "CANCELED", "TIMED_OUT"]
    )
    def test_result_failed_states_return_failed_status(
        self, temporal_status: str
    ) -> None:
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status.name = temporal_status
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-f")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "failed"
            assert temporal_status.lower() in data["error"].lower()
        finally:
            self._teardown()

    def test_result_unknown_status_returns_lowercased(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.status.name = "EXOTIC_STATE"
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-other")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "exotic_state"
        finally:
            self._teardown()

    def test_result_wait_true_returns_completed(self) -> None:
        """With ?wait=true, retrieve the workflow result directly."""
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.workflow_type = "res-test"  # single implicit entrypoint
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    new=AsyncMock(return_value={"final": "value"}),
                ),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-wait?wait=true")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["status"] == "completed"
            assert data["result"] == {"final": "value"}
        finally:
            self._teardown()

    def test_result_wait_failure_returns_failed_envelope(self) -> None:
        """A non-WorkflowFailureError raised during result decoding produces
        the new ``result_decode_failed`` status (PR #1603 / BLDX-1173) —
        distinct from a real workflow failure. Both still set
        ``success=false`` and must not leak the underlying exception text.
        """
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()
            desc = MagicMock()
            desc.workflow_type = "res-test"
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    new=AsyncMock(side_effect=RuntimeError("internal-error-xxx")),
                ),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-wait-fail?wait=true")
            assert response.status_code == 200
            body = response.json()
            data = body["data"]
            # Generic exception during decode → new distinct status.
            assert data["status"] == "result_decode_failed"
            assert body["success"] is False
            # Generic message — must not leak the raw exception text.
            assert "internal-error-xxx" not in str(body)
        finally:
            self._teardown()

    def test_result_not_found_returns_404(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        import temporalio.service

        try:
            svc = self._setup()
            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(
                side_effect=temporalio.service.RPCError(
                    "wording can change",
                    status=temporalio.service.RPCStatusCode.NOT_FOUND,
                    raw_grpc_status=None,
                )
            )
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)
            with patch(
                "application_sdk.handler.service._get_temporal_client",
                new=AsyncMock(return_value=mock_client),
            ):
                client = TestClient(svc, raise_server_exceptions=False)
                response = client.get("/workflows/v1/result/wf-nope")
            assert response.status_code == 404
        finally:
            self._teardown()

    def test_result_503_when_unconfigured(self) -> None:
        client = _make_client()
        response = client.get("/workflows/v1/result/wf-1")
        assert response.status_code == 503

    def test_status_503_when_unconfigured(self) -> None:
        client = _make_client()
        response = client.get("/workflows/v1/status/wf-1/run-1")
        assert response.status_code == 503


class TestGetResultMultiEntrypointOutputType:
    """Regression: /result must resolve per-entrypoint output_type from
    desc.workflow_type, not use the app's first-registered type or None.

    Original bug: _output_type on the App class was set to the first entry
    point's Output type (alphabetically via dir()), so any workflow that ran
    a different entry point would be deserialised with the wrong schema.

    The fix uses desc.workflow_type (e.g. "app-name:beta") to look up the
    correct EntryPointMetadata and pass its output_type to _get_workflow_result.
    """

    def _setup(self):
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

        class _MultiEpApp(App):
            name = "multi-ep-test"

            @entrypoint
            async def alpha(self, input: _AlphaInput) -> _AlphaOutput:
                return _AlphaOutput()

            @entrypoint
            async def beta(self, input: _BetaInput) -> _BetaOutput:
                return _BetaOutput()

        return create_app_handler_service(
            _TestHandler(),
            app_name="multi-ep-test",
            app_class=_MultiEpApp,
            temporal_host="temporal:7233",
        )

    def _teardown(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    @pytest.mark.parametrize(
        "ep_name,expected_output_type",
        [
            ("alpha", _AlphaOutput),
            ("beta", _BetaOutput),
        ],
    )
    def test_result_completed_resolves_per_entrypoint_output_type(
        self, ep_name: str, expected_output_type: type
    ) -> None:
        """Polling path: output_type passed to _get_workflow_result must match
        the entrypoint encoded in desc.workflow_type, not the app's
        first-registered type (_AlphaOutput) and not None."""
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()

            desc = MagicMock()
            desc.status.name = "COMPLETED"
            desc.workflow_type = f"multi-ep-test:{ep_name}"

            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

            captured: list[type | None] = []

            async def _spy(client, *, workflow_id, output_type):
                captured.append(output_type)
                return {"result_field": "ok"}

            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    side_effect=_spy,
                ),
            ):
                client = TestClient(svc)
                response = client.get(f"/workflows/v1/result/wf-{ep_name}")

            assert response.status_code == 200
            assert len(captured) == 1
            assert captured[0] is expected_output_type, (
                f"For entrypoint {ep_name!r} expected output_type="
                f"{expected_output_type.__name__!r}, got {captured[0]!r}. "
                "The /result endpoint must resolve per-entrypoint output_type "
                "from desc.workflow_type, not use the app-level first-registered "
                "type or None."
            )
        finally:
            self._teardown()

    def test_result_wait_resolves_per_entrypoint_output_type(self) -> None:
        """wait=True path: describe() must be called to obtain workflow_type
        so the correct output_type can be resolved before waiting."""
        from unittest.mock import AsyncMock, MagicMock

        try:
            svc = self._setup()

            desc = MagicMock()
            desc.workflow_type = "multi-ep-test:beta"

            mock_handle = MagicMock()
            mock_handle.describe = AsyncMock(return_value=desc)
            mock_client = MagicMock()
            mock_client.get_workflow_handle = MagicMock(return_value=mock_handle)

            captured: list[type | None] = []

            async def _spy(client, *, workflow_id, output_type):
                captured.append(output_type)
                return {"result_field": "ok"}

            with (
                patch(
                    "application_sdk.handler.service._get_temporal_client",
                    new=AsyncMock(return_value=mock_client),
                ),
                patch(
                    "application_sdk.handler.service._get_workflow_result",
                    side_effect=_spy,
                ),
            ):
                client = TestClient(svc)
                response = client.get("/workflows/v1/result/wf-beta-wait?wait=true")

            assert response.status_code == 200
            assert len(captured) == 1
            assert captured[0] is _BetaOutput, (
                f"For wait=True + 'beta' entrypoint expected output_type=_BetaOutput, "
                f"got {captured[0]!r}. "
                "The wait=True path must call describe() and resolve output_type "
                "from desc.workflow_type."
            )
        finally:
            self._teardown()


class TestUploadFileEndpoint:
    """Tests for POST /workflows/v1/file."""

    def test_upload_no_storage_returns_503(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/file",
            files={"file": ("hello.txt", b"hello")},
        )
        assert response.status_code == 503

    def test_upload_happy_path_returns_metadata(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="upload-test", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.upload_file", new=AsyncMock(return_value=None)
        ) as mock_upload:
            response = client.post(
                "/workflows/v1/file",
                files={"file": ("report.pdf", b"data", "application/pdf")},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        data = body["data"]
        # alias_generator=to_camel — fields exposed as camelCase
        assert data["rawName"] == "report.pdf"
        assert data["extension"] == "pdf"
        assert data["contentType"] == "application/pdf"
        assert data["fileSize"] == 4
        assert data["isUploaded"] is True
        # Storage upload was actually invoked
        mock_upload.assert_awaited_once()

    def test_upload_with_explicit_filename_and_prefix_overrides(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="upload2", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.upload_file", new=AsyncMock(return_value=None)
        ) as mock_upload:
            response = client.post(
                "/workflows/v1/file",
                files={"file": ("ignored.txt", b"hi")},
                data={"filename": "real.csv", "prefix": "custom/path"},
            )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["rawName"] == "real.csv"
        assert data["extension"] == "csv"
        # Key must use the supplied prefix, not the default
        assert data["key"].startswith("custom/path/")
        # And the storage layer was called with that same key
        called_key = mock_upload.await_args.args[0]
        assert called_key.startswith("custom/path/")

    def test_upload_strips_unsafe_extension_chars(self) -> None:
        """Extensions with non-alphanumerics must be sanitised — no path traversal."""
        from unittest.mock import AsyncMock, MagicMock

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="upload3", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.upload_file", new=AsyncMock(return_value=None)
        ):
            response = client.post(
                "/workflows/v1/file",
                files={"file": ("evil.tar/../foo", b"x")},
            )
        assert response.status_code == 200
        data = response.json()["data"]
        # Extension must not contain '/' or '.'
        assert "/" not in data["extension"]
        assert ".." not in data["extension"]

    def test_upload_falls_back_to_octet_stream_when_no_content_type(self) -> None:
        from unittest.mock import AsyncMock, MagicMock

        mock_storage = MagicMock()
        app = create_app_handler_service(
            _TestHandler(), app_name="upload4", storage=mock_storage
        )
        client = TestClient(app)
        with patch(
            "application_sdk.storage.ops.upload_file", new=AsyncMock(return_value=None)
        ):
            response = client.post(
                "/workflows/v1/file",
                files={"file": ("blob", b"raw")},
            )
        assert response.status_code == 200
        data = response.json()["data"]
        assert data["contentType"] == "application/octet-stream"
