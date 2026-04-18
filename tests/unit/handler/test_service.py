"""Unit tests for the handler FastAPI service."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from application_sdk.contracts.base import Input, Output
from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    ApiMetadataObject,
    ApiMetadataOutput,
    AuthInput,
    AuthOutput,
    AuthStatus,
    EventFilterRule,
    EventTriggerConfig,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    SqlMetadataObject,
    SqlMetadataOutput,
    SubscriptionConfig,
)
from application_sdk.handler.service import (
    _flatten_to_pairs,
    _normalize_credentials,
    _pairs_to_flat,
    _wrap_response,
    create_app_handler_service,
)

# ---------------------------------------------------------------------------
# Test Handler implementation
# ---------------------------------------------------------------------------


class _TestHandler(Handler):
    """Minimal handler for testing — returns SqlMetadataOutput."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return SqlMetadataOutput(objects=[])


class _ApiTreeHandler(Handler):
    """Handler that returns ApiMetadataOutput (BI connector path)."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return ApiMetadataOutput(
            objects=[
                ApiMetadataObject(value="tag-1", title="Tag One", node_type="tag"),
                ApiMetadataObject(value="tag-2", title="Tag Two", node_type="tag"),
            ]
        )


class _FailingHandler(Handler):
    """Handler that raises HandlerError."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise HandlerError("auth failed")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise HandlerError("preflight failed")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        raise HandlerError("metadata failed")


# ---------------------------------------------------------------------------
# Module-level contract types for routing tests
# (locally-defined dataclasses fail get_type_hints() when
#  'from __future__ import annotations' is active)
# ---------------------------------------------------------------------------


# Plain Pydantic subclasses — @dataclass would generate a conflicting __init__
# that breaks Pydantic's __pydantic_fields_set__ initialisation.
class _RoutingInput(Input, allow_unbounded_fields=True):  # type: ignore[call-arg]
    name: str = ""


class _RoutingOutput(Output, allow_unbounded_fields=True):  # type: ignore[call-arg]
    result: str = ""


# ---------------------------------------------------------------------------
# Helper: create test client
# ---------------------------------------------------------------------------


def _make_client(handler: Handler | None = None) -> TestClient:
    handler = handler or _TestHandler()
    app = create_app_handler_service(handler, app_name="test-app")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthEndpoints:
    """Tests for /health and /server/ready."""

    def test_health_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_server_health_alias_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_ready_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_ready_alias_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/ready")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


