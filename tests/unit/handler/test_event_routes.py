"""The Dapr subscription and event routes, registered on a bare app through their source.

These build only ``register_event_routes`` over a fake Temporal client, not the
whole handler service; ``test_service_events.py`` covers the same routes as the
worker wires them.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application_sdk.handler.contracts import (
    EventFilterRule,
    EventTriggerConfig,
    SubscriptionConfig,
)
from application_sdk.handler.event_routes import (
    EventRoutesSource,
    register_event_routes,
)
from application_sdk.handler.service import WorkflowClientConfig

_TRIGGER = EventTriggerConfig(
    event_id="extract",
    event_type="metadata_extraction",
    event_name="extraction_requested",
    event_filters=[EventFilterRule(path="event.data.kind", operator="==", value="db")],
)


class _EventInput:
    """An app input that records what the route stamps on it."""

    def __init__(self, **data: Any) -> None:
        self.data = data
        self.workflow_id = ""
        self.correlation_id = data.get("correlation_id", "")
        self._correlation_id = ""


class _App:
    _app_name = "events-test"
    _input_type = _EventInput


async def _on_message(payload: dict[str, Any]) -> dict[str, str]:
    return {"status": "SUCCESS"}


_SUBSCRIPTION = SubscriptionConfig(
    component_name="pubsub",
    topic="assets",
    route="assets",
    handler=_on_message,
    dead_letter_topic="assets-dlq",
)


def _config(
    *, configured: bool = True, timeout_hours: int | None = None
) -> WorkflowClientConfig:
    return WorkflowClientConfig(
        host="localhost:7233" if configured else "",
        task_queue="events-queue",
        app_name="events-test",
        app_class=_App,
        workflow_max_timeout_hours=timeout_hours,
    )


def _routes(client: Any = None, **config: Any) -> tuple[TestClient, MagicMock]:
    if client is None:
        client = MagicMock()
        client.start_workflow = AsyncMock(
            return_value=MagicMock(id="wf-1", result_run_id="run-1")
        )
    workflow_config = _config(**config)
    app = FastAPI()
    register_event_routes(
        app,
        EventRoutesSource(
            app_name="events-test",
            workflow_config=lambda: workflow_config,
            temporal_client=AsyncMock(return_value=client),
            event_triggers=[_TRIGGER],
            subscriptions=[_SUBSCRIPTION],
        ),
    )
    return TestClient(app, raise_server_exceptions=False), client


class TestDaprSubscribe:
    def test_lists_subscriptions_then_triggers(self) -> None:
        http, _ = _routes()

        entries = http.get("/dapr/subscribe").json()

        assert entries[0] == {
            "pubsubname": "pubsub",
            "topic": "assets",
            "route": "/subscriptions/v1/assets",
            "deadLetterTopic": "assets-dlq",
        }
        rule = entries[1]["routes"]["rules"][0]
        assert entries[1]["topic"] == "metadata_extraction"
        assert rule["path"] == "/events/v1/event/extract"
        assert rule["match"] == (
            "(event.data.kind == 'db') && "
            "event.data.event_name == 'extraction_requested' && "
            "event.data.event_type == 'metadata_extraction'"
        )
        assert entries[1]["routes"]["default"] == "/events/v1/drop"

    def test_subscription_handler_is_routed(self) -> None:
        http, _ = _routes()

        response = http.post("/subscriptions/v1/assets", json={"id": 1})

        assert response.status_code == 200
        assert response.json() == {"status": "SUCCESS"}

    def test_drop_answers_drop(self) -> None:
        http, _ = _routes()

        assert http.post("/events/v1/drop").json() == {
            "success": False,
            "status": "DROP",
        }


class TestHandleEvent:
    def test_not_configured_is_503(self) -> None:
        http, client = _routes(configured=False)

        response = http.post("/events/v1/event/extract", json={})

        assert response.status_code == 503
        assert response.json()["detail"] == "Workflow execution not configured."
        client.start_workflow.assert_not_awaited()

    def test_unknown_event_is_404(self) -> None:
        http, _ = _routes()

        response = http.post("/events/v1/event/nope", json={})

        assert response.status_code == 404
        assert response.json()["detail"] == "No trigger registered for event_id: nope"

    def test_body_that_is_not_json_is_400(self) -> None:
        http, client = _routes()

        response = http.post("/events/v1/event/extract", content=b"{not json")

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid JSON body"
        client.start_workflow.assert_not_awaited()

    def test_starts_the_app_workflow_with_the_event_data(self) -> None:
        http, client = _routes(timeout_hours=2)

        response = http.post(
            "/events/v1/event/extract",
            json={"data": {"kind": "db", "correlation_id": "corr-1"}},
        )

        assert response.status_code == 200
        assert response.json() == {
            "status": "SUCCESS",
            "workflow_id": "wf-1",
            "run_id": "run-1",
            "correlation_id": "corr-1",
        }
        call = client.start_workflow.await_args
        assert call.args[0] == "events-test"
        (input_data,) = call.kwargs["args"]
        assert input_data.data["kind"] == "db"
        assert call.kwargs["id"].startswith("events-test-event-extract-")
        assert input_data.workflow_id == call.kwargs["id"]
        assert call.kwargs["task_queue"] == "events-queue"
        assert call.kwargs["execution_timeout"].total_seconds() == 2 * 3600

    def test_failed_start_asks_dapr_to_retry(self) -> None:
        client = MagicMock()
        client.start_workflow = AsyncMock(side_effect=RuntimeError("frontend down"))
        http, _ = _routes(client)

        response = http.post("/events/v1/event/extract", json={"data": {}})

        assert response.status_code == 500
        assert response.json() == {"status": "RETRY"}


def test_workflow_config_is_read_per_event() -> None:
    """The config is read when the event arrives, not when the routes are built."""
    client = MagicMock()
    client.start_workflow = AsyncMock(
        return_value=MagicMock(id="wf-1", result_run_id="run-1")
    )
    current = {"config": _config(configured=False)}
    app = FastAPI()
    register_event_routes(
        app,
        EventRoutesSource(
            app_name="events-test",
            workflow_config=lambda: current["config"],
            temporal_client=AsyncMock(return_value=client),
            event_triggers=[_TRIGGER],
        ),
    )
    http = TestClient(app, raise_server_exceptions=False)
    assert http.post("/events/v1/event/extract", json={}).status_code == 503

    current["config"] = WorkflowClientConfig(
        host="localhost:7233",
        task_queue="reconfigured-queue",
        app_name="events-test",
        app_class=_App,
    )
    response = http.post("/events/v1/event/extract", json={"data": {}})

    assert response.status_code == 200
    assert client.start_workflow.await_args.kwargs["task_queue"] == "reconfigured-queue"
