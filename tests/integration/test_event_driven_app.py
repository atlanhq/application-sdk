"""Integration test for the full event-driven app lifecycle.

Exercises the complete developer experience: define an App with run() +
multiple @on_event handlers + shared @task methods, then verify that all
registries and handler endpoints behave correctly.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient

from application_sdk.app import App, AppRegistry, EventRegistry, on_event, task
from application_sdk.app.registry import TaskRegistry
from application_sdk.contracts.base import Input, Output
from application_sdk.handler.base import Handler
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
from application_sdk.handler.service import create_app_handler_service

# =============================================================================
# Contract dataclasses (module-level so they exist before class definition)
# =============================================================================


@dataclass
class CrawlInput(Input):
    source: str = ""


@dataclass
class CrawlOutput(Output):
    synced: int = 0


@dataclass
class AnomalyEvent(Input):
    anomaly_id: str = ""
    severity: float = 0.0


@dataclass
class AnomalyOutput(Output):
    action: str = ""


@dataclass
class ScanEvent(Input):
    scan_id: str = ""


@dataclass
class ScanOutput(Output):
    processed: bool = False


@dataclass
class SyncInput(Input):
    data: str = ""


@dataclass
class SyncOutput(Output):
    ok: bool = False


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture(autouse=True)
def _reset_registries():
    """Reset all registries before and after each test."""
    AppRegistry.reset()
    TaskRegistry.reset()
    EventRegistry.reset()
    yield
    AppRegistry.reset()
    TaskRegistry.reset()
    EventRegistry.reset()


@pytest.fixture()
def qualytics_app_cls():
    """Define the QualyticsApp class inside a fixture so registration
    happens after registry reset."""

    class QualyticsApp(App):
        name = "qualytics-app"
        version = "1.0.0"

        async def run(self, input: CrawlInput) -> CrawlOutput:
            await self.sync_to_atlan(SyncInput(data="crawl"))
            return CrawlOutput(synced=1)

        @on_event(
            topic="qualytics-events", event_name="anomaly_detected", version="2.0"
        )
        async def handle_anomaly(self, input: AnomalyEvent) -> AnomalyOutput:
            if input.severity < 0.5:
                return AnomalyOutput(action="skipped")
            await self.sync_to_atlan(SyncInput(data="anomaly"))
            return AnomalyOutput(action="processed")

        @on_event(topic="qualytics-events", event_name="scan_completed", version="1.0")
        async def handle_scan(self, input: ScanEvent) -> ScanOutput:
            await self.sync_to_atlan(SyncInput(data="scan"))
            return ScanOutput(processed=True)

        @task(timeout_seconds=120)
        async def sync_to_atlan(self, input: SyncInput) -> SyncOutput:
            return SyncOutput(ok=True)

    return QualyticsApp


class _MinimalHandler(Handler):
    """Minimal handler for endpoint testing."""

    async def test_auth(self, input: AuthInput) -> AuthOutput:
        return AuthOutput(status=AuthStatus.SUCCESS, message="ok")

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(status=PreflightStatus.READY, message="ready")

    async def fetch_metadata(self, input: MetadataInput) -> MetadataOutput:
        return MetadataOutput(objects=[], total_count=0)


@pytest.fixture()
def handler_client(qualytics_app_cls):
    """Create a FastAPI TestClient with the handler service wired to qualytics-app."""
    handler = _MinimalHandler()
    app = create_app_handler_service(handler, app_name="qualytics-app")
    return TestClient(app)


# =============================================================================
# Tests
# =============================================================================


class TestMainAppRegistered:
    """Verify the main App is in AppRegistry with correct types."""

    def test_main_app_registered(self, qualytics_app_cls):
        registry = AppRegistry.get_instance()
        meta = registry.get("qualytics-app")

        assert meta.name == "qualytics-app"
        assert meta.version == "1.0.0"
        assert meta.input_type is CrawlInput
        assert meta.output_type is CrawlOutput
        assert meta.app_cls is qualytics_app_cls


class TestEventHandlersRegistered:
    """Verify both @on_event handlers appear in EventRegistry."""

    def test_event_handlers_registered(self, qualytics_app_cls):
        registry = EventRegistry.get_instance()
        handlers = registry.get_by_app("qualytics-app")

        assert len(handlers) == 2

        event_ids = {h.event_id for h in handlers}
        assert "anomaly_detected__2_0" in event_ids
        assert "scan_completed__1_0" in event_ids

        # Verify topics
        topics = {h.topic for h in handlers}
        assert topics == {"qualytics-events"}


class TestEventHandlerWorkflowsInAppRegistry:
    """Verify dynamic event-handler workflows are discoverable in AppRegistry."""

    def test_event_handler_workflows_in_app_registry(self, qualytics_app_cls):
        registry = AppRegistry.get_instance()

        # anomaly handler workflow
        anomaly_meta = registry.get("qualytics-app.handle-anomaly")
        assert anomaly_meta.name == "qualytics-app.handle-anomaly"
        assert anomaly_meta.input_type is AnomalyEvent
        assert anomaly_meta.output_type is AnomalyOutput
        assert issubclass(anomaly_meta.app_cls, qualytics_app_cls)
        assert anomaly_meta.app_cls is not qualytics_app_cls

        # scan handler workflow
        scan_meta = registry.get("qualytics-app.handle-scan")
        assert scan_meta.name == "qualytics-app.handle-scan"
        assert scan_meta.input_type is ScanEvent
        assert scan_meta.output_type is ScanOutput
        assert issubclass(scan_meta.app_cls, qualytics_app_cls)
        assert scan_meta.app_cls is not qualytics_app_cls


class TestSharedTasksRegistered:
    """Verify @task methods are registered in TaskRegistry for the parent app."""

    def test_shared_tasks_registered(self, qualytics_app_cls):
        registry = TaskRegistry.get_instance()
        tasks = registry.get_tasks_for_app("qualytics-app")
        task_names = {t.name for t in tasks}

        # The user-defined task must be present
        assert "sync_to_atlan" in task_names

        # Verify its metadata
        sync_task = registry.get("qualytics-app", "sync_to_atlan")
        assert sync_task.timeout_seconds == 120
        assert sync_task.app_name == "qualytics-app"


class TestDaprSubscribeEndpoint:
    """Verify /dapr/subscribe returns correct subscription configs."""

    def test_dapr_subscribe_endpoint(self, handler_client):
        response = handler_client.get("/dapr/subscribe")
        assert response.status_code == 200

        subs = response.json()
        # Filter to EventRegistry-generated subscriptions
        registry_subs = [s for s in subs if s.get("pubsubname") == "eventstore"]
        assert len(registry_subs) >= 1

        # All handlers share the same topic, so there should be one subscription
        sub = registry_subs[0]
        assert sub["topic"] == "qualytics-events"

        # Check rules contain both event handlers
        rules = sub["routes"]["rules"]
        rule_paths = [r["path"] for r in rules]
        assert any("anomaly_detected__2_0" in p for p in rule_paths)
        assert any("scan_completed__1_0" in p for p in rule_paths)

        # Check version appears in match expressions
        rule_matches = [r["match"] for r in rules]
        assert any("2.0" in m for m in rule_matches)
        assert any("1.0" in m for m in rule_matches)

        # Default route should be drop
        assert sub["routes"]["default"] == "/events/v1/drop"


class TestEventDropEndpoint:
    """Verify /events/v1/drop returns DROP status."""

    def test_event_drop_endpoint(self, handler_client):
        response = handler_client.post("/events/v1/drop")
        assert response.status_code == 200

        body = response.json()
        assert body["status"] == "DROP"