class TestAuthEndpoint:
    """Tests for POST /workflows/v1/auth."""

    def test_auth_success(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": [], "connection_id": "test-conn"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["status"] == "success"

    def test_auth_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500

    def test_auth_response_has_envelope_format(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        body = response.json()
        assert "success" in body
        assert "message" in body
        assert "data" in body

    def test_auth_with_credentials_succeeds(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "credentials": [{"key": "api_key", "value": "secret123"}],
            },
        )
        assert response.status_code == 200


class TestPreflightEndpoint:
    """Tests for POST /workflows/v1/check."""

    def test_preflight_success(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        # v2 format: data is a dict of check results keyed by camelCase name.
        # _TestHandler returns no checks, so data is empty.
        assert body["data"] == {}
        assert body["message"] == "ready"

    def test_preflight_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 500


class TestMetadataEndpoint:
    """Tests for POST /workflows/v1/metadata."""

    def test_metadata_sql_empty_returns_empty_list(self) -> None:
        """SqlMetadataOutput with no objects → empty list in data."""
        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == []

    def test_metadata_sql_returns_flat_rows(self) -> None:
        """SqlMetadataOutput → [{TABLE_CATALOG, TABLE_SCHEMA}] for sqltree widget."""

        class _SQLHandler(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return SqlMetadataOutput(
                    objects=[
                        SqlMetadataObject(
                            TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="FINANCE"
                        ),
                        SqlMetadataObject(
                            TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="SALES"
                        ),
                    ]
                )

        client = _make_client(handler=_SQLHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == [
            {"TABLE_CATALOG": "DEFAULT", "TABLE_SCHEMA": "FINANCE"},
            {"TABLE_CATALOG": "DEFAULT", "TABLE_SCHEMA": "SALES"},
        ]
        assert body["message"] == "Fetched 2 objects"

    def test_metadata_api_returns_tree_nodes(self) -> None:
        """ApiMetadataOutput → [{value, title, node_type, children}] for apitree widget."""
        client = _make_client(handler=_ApiTreeHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"] == [
            {"value": "tag-1", "title": "Tag One", "node_type": "tag", "children": []},
            {"value": "tag-2", "title": "Tag Two", "node_type": "tag", "children": []},
        ]
        assert body["message"] == "Fetched 2 objects"

    def test_metadata_api_nested_children(self) -> None:
        """ApiMetadataOutput with nested children serializes the full tree."""

        class _NestedHandler(_TestHandler):
            async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
                return ApiMetadataOutput(
                    objects=[
                        ApiMetadataObject(
                            value="proj-1",
                            title="Project A",
                            node_type="project",
                            children=[
                                ApiMetadataObject(
                                    value="ws-1",
                                    title="Workspace 1",
                                    node_type="workspace",
                                ),
                                ApiMetadataObject(
                                    value="ws-2",
                                    title="Workspace 2",
                                    node_type="workspace",
                                ),
                            ],
                        ),
                    ]
                )

        client = _make_client(handler=_NestedHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert data[0]["value"] == "proj-1"
        assert len(data[0]["children"]) == 2
        assert data[0]["children"][0]["value"] == "ws-1"
        assert data[0]["children"][1]["node_type"] == "workspace"

    def test_metadata_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 500


class TestStartWorkflowEndpoint:
    """Tests for POST /workflows/v1/start."""

    def test_start_workflow_without_temporal_config_returns_503(self) -> None:
        # Service created without app_class → not configured
        client = _make_client()
        response = client.post(
            "/workflows/v1/start",
            json={"name": "test"},
        )
        assert response.status_code == 503

    def test_start_workflow_with_app_class_but_no_temporal_host_returns_503(
        self,
    ) -> None:
        from application_sdk.app.base import App
        from application_sdk.app.registry import AppRegistry, TaskRegistry
        from application_sdk.contracts.base import Input, Output

        AppRegistry.reset()
        TaskRegistry.reset()

        try:

            @dataclass
            class _StartInput(Input, allow_unbounded_fields=True):
                name: str = ""

            @dataclass
            class _StartOutput(Output, allow_unbounded_fields=True):
                result: str = ""

            class _StartApp(App):
                async def run(self, input: _StartInput) -> _StartOutput:
                    return _StartOutput()

            # No temporal_host → is_configured() returns False
            handler = _TestHandler()
            app = create_app_handler_service(
                handler,
                app_name="start-test",
                app_class=_StartApp,
                temporal_host="",  # empty → not configured
            )
            client = TestClient(app)
            response = client.post("/workflows/v1/start", json={"name": "world"})
            assert response.status_code == 503
        finally:
            AppRegistry.reset()
            TaskRegistry.reset()


class TestErrorInfoDisclosure:
    """Regression: error responses must not leak internal exception details.

    The security fix removed exception interpolation from 4 HTTP error
    handlers. These tests verify the generic messages are returned.
    """

    def test_start_workflow_type_error_returns_generic_message(self) -> None:
        """TypeError handler on /start must not leak internal type info."""
        # Send invalid JSON that triggers a TypeError in workflow start
        client = _make_client()
        response = client.post(
            "/workflows/v1/start",
            json={"deliberately": "wrong_shape"},
        )
        # May return 400 (TypeError) or 503 (not configured) depending on setup
        # Either way, response body must not contain Python exception details
        body_str = str(response.json())
        assert "Traceback" not in body_str
        assert "TypeError" not in body_str

    def test_handler_error_500_does_not_leak_exception(self) -> None:
        """Handler endpoint errors should return generic messages."""
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/auth",
            json={"credentials": []},
        )
        assert response.status_code == 500
        body_str = str(response.json())
        assert "Traceback" not in body_str
        assert "File " not in body_str


class TestStartWorkflowRouting:
    """Tests for /workflows/v1/start entry-point routing logic."""

    def setup_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        from application_sdk.app.registry import AppRegistry, TaskRegistry

        AppRegistry.reset()
        TaskRegistry.reset()

    def _make_routed_client(self, app_cls: type, *, patch_temporal: bool = True):  # type: ignore[return]
        """Create a TestClient wired to app_cls with Temporal mocked out."""
        from unittest.mock import AsyncMock, MagicMock, patch

        handler = _TestHandler()
        svc = create_app_handler_service(
            handler,
            app_name="routing-test",
            app_class=app_cls,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-123"
        mock_handle.result_run_id = "run-abc"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        client = TestClient(svc, raise_server_exceptions=False)
        return client, patcher

    def test_single_ep_no_workflow_type_auto_selects(self) -> None:
        """Single-entry-point app accepts request with no workflow_type."""
        # Use module-level types — locally-defined dataclasses can't be resolved
        # by get_type_hints() because 'from __future__ import annotations' is active.
        from application_sdk.app.base import App

        class _SingleEpApp(App):
            async def run(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_SingleEpApp)
        try:
            response = client.post("/workflows/v1/start", json={"name": "hello"})
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_multi_ep_entrypoint_query_param_routes_correctly(self) -> None:
        """Multi-entry-point app with ?entrypoint= query param returns 200."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_MultiEpApp)
        try:
            response = client.post(
                "/workflows/v1/start?entrypoint=extract",
                json={"name": "x"},
            )
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_multi_ep_body_workflow_type_fallback_routes_correctly(self) -> None:
        """Multi-entry-point app falls back to body 'workflow_type' when ?entrypoint= absent."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _MultiEpFallbackApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_MultiEpFallbackApp)
        try:
            response = client.post(
                "/workflows/v1/start",
                json={"workflow_type": "extract", "name": "x"},
            )
            assert response.status_code == 200
        finally:
            patcher.stop()

    def test_query_param_takes_precedence_over_body_workflow_type(self) -> None:
        """?entrypoint= wins over body 'workflow_type' when both are provided."""
        from unittest.mock import AsyncMock, MagicMock, patch

        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _PrecedenceApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        handler = _TestHandler()
        svc = create_app_handler_service(
            handler,
            app_name="precedence-test",
            app_class=_PrecedenceApp,
            temporal_host="temporal:7233",
        )
        mock_client = MagicMock()
        mock_handle = MagicMock()
        mock_handle.id = "wf-prec"
        mock_handle.result_run_id = "run-prec"
        mock_client.start_workflow = AsyncMock(return_value=mock_handle)
        patcher = patch(
            "application_sdk.handler.service._get_temporal_client",
            new=AsyncMock(return_value=mock_client),
        )
        patcher.start()
        tc = TestClient(svc, raise_server_exceptions=False)
        try:
            # ?entrypoint=extract wins over body workflow_type=load
            response = tc.post(
                "/workflows/v1/start?entrypoint=extract",
                json={"workflow_type": "load", "name": "x"},
            )
            assert response.status_code == 200
            assert mock_client.start_workflow.call_count == 1
            # The started workflow name must end in ':extract', not ':load'
            started_name = mock_client.start_workflow.call_args[0][0]
            assert ":load" not in started_name, f"load was dispatched: {started_name!r}"
            assert started_name.endswith(
                ":extract"
            ), f"Expected :extract, got {started_name!r}"
        finally:
            patcher.stop()

    def test_unknown_entrypoint_query_param_returns_400(self) -> None:
        """Providing an unrecognised ?entrypoint= returns 400 without leaking names."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _TwoEpApp(App):
            @entrypoint
            async def extract(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def load(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_TwoEpApp)
        try:
            response = client.post(
                "/workflows/v1/start?entrypoint=does-not-exist",
                json={"name": "x"},
            )
            assert response.status_code == 400
            detail = response.json().get("detail", "")
            assert "extract" not in detail
            assert "load" not in detail
        finally:
            patcher.stop()

    def test_multi_ep_missing_entrypoint_returns_400(self) -> None:
        """Multi-entry-point app with no ?entrypoint= and no body selector returns 400."""
        from application_sdk.app.base import App
        from application_sdk.app.entrypoint import entrypoint

        class _AnotherMultiEpApp(App):
            @entrypoint
            async def step_a(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

            @entrypoint
            async def step_b(self, input: _RoutingInput) -> _RoutingOutput:
                return _RoutingOutput()

        client, patcher = self._make_routed_client(_AnotherMultiEpApp)
        try:
            response = client.post("/workflows/v1/start", json={"name": "x"})
            assert response.status_code == 400
            detail = response.json().get("detail", "")
            assert "step-a" not in detail
            assert "step-b" not in detail
        finally:
            patcher.stop()


class TestWrapResponse:
    """Tests for _wrap_response helper."""

    def test_basic_structure(self) -> None:
        result = _wrap_response({"key": "value"})
        assert result["success"] is True
        assert result["message"] == ""
        assert result["data"] == {"key": "value"}

    def test_custom_message(self) -> None:
        result = _wrap_response({}, message="All good")
        assert result["message"] == "All good"

    def test_failure_response(self) -> None:
        result = _wrap_response({"error": "oops"}, success=False, message="failed")
        assert result["success"] is False
        assert result["message"] == "failed"

    def test_list_data(self) -> None:
        result = _wrap_response([{"a": 1}, {"b": 2}], message="ok")
        assert result["data"] == [{"a": 1}, {"b": 2}]
        assert result["message"] == "ok"


class TestRunIdPathParam:
    """Tests for stop and status endpoints using slash-containing run_ids."""

    def test_status_with_slash_run_id(self) -> None:
        # Without a configured Temporal client the service returns 503,
        # but the route itself must resolve (not 404/405).
        client = _make_client()
        response = client.get("/workflows/v1/status/my-workflow/some/slashed/run-id")
        # 503 = route resolved but Temporal not configured; proves :path matched
        assert response.status_code == 503

    def test_stop_with_slash_run_id(self) -> None:
        client = _make_client()
        response = client.post("/workflows/v1/stop/my-workflow/some/slashed/run-id")
        assert response.status_code == 503


class TestConfigMapEndpoints:
    """Tests for GET /workflows/v1/configmap/{id} and /configmaps."""

    def test_configmap_not_found_returns_empty(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmap/nonexistent")
            assert response.status_code == 200
            body = response.json()
            assert body["data"] == {}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmap_returns_wrapped_k8s_shape(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        raw = {"config": {"key": "value"}}
        (tmp_path / "my-config.json").write_text(json.dumps(raw))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmap/my-config")
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["kind"] == "ConfigMap"
            assert data["apiVersion"] == "v1"
            assert data["metadata"]["name"] == "my-config"
            parsed_config = json.loads(data["data"]["config"])
            assert parsed_config == {"key": "value"}
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_lists_stems_excluding_manifest(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        (tmp_path / "config-a.json").write_text("{}")
        (tmp_path / "config-b.json").write_text("{}")
        (tmp_path / "manifest.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "config-a" in configmaps
            assert "config-b" in configmaps
            assert "manifest" not in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_empty_when_dir_missing(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        missing = tmp_path / "nonexistent"
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = missing
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            assert response.json()["data"]["configmaps"] == []
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestWorkflowConfigValidation:
    """Tests for _CONFIG_KEY_RE validation on /workflows/v1/config/{config_id}."""

    @pytest.mark.parametrize(
        "config_id",
        [
            "../foo",
            "a/b",
            "a.b",
            "",
            "a" * 129,
            "%2e%2e",
            "a b",
        ],
    )
    def test_get_config_rejects_invalid_config_id(self, config_id: str) -> None:
        client = _make_client()
        response = client.get(f"/workflows/v1/config/{config_id}")
        assert response.status_code in {
            400,
            404,
            422,
        }, f"Expected rejection for {config_id!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "config_id",
        [
            "../foo",
            "a/b",
            "a.b",
            "",
            "a" * 129,
            "%2e%2e",
            "a b",
        ],
    )
    def test_post_config_rejects_invalid_config_id(self, config_id: str) -> None:
        client = _make_client()
        response = client.post(
            f"/workflows/v1/config/{config_id}",
            json={"key": "value"},
        )
        assert response.status_code in {
            400,
            404,
            422,
        }, f"Expected rejection for {config_id!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "type_param",
        ["../etc", "a/b", "a.b", "a" * 129],
    )
    def test_get_config_rejects_invalid_type_param(self, type_param: str) -> None:
        client = _make_client()
        response = client.get(
            "/workflows/v1/config/valid-id",
            params={"type": type_param},
        )
        assert response.status_code in {
            400,
            503,
        }, f"Expected rejection for type={type_param!r}, got {response.status_code}"

    @pytest.mark.parametrize(
        "config_id",
        ["abc123", "ABC_123", "a-b", "a" * 128],
    )
    def test_get_config_accepts_valid_config_id(self, config_id: str) -> None:
        """Valid config_ids pass the regex check (result is 503/404 without a store, not 400)."""
        client = _make_client()
        response = client.get(f"/workflows/v1/config/{config_id}")
        assert (
            response.status_code != 400
        ), f"Valid config_id {config_id!r} was wrongly rejected"


class TestDaprSubscribeEndpoint:
    """Tests for GET /dapr/subscribe."""

    def test_subscribe_empty_when_no_triggers_or_subs(self) -> None:
        client = _make_client()
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        assert response.json() == []

    def test_subscribe_includes_event_trigger(self) -> None:
        handler = _TestHandler()
        trigger = EventTriggerConfig(
            event_id="my-trigger",
            event_type="metadata_extraction",
            event_name="extraction_requested",
            event_filters=[
                EventFilterRule(path="event.data.type", operator="==", value="meta")
            ],
        )
        app = create_app_handler_service(
            handler, app_name="test-app", event_triggers=[trigger]
        )
        client = TestClient(app)
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        subs = response.json()
        assert len(subs) == 1
        sub = subs[0]
        assert sub["topic"] == "metadata_extraction"
        routes = sub["routes"]
        assert routes["rules"][0]["path"] == "/events/v1/event/my-trigger"
        assert routes["default"] == "/events/v1/drop"

    def test_subscribe_includes_subscription_config(self) -> None:
        handler = _TestHandler()

        async def my_handler(request: object) -> dict:
            return {"status": "SUCCESS"}

        sub = SubscriptionConfig(
            component_name="my-pubsub",
            topic="my-topic",
            route="my-route",
            handler=my_handler,
        )
        app = create_app_handler_service(
            handler, app_name="test-app", subscriptions=[sub]
        )
        client = TestClient(app)
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        subs = response.json()
        assert len(subs) == 1
        assert subs[0]["pubsubname"] == "my-pubsub"
        assert subs[0]["topic"] == "my-topic"
        assert subs[0]["route"] == "/subscriptions/v1/my-route"


class TestDropEventEndpoint:
    """Tests for POST /events/v1/drop."""

    def test_drop_returns_drop_status(self) -> None:
        client = _make_client()
        response = client.post("/events/v1/drop")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "DROP"
        assert body["success"] is False


class TestDynamicSubscriptionRoutes:
    """Tests for dynamic /subscriptions/v1/{route} endpoints."""

    def test_subscription_route_is_registered(self) -> None:
        handler = _TestHandler()

        async def my_handler() -> dict:
            return {"status": "SUCCESS"}

        sub = SubscriptionConfig(
            component_name="pubsub",
            topic="topic",
            route="my-handler",
            handler=my_handler,
        )
        app = create_app_handler_service(
            handler, app_name="test-app", subscriptions=[sub]
        )
        client = TestClient(app)
        response = client.post("/subscriptions/v1/my-handler")
        assert response.status_code == 200
        assert response.json() == {"status": "SUCCESS"}


class TestManifestEndpoint:
    """Tests for GET /workflows/v1/manifest."""

    def _make_manifest(self):
        from application_sdk.handler.manifest import (
            AppManifest,
            DagNode,
            DagNodeDependency,
            ExecuteWorkflowInputs,
        )

        return AppManifest(
            execution_mode="dag",
            dag={
                "extract": DagNode(
                    activity_name="execute_workflow",
                    activity_display_name="Extract",
                    app_name="my-extractor",
                    inputs=ExecuteWorkflowInputs(
                        workflow_type="extraction",
                        task_queue="my-extractor-queue",
                        args={"connection": "{{connection}}"},
                    ),
                ),
                "load": DagNode(
                    activity_name="execute_workflow",
                    activity_display_name="Load",
                    app_name="my-loader",
                    inputs=ExecuteWorkflowInputs(
                        workflow_type="loading",
                        task_queue="my-loader-queue",
                    ),
                    depends_on=DagNodeDependency(node_id="extract"),
                ),
            },
            init_endpoint="/workflows/v1/init",
        )

    def test_manifest_returns_programmatic_manifest(self) -> None:
        manifest = self._make_manifest()
        app = create_app_handler_service(
            _TestHandler(), app_name="test-app", manifest=manifest
        )
        client = TestClient(app)
        response = client.get("/workflows/v1/manifest")
        assert response.status_code == 200
        body = response.json()
        assert body["execution_mode"] == "dag"
        assert "extract" in body["dag"]
        assert body["dag"]["extract"]["app_name"] == "my-extractor"
        assert body["dag"]["load"]["depends_on"]["node_id"] == "extract"
        assert body["init_endpoint"] == "/workflows/v1/init"

    def test_manifest_falls_back_to_disk(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "disk-app",
                    "inputs": {
                        "workflow_type": "extraction",
                        "task_queue": "disk-queue",
                    },
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest_data))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["execution_mode"] == "dag"
            assert body["dag"]["extract"]["app_name"] == "disk-app"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_disk_substitutes_deployment_name(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "my-app",
                    "inputs": {
                        "workflow_type": "extraction",
                        "task_queue": "{deployment_name}-queue",
                    },
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        svc_module.DEPLOYMENT_NAME = "prod-deploy"
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["dag"]["extract"]["inputs"]["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_manifest_disk_substitutes_app_name(self, tmp_path: Path) -> None:
        from application_sdk.handler import service as svc_module

        manifest_data = {
            "execution_mode": "dag",
            "dag": {
                "extract": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Extract",
                    "app_name": "{app_name}",
                    "inputs": {
                        "workflow_type": "extraction",
                        "task_queue": "{deployment_name}-queue",
                    },
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        svc_module.DEPLOYMENT_NAME = "prod-deploy"
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["dag"]["extract"]["app_name"] == "test-app"
            assert body["dag"]["extract"]["inputs"]["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_manifest_programmatic_takes_priority(self, tmp_path: Path) -> None:
        """When both programmatic and disk manifest exist, programmatic wins."""
        from application_sdk.handler import service as svc_module

        disk_data = {
            "execution_mode": "disk-mode",
            "dag": {
                "node": {
                    "activity_name": "execute_workflow",
                    "activity_display_name": "Node",
                    "app_name": "disk-app",
                    "inputs": {"workflow_type": "t", "task_queue": "q"},
                }
            },
        }
        (tmp_path / "manifest.json").write_text(__import__("json").dumps(disk_data))

        manifest = self._make_manifest()
        app = create_app_handler_service(
            _TestHandler(), app_name="test-app", manifest=manifest
        )
        client = TestClient(app)

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            # Programmatic manifest wins — has "dag" mode, not "disk-mode"
            assert body["execution_mode"] == "dag"
            assert "extract" in body["dag"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_manifest_404_when_none(self, tmp_path: Path) -> None:
        """Returns 404 when no manifest param and no manifest.json on disk."""
        from application_sdk.handler import service as svc_module

        missing = tmp_path / "nonexistent"
        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = missing
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_simple_app_manifest_lives_at_root_not_in_subdir(
        self, tmp_path: Path
    ) -> None:
        """Simple apps (only run(), no @entrypoint) place manifest.json at
        CONTRACT_GENERATED_DIR/manifest.json — no subdirectory.

        Heracles does not pass ?entrypoint= for these apps.  The endpoint must
        serve the root file when no param is present, and must NOT search inside
        any named subdirectory.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)
        # Root manifest — correct location for a simple app.
        (contract_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "linear", "app_name": "simple-app"})
        )
        # A subdir manifest should NOT be found when no ?entrypoint= is provided.
        subdir = contract_dir / "run"
        subdir.mkdir()
        (subdir / "manifest.json").write_text(json.dumps({"app_name": "wrong"}))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            # No ?entrypoint= → root manifest, not the subdir one.
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            body = response.json()
            assert body["app_name"] == "simple-app"
            assert body["execution_mode"] == "linear"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_simple_app_manifest_convention_vs_multi_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """Documents the path convention side-by-side:

        - Simple app (no @entrypoint):  CONTRACT_GENERATED_DIR/manifest.json
          Served via GET /manifest (no ?entrypoint= param).
        - Multi-entrypoint app:         CONTRACT_GENERATED_DIR/{name}/manifest.json
          Served via GET /manifest?entrypoint={name}.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        # Simple-app root manifest.
        (contract_dir / "manifest.json").write_text(json.dumps({"app_name": "simple"}))
        # Multi-entrypoint subdir manifests.
        for ep_name in ("crawler", "miner"):
            ep_dir = contract_dir / ep_name
            ep_dir.mkdir()
            (ep_dir / "manifest.json").write_text(json.dumps({"app_name": ep_name}))

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()

            # Simple app path: no param → root.
            resp = client.get("/workflows/v1/manifest")
            assert resp.status_code == 200
            assert resp.json()["app_name"] == "simple"

            # Multi-entrypoint paths: ?entrypoint= → subdir.
            for ep_name in ("crawler", "miner"):
                resp = client.get(f"/workflows/v1/manifest?entrypoint={ep_name}")
                assert resp.status_code == 200
                assert resp.json()["app_name"] == ep_name
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original


class TestNormalizeCredentials:
    """Tests for _normalize_credentials v2→v3 compat shim."""

    def test_v3_list_passthrough(self) -> None:
        body = {"credentials": [{"key": "host", "value": "localhost"}]}
        result = _normalize_credentials(body)
        assert result["credentials"] == [{"key": "host", "value": "localhost"}]

    def test_v3_empty_list_passthrough(self) -> None:
        body = {"credentials": []}
        result = _normalize_credentials(body)
        assert result["credentials"] == []

    def test_missing_credentials_passthrough(self) -> None:
        body = {"other_key": "value"}
        result = _normalize_credentials(body)
        assert result == {"other_key": "value"}

    def test_null_credentials_passthrough(self) -> None:
        body = {"credentials": None}
        result = _normalize_credentials(body)
        assert result["credentials"] is None

    def test_v2_dict_conversion(self) -> None:
        body = {
            "credentials": {
                "host": "app.mode.com",
                "username": "user1",
                "password": "secret",
            }
        }
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "app.mode.com"
        assert keys["username"] == "user1"
        assert keys["password"] == "secret"

    def test_v2_extra_flattening(self) -> None:
        body = {
            "credentials": {
                "host": "app.mode.com",
                "extra": {"workspace": "atlan", "region": "us"},
            }
        }
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "app.mode.com"
        assert keys["extra.workspace"] == "atlan"
        assert keys["extra.region"] == "us"
        assert "extra" not in keys

    def test_none_values_excluded(self) -> None:
        body = {"credentials": {"host": "localhost", "port": None}}
        result = _normalize_credentials(body)
        keys = [c["key"] for c in result["credentials"]]
        assert "host" in keys
        assert "port" not in keys

    def test_non_string_values_serialized_as_json(self) -> None:
        body = {
            "credentials": {
                "host": "localhost",
                "port": 5432,
                "ssl": True,
                "options": {"timeout": 30},
            }
        }
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "localhost"
        assert keys["port"] == "5432"
        assert keys["ssl"] == "true"
        assert keys["options"] == '{"timeout": 30}'

    def test_non_dict_extra_ignored(self) -> None:
        body = {"credentials": {"host": "localhost", "extra": "not-a-dict"}}
        result = _normalize_credentials(body)
        keys = {c["key"]: c["value"] for c in result["credentials"]}
        assert keys["host"] == "localhost"
        assert "extra" not in keys

    def test_does_not_mutate_original_body(self) -> None:
        body = {
            "credentials": {"host": "localhost"},
            "metadata": {"key": "value"},
        }
        original_meta = body["metadata"]
        result = _normalize_credentials(body)
        assert result is not body
        assert result["metadata"] is original_meta
        assert isinstance(body["credentials"], dict)

    def test_preserves_other_body_fields(self) -> None:
        body = {
            "credentials": {"host": "localhost"},
            "connection_id": "conn-123",
            "timeout_seconds": 60,
        }
        result = _normalize_credentials(body)
        assert result["connection_id"] == "conn-123"
        assert result["timeout_seconds"] == 60

    def test_v2_auth_integration(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "credentials": {
                    "host": "app.mode.com",
                    "username": "user",
                    "password": "pass",
                    "extra": {"workspace": "ws"},
                }
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "success"

    # ── Flat top-level credential format (Heracles credential test) ──────

    def test_flat_toplevel_conversion(self) -> None:
        """Heracles sends flat top-level keys: {"host": ..., "authType": ...}."""
        body = {
            "host": "myns.servicebus.windows.net:9093",
            "port": 9093,
            "authType": "basic",
            "username": "$ConnectionString",
            "password": "Endpoint=sb://myns/;SharedAccessKeyName=key;SharedAccessKey=secret",
            "extra": {"security_protocol": "SASL_SSL"},
            "connectorConfigName": "atlan-connectors-azure-event-hub",
        }
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "myns.servicebus.windows.net:9093"
        assert keys["authType"] == "basic"
        assert keys["username"] == "$ConnectionString"
        assert "Endpoint=sb://" in keys["password"]
        assert keys["extra.security_protocol"] == "SASL_SSL"
        assert keys["port"] == "9093"

    def test_flat_toplevel_no_extra(self) -> None:
        body = {"host": "localhost", "authType": "basic", "password": "secret"}
        result = _normalize_credentials(body)
        creds = result["credentials"]
        assert isinstance(creds, list)
        keys = {c["key"]: c["value"] for c in creds}
        assert keys["host"] == "localhost"
        assert keys["authType"] == "basic"

    def test_flat_toplevel_preserves_other_fields(self) -> None:
        body = {
            "host": "localhost",
            "authType": "basic",
            "connection_id": "conn-123",
            "timeout_seconds": 30,
        }
        result = _normalize_credentials(body)
        assert result["connection_id"] == "conn-123"
        assert result["timeout_seconds"] == 30
        assert isinstance(result["credentials"], list)

    def test_no_credential_keys_passthrough(self) -> None:
        """Body with no known credential keys should pass through unchanged."""
        body = {"connection_id": "conn-123", "metadata": {"key": "value"}}
        result = _normalize_credentials(body)
        assert "credentials" not in result
        assert result == body

    def test_flat_toplevel_auth_integration(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/auth",
            json={
                "host": "app.mode.com",
                "authType": "basic",
                "username": "user",
                "password": "pass",
                "extra": {"workspace": "ws"},
                "connectorConfigName": "test-connector",
            },
        )
        assert response.status_code == 200
        assert response.json()["data"]["status"] == "success"


class TestFlattenToPairs:
    """Tests for _flatten_to_pairs (flat dict → v3 list)."""

    def test_simple_keys(self) -> None:
        result = _flatten_to_pairs({"host": "db.example.com", "username": "admin"})
        keys = {p["key"]: p["value"] for p in result}
        assert keys == {"host": "db.example.com", "username": "admin"}

    def test_extra_nested(self) -> None:
        creds = dict(host="db.example.com", extra={"role": "ADMIN", "warehouse": "WH"})
        result = _flatten_to_pairs(creds)
        keys = {p["key"]: p["value"] for p in result}
        assert keys["extra.role"] == "ADMIN"
        assert keys["extra.warehouse"] == "WH"
        assert "extra" not in keys

    def test_none_values_skipped(self) -> None:
        result = _flatten_to_pairs({"host": "db.example.com", "port": None})
        keys = [p["key"] for p in result]
        assert "port" not in keys

    def test_empty_dict(self) -> None:
        assert _flatten_to_pairs({}) == []

    def test_non_dict_extra_dropped(self) -> None:
        """Non-dict extra values are silently ignored."""
        result = _flatten_to_pairs({"host": "db.example.com", "extra": "string"})
        keys = {p["key"] for p in result}
        assert keys == {"host"}

    def test_mutates_input_extra_key(self) -> None:
        """_flatten_to_pairs pops 'extra' from the input dict."""
        creds = {"host": "db.example.com", "extra": {"role": "ADMIN"}}
        _flatten_to_pairs(creds)
        assert "extra" not in creds


class TestPairsToFlat:
    """Tests for _pairs_to_flat (v3 list → flat dict)."""

    def test_simple_keys(self) -> None:
        pairs = [
            {"key": "host", "value": "db.example.com"},
            {"key": "username", "value": "admin"},
        ]
        result = _pairs_to_flat(pairs)
        assert result == {"host": "db.example.com", "username": "admin"}

    def test_extra_keys_nested(self) -> None:
        pairs = [
            {"key": "host", "value": "db.example.com"},
            {"key": "extra.role", "value": "ACCOUNTADMIN"},
            {"key": "extra.warehouse", "value": "MINER_WH"},
        ]
        result = _pairs_to_flat(pairs)
        assert result["host"] == "db.example.com"
        assert result["extra"] == {"role": "ACCOUNTADMIN", "warehouse": "MINER_WH"}
        assert "extra.role" not in result
        assert "extra.warehouse" not in result

    def test_no_extra_keys(self) -> None:
        pairs = [{"key": "host", "value": "db.example.com"}]
        result = _pairs_to_flat(pairs)
        assert "extra" not in result
        assert result == {"host": "db.example.com"}

    def test_empty_list(self) -> None:
        assert _pairs_to_flat([]) == {}

    def test_single_extra_key(self) -> None:
        """A single extra.* key still produces a nested extra dict."""
        pairs = [
            {"key": "host", "value": "db.example.com"},
            {"key": "extra.role", "value": "ADMIN"},
        ]
        result = _pairs_to_flat(pairs)
        assert result == {"host": "db.example.com", "extra": {"role": "ADMIN"}}

    def test_malformed_pair_raises_key_error(self) -> None:
        """Pairs missing 'key' or 'value' raise KeyError."""
        import pytest

        with pytest.raises(KeyError):
            _pairs_to_flat([{"key": "host"}])  # missing "value"
        with pytest.raises(KeyError):
            _pairs_to_flat([{"value": "db.example.com"}])  # missing "key"

    def test_round_trip_with_flatten_to_pairs(self) -> None:
        """_pairs_to_flat reverses _flatten_to_pairs for string values."""
        original = {
            "host": "snow.example.com",
            "authType": "basic",
            "username": "admin",
            "password": "secret",
            "extra": {
                "role": "ACCOUNTADMIN",
                "warehouse": "COMPUTE_WH",
                "database": "PROD",
            },
        }
        pairs = _flatten_to_pairs(
            dict(original)
        )  # dict() because _flatten_to_pairs pops extra
        restored = _pairs_to_flat(pairs)
        assert restored == original

    def test_round_trip_lossy_for_non_string_values(self) -> None:
        """Non-string values are stringified by _flatten_to_pairs and stay strings."""
        original = {"host": "db.example.com", "port": 5432, "ssl": True}
        pairs = _flatten_to_pairs(dict(original))
        restored = _pairs_to_flat(pairs)
        # Values are stringified — not equal to original types
        assert restored["port"] == "5432"  # int → str
        assert restored["ssl"] == "true"  # bool → str (json.dumps)
        assert restored["host"] == "db.example.com"  # str stays str


class TestStartCredentialPersistence:
    """Tests for inline credential save in /start handler.

    The /start endpoint needs Temporal, so we test the normalization +
    MockStateStore interaction directly to verify the contract.
    """

    async def test_normalize_then_store_v2_dict_credentials(self) -> None:
        """V2 dict credentials are normalized to v3 list before storage."""
        from application_sdk.testing.mocks import MockStateStore

        body = {
            "credentials": {
                "host": "db.example.com",
                "port": 5432,
                "username": "admin",
                "password": "secret",
            },
            "other_field": "kept",
        }

        body = _normalize_credentials(body)

        assert isinstance(body["credentials"], list)
        keys = {item["key"] for item in body["credentials"]}
        assert "host" in keys
        assert "port" in keys

        store = MockStateStore()
        guid = "test-guid-123"
        flat_creds = _pairs_to_flat(body["credentials"])
        await store.save(f"cred:{guid}", flat_creds)

        result = await store.load(f"cred:{guid}")
        assert result is not None
        assert result["host"] == "db.example.com"

        # Verify other fields preserved
        assert body["other_field"] == "kept"
        assert "credentials" in body

    async def test_normalize_then_store_v3_list_credentials(self) -> None:
        """V3 list credentials pass through normalization unchanged."""
        from application_sdk.testing.mocks import MockStateStore

        body = {
            "credentials": [
                {"key": "host", "value": "db.example.com"},
                {"key": "username", "value": "admin"},
            ],
        }

        body = _normalize_credentials(body)

        assert isinstance(body["credentials"], list)
        assert len(body["credentials"]) == 2

        store = MockStateStore()
        guid = "test-guid-456"
        flat_creds = _pairs_to_flat(body["credentials"])
        await store.save(f"cred:{guid}", flat_creds)

        result = await store.load(f"cred:{guid}")
        assert result is not None
        assert result["host"] == "db.example.com"

    def test_no_credentials_skips_store(self) -> None:
        """Body without credentials is not stored."""
        body = {"name": "test-workflow"}
        body = _normalize_credentials(body)
        assert "credentials" not in body or not body.get("credentials")

    async def test_credential_resolver_reads_from_secret_store(self) -> None:
        """CredentialResolver reads credentials from secret store."""
        from application_sdk.credentials.ref import CredentialRef
        from application_sdk.credentials.resolver import CredentialResolver
        from application_sdk.testing.mocks import MockSecretStore

        store = MockSecretStore()
        creds = [
            {"key": "host", "value": "db.example.com"},
            {"key": "port", "value": "5432"},
        ]
        store.set("my-guid", json.dumps(creds))

        resolver = CredentialResolver(secret_store=store)
        ref = CredentialRef(name="my-guid", credential_type="basic")

        result = await resolver.resolve_raw(ref)

        assert isinstance(result, list)
        assert result[0]["key"] == "host"
        assert result[0]["value"] == "db.example.com"


# ---------------------------------------------------------------------------
# Entrypoint manifest resolution tests (Option B: name == folder name)
# ---------------------------------------------------------------------------


class TestEntrypointManifestResolution:
    """Tests for GET /workflows/v1/manifest?entrypoint={name}.

    Option B: entrypoint name IS the folder name — no atlan.yaml parsing.
    GET /manifest?entrypoint=X serves CONTRACT_GENERATED_DIR/X/manifest.json.
    """

    def test_manifest_with_entrypoint_returns_correct_manifest(
        self, tmp_path: Path
    ) -> None:
        """GET /manifest?entrypoint=snowflake resolves to snowflake/manifest.json."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "snowflake"
        ep_dir.mkdir(parents=True)
        manifest_data = {"execution_mode": "dag", "app_name": "snowflake-extractor"}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=snowflake")
            assert response.status_code == 200
            body = response.json()
            assert body["execution_mode"] == "dag"
            assert body["app_name"] == "snowflake-extractor"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_manifest_substitutes_deployment_name_for_entrypoint(
        self, tmp_path: Path
    ) -> None:
        """Entrypoint manifest replaces {deployment_name} placeholder."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "ep1"
        ep_dir.mkdir(parents=True)
        manifest_data = {"task_queue": "{deployment_name}-queue"}
        (ep_dir / "manifest.json").write_text(json.dumps(manifest_data))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        original_dep = svc_module.DEPLOYMENT_NAME
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        svc_module.DEPLOYMENT_NAME = "prod-deploy"
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=ep1")
            assert response.status_code == 200
            body = response.json()
            assert body["task_queue"] == "prod-deploy-queue"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir
            svc_module.DEPLOYMENT_NAME = original_dep

    def test_valid_but_unknown_entrypoint_returns_404(self, tmp_path: Path) -> None:
        """Well-formed name with no matching subdir on disk returns 404."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=unknown")
            assert response.status_code == 404
            assert "No manifest found" in response.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_entrypoint_with_missing_manifest_file_returns_404(
        self, tmp_path: Path
    ) -> None:
        """Subdir exists but manifest.json is absent — returns 404."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        (contract_dir / "snowflake").mkdir(parents=True)  # dir, but no manifest.json

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest?entrypoint=snowflake")
            assert response.status_code == 404
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_invalid_entrypoint_name_returns_400(self, tmp_path: Path) -> None:
        """Entrypoint names with path-traversal or illegal chars return 400."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            for bad_name in ["../etc", "a/b", "name with spaces", "name@bad"]:
                resp = client.get(f"/workflows/v1/manifest?entrypoint={bad_name}")
                assert resp.status_code == 400, f"Expected 400 for {bad_name!r}"
                assert resp.json()["detail"] == "Invalid entrypoint name"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_valid_name_not_in_registry_returns_404(self, tmp_path: Path) -> None:
        """A well-formed name with no matching manifest on disk returns 404.

        The glob-based registry only contains entrypoints whose manifest.json
        exists; a valid-format name with no file returns 404, not 400. Path escape
        is structurally impossible because glob() only yields paths under
        CONTRACT_GENERATED_DIR — no is_relative_to guard is needed.
        """
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            resp = client.get("/workflows/v1/manifest?entrypoint=valid")
            assert resp.status_code == 404
            assert "valid" in resp.json()["detail"]
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_legacy_manifest_alias_forwards_entrypoint(self, tmp_path: Path) -> None:
        """GET /manifest?entrypoint=name uses the legacy alias correctly."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        ep_dir = contract_dir / "snow"
        ep_dir.mkdir(parents=True)
        (ep_dir / "manifest.json").write_text(json.dumps({"app_name": "snow-app"}))

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/manifest?entrypoint=snow")
            assert response.status_code == 200
            assert response.json()["app_name"] == "snow-app"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir

    def test_no_entrypoint_param_serves_root_manifest(self, tmp_path: Path) -> None:
        """Without entrypoint param, serves root manifest unchanged."""
        from application_sdk.handler import service as svc_module

        contract_dir = tmp_path / "generated"
        contract_dir.mkdir(parents=True)
        (contract_dir / "manifest.json").write_text(
            json.dumps({"execution_mode": "linear"})
        )

        original_dir = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = contract_dir
        try:
            client = _make_client()
            response = client.get("/workflows/v1/manifest")
            assert response.status_code == 200
            assert response.json()["execution_mode"] == "linear"
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original_dir


# ---------------------------------------------------------------------------
# Configmaps deduplication tests
# ---------------------------------------------------------------------------


class TestConfigMapsDeduplication:
    """Tests for list_configmaps deduplication across subdirectories."""

    def test_configmaps_dedupes_across_subdirs(self, tmp_path: Path) -> None:
        """Configmaps with same stem in different subdirs appear only once."""
        from application_sdk.handler import service as svc_module

        # Root-level configs
        (tmp_path / "config-a.json").write_text("{}")
        (tmp_path / "config-b.json").write_text("{}")

        # Sub-directory with duplicate stem + new config
        subdir = tmp_path / "snow-gen"
        subdir.mkdir()
        (subdir / "config-a.json").write_text("{}")  # duplicate of root
        (subdir / "config-c.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            # config-a should appear only once despite being in root and subdir
            assert configmaps.count("config-a") == 1
            assert "config-b" in configmaps
            assert "config-c" in configmaps
            assert "manifest" not in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_rglob_finds_nested_configs(self, tmp_path: Path) -> None:
        """rglob finds JSON files in nested sub-directories."""
        from application_sdk.handler import service as svc_module

        # Only sub-directory configs, no root configs
        subdir = tmp_path / "entrypoint-a"
        subdir.mkdir()
        (subdir / "nested-config.json").write_text("{}")

        deep_subdir = tmp_path / "entrypoint-b" / "deep"
        deep_subdir.mkdir(parents=True)
        (deep_subdir / "deep-config.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "nested-config" in configmaps
            assert "deep-config" in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original

    def test_configmaps_excludes_manifest_in_subdirs(self, tmp_path: Path) -> None:
        """manifest.json in subdirectories is also excluded."""
        from application_sdk.handler import service as svc_module

        subdir = tmp_path / "ep-gen"
        subdir.mkdir()
        (subdir / "manifest.json").write_text("{}")
        (subdir / "real-config.json").write_text("{}")

        original = svc_module.CONTRACT_GENERATED_DIR
        svc_module.CONTRACT_GENERATED_DIR = tmp_path
        try:
            client = _make_client()
            response = client.get("/workflows/v1/configmaps")
            assert response.status_code == 200
            configmaps = response.json()["data"]["configmaps"]
            assert "manifest" not in configmaps
            assert "real-config" in configmaps
        finally:
            svc_module.CONTRACT_GENERATED_DIR = original
