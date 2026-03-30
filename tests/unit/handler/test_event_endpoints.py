"""Unit tests for EventRegistry-based event endpoints in the handler service."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from application_sdk.app.event import EventHandlerMetadata
from application_sdk.app.event_registry import EventRegistry, RegisteredEventHandler
from application_sdk.contracts.base import Input, Output
from application_sdk.handler.base import Handler
from application_sdk.handler.contracts import (
    AuthInput,
    AuthOutput,
    AuthStatus,
    EventResponse,
    EventStatus,
    MetadataInput,
    MetadataOutput,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.handler.service import create_app_handler_service

# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


class _TestEventInput(Input, allow_unbounded_fields=True):
    entity_id: str = ""
    action: str = ""


class _TestEventOutput(Output, allow_unbounded_fields=True):
    result: str = ""


class _TestHandler(Handler):
    """Minimal handler for testing."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="auth ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[], total_count=0)


async def _dummy_handler(self: object, input: _TestEventInput) -> _TestEventOutput:
    return _TestEventOutput(result="done")


def _register_test_handler(
    app_name: str = "test-app",
    event_name: str = "entity_update",
    version: str = "1.0",
    topic: str = "atlas_events",
) -> RegisteredEventHandler:
    """Register a test event handler in the EventRegistry."""
    meta = EventHandlerMetadata(
        topic=topic,
        event_name=event_name,
        version=version,
        func=_dummy_handler,
        input_type=_TestEventInput,
        output_type=_TestEventOutput,
    )
    registry = EventRegistry.get_instance()
    return registry.register(
        app_name=app_name,
        handler_metadata=meta,
        workflow_name=f"{app_name}.{event_name}",
    )


def _make_client_with_registry(app_name: str = "test-app") -> TestClient:
    handler = _TestHandler()
    app = create_app_handler_service(handler, app_name=app_name)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEventResponseContract:
    """Tests for EventStatus and EventResponse models."""

    def test_event_status_values(self) -> None:
        assert EventStatus.SUCCESS.value == "success"
        assert EventStatus.DROP.value == "drop"
        assert EventStatus.RETRY.value == "retry"

    def test_event_response_model_dump(self) -> None:
        resp = EventResponse(
            status=EventStatus.SUCCESS,
            workflow_id="wf-123",
            message="ok",
        )
        dumped = resp.model_dump()
        assert dumped["status"] == "success"
        assert dumped["workflow_id"] == "wf-123"
        assert dumped["message"] == "ok"

    def test_event_response_defaults(self) -> None:
        resp = EventResponse(status=EventStatus.DROP)
        assert resp.workflow_id == ""
        assert resp.message == ""


class TestDaprSubscribeWithRegistry:
    """Tests for GET /dapr/subscribe including EventRegistry subscriptions."""

    def setup_method(self) -> None:
        EventRegistry.reset()

    def teardown_method(self) -> None:
        EventRegistry.reset()

    def test_subscribe_empty_when_no_registry_handlers(self) -> None:
        client = _make_client_with_registry()
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        assert response.json() == []

    def test_subscribe_includes_registry_subscriptions(self) -> None:
        _register_test_handler(app_name="test-app", event_name="entity_update")
        client = _make_client_with_registry(app_name="test-app")
        response = client.get("/dapr/subscribe")
        assert response.status_code == 200
        subs = response.json()
        # Should include EventRegistry-generated subscription
        registry_subs = [s for s in subs if s.get("pubsubname") == "eventstore"]
        assert len(registry_subs) >= 1
        sub = registry_subs[0]
        assert sub["topic"] == "atlas_events"
        rules = sub["routes"]["rules"]
        assert any("/events/v1/event/entity_update__1_0" in r["path"] for r in rules)
        assert sub["routes"]["default"] == "/events/v1/drop"


class TestDropEventEndpointWithRegistry:
    """Tests for POST /events/v1/drop."""

    def setup_method(self) -> None:
        EventRegistry.reset()

    def teardown_method(self) -> None:
        EventRegistry.reset()

    def test_drop_returns_drop_status(self) -> None:
        client = _make_client_with_registry()
        response = client.post("/events/v1/drop")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "DROP"


class TestRegistryEventRoutes:
    """Tests for per-event handler routes from EventRegistry."""

    def setup_method(self) -> None:
        EventRegistry.reset()

    def teardown_method(self) -> None:
        EventRegistry.reset()

    def test_event_route_exists_for_registered_handler(self) -> None:
        _register_test_handler(app_name="test-app", event_name="entity_update")
        client = _make_client_with_registry(app_name="test-app")
        # The route should exist — posting to it will fail because Temporal
        # is not configured, but the route itself should be registered (not 404/405)
        response = client.post(
            "/events/v1/event/entity_update__1_0",
            json={"data": {"entity_id": "abc", "action": "update"}},
        )
        # Should not be 404 (route not found) or 405 (method not allowed)
        assert response.status_code == 200
        body = response.json()
        # Should return RETRY since Temporal client is not available
        assert body["status"] in ("retry", "success")

    @patch(
        "application_sdk.handler.service._get_temporal_client",
        new_callable=AsyncMock,
    )
    def test_event_route_starts_workflow(self, mock_get_client: AsyncMock) -> None:
        _register_test_handler(app_name="test-app", event_name="entity_update")

        mock_client = AsyncMock()
        mock_handle = AsyncMock()
        mock_handle.id = "wf-123"
        mock_handle.result_run_id = "run-456"
        mock_client.start_workflow.return_value = mock_handle
        mock_get_client.return_value = mock_client

        # Must register before creating the app so routes are built
        client = _make_client_with_registry(app_name="test-app")
        response = client.post(
            "/events/v1/event/entity_update__1_0",
            json={"data": {"entity_id": "abc", "action": "update"}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "success"
        assert body["message"] == "Workflow started"
        assert body["workflow_id"].startswith("test-app-event-entity_update__1_0-")

        # Verify Temporal was called correctly
        mock_client.start_workflow.assert_called_once()
        call_args = mock_client.start_workflow.call_args
        assert call_args[0][0] == "test-app.entity_update"  # workflow_name

    @patch(
        "application_sdk.handler.service._get_temporal_client",
        new_callable=AsyncMock,
    )
    def test_event_route_returns_retry_on_failure(
        self, mock_get_client: AsyncMock
    ) -> None:
        _register_test_handler(app_name="test-app", event_name="entity_update")

        mock_client = AsyncMock()
        mock_client.start_workflow.side_effect = RuntimeError("connection failed")
        mock_get_client.return_value = mock_client

        client = _make_client_with_registry(app_name="test-app")
        response = client.post(
            "/events/v1/event/entity_update__1_0",
            json={"data": {"entity_id": "abc", "action": "update"}},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "retry"
        assert "connection failed" in body["message"]

    def test_multiple_event_routes_registered(self) -> None:
        _register_test_handler(
            app_name="test-app", event_name="entity_update", topic="atlas_events"
        )
        _register_test_handler(
            app_name="test-app", event_name="entity_delete", topic="atlas_events"
        )

        client = _make_client_with_registry(app_name="test-app")

        # Both routes should exist
        routes = [r.path for r in client.app.routes]  # type: ignore[union-attr]
        assert "/events/v1/event/entity_update__1_0" in routes
        assert "/events/v1/event/entity_delete__1_0" in routes
