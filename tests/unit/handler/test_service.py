"""Unit tests for the handler FastAPI service."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.testclient import TestClient

from application_sdk.handler.base import Handler, HandlerError
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.handler.service import (
    _convert_enums_recursive,
    _serialize_output,
    _wrap_response,
    create_app_handler_service,
)

# ---------------------------------------------------------------------------
# Test Handler implementation
# ---------------------------------------------------------------------------


class _TestHandler(Handler):
    """Minimal handler for testing."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[], total_count=0)


class _FailingHandler(Handler):
    """Handler that raises HandlerError."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        raise HandlerError("auth failed")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise HandlerError("preflight failed")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        raise HandlerError("metadata failed")


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

    def test_ready_endpoint_returns_200(self) -> None:
        client = _make_client()
        response = client.get("/server/ready")
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
        assert body["data"]["status"] == "ready"

    def test_preflight_handler_error_returns_500(self) -> None:
        client = _make_client(handler=_FailingHandler())
        response = client.post(
            "/workflows/v1/check",
            json={"credentials": []},
        )
        assert response.status_code == 500


class TestMetadataEndpoint:
    """Tests for POST /workflows/v1/metadata."""

    def test_metadata_success(self) -> None:
        client = _make_client()
        response = client.post(
            "/workflows/v1/metadata",
            json={"credentials": []},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["data"]["total_count"] == 0

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


class TestSerializeOutput:
    """Tests for _serialize_output helper."""

    def test_serializes_dataclass_output(self) -> None:
        output = AuthOutput(status=AuthStatus.SUCCESS, message="ok")
        result = _serialize_output(output)
        assert isinstance(result, dict)
        assert result["status"] == "success"
        assert result["message"] == "ok"

    def test_enum_converted_to_string(self) -> None:
        output = AuthOutput(status=AuthStatus.FAILED)
        result = _serialize_output(output)
        assert result["status"] == "failed"

    def test_serializes_dict_directly(self) -> None:
        from enum import Enum

        class MyEnum(Enum):
            FOO = "foo"

        d = {"key": MyEnum.FOO, "value": "bar"}
        result = _serialize_output(d)
        assert result["key"] == "foo"


class TestConvertEnumsRecursive:
    """Tests for _convert_enums_recursive helper."""

    def test_enum_converted_to_value(self) -> None:
        result = _convert_enums_recursive(AuthStatus.SUCCESS)
        assert result == "success"

    def test_non_enum_returned_unchanged(self) -> None:
        assert _convert_enums_recursive("hello") == "hello"
        assert _convert_enums_recursive(42) == 42
        assert _convert_enums_recursive(None) is None

    def test_dict_with_enum_values(self) -> None:
        result = _convert_enums_recursive({"status": AuthStatus.FAILED, "code": 500})
        assert result == {"status": "failed", "code": 500}

    def test_list_with_enum_values(self) -> None:
        result = _convert_enums_recursive([AuthStatus.SUCCESS, AuthStatus.FAILED])
        assert result == ["success", "failed"]

    def test_nested_structure(self) -> None:
        result = _convert_enums_recursive(
            {"outer": {"inner": AuthStatus.EXPIRED}, "list": [PreflightStatus.READY]}
        )
        assert result["outer"]["inner"] == "expired"
        assert result["list"] == ["ready"]


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
