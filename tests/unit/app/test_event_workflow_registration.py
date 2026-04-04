"""Tests for @on_event wiring into App.__init_subclass__.

Verifies that @on_event-decorated methods on an App class are automatically
registered as dynamic Temporal workflow subclasses in both AppRegistry and
EventRegistry.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.base import App
from application_sdk.app.event import on_event
from application_sdk.app.event_registry import EventRegistry
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.contracts.base import Input, Output

# =============================================================================
# Test contracts
# =============================================================================


@dataclass
class MainInput(Input):
    value: str = ""


@dataclass
class MainOutput(Output):
    result: str = ""


@dataclass
class AnomalyInput(Input):
    anomaly_id: str = ""


@dataclass
class AnomalyOutput(Output):
    handled: bool = False


@dataclass
class AlertInput(Input):
    alert_id: str = ""


@dataclass
class AlertOutput(Output):
    notified: bool = False


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


# =============================================================================
# Tests
# =============================================================================


class TestEventHandlersRegisteredInEventRegistry:
    """App with 2 @on_event methods: verify both appear in EventRegistry."""

    def test_event_handlers_registered_in_event_registry(self) -> None:
        class QualyticsApp(App):
            name = "qualytics-app"

            @on_event(topic="atlas_events", event_name="anomaly_detected")
            async def handle_anomaly(self, input: AnomalyInput) -> AnomalyOutput:
                return AnomalyOutput(handled=True)

            @on_event(topic="atlas_events", event_name="alert_fired", version="2.0")
            async def handle_alert(self, input: AlertInput) -> AlertOutput:
                return AlertOutput(notified=True)

            async def run(self, input: MainInput) -> MainOutput:
                return MainOutput(result="ok")

        registry = EventRegistry.get_instance()

        # Both handlers should be registered
        handlers = registry.get_by_app("qualytics-app")
        assert len(handlers) == 2

        event_ids = {h.event_id for h in handlers}
        assert "anomaly_detected__1_0" in event_ids
        assert "alert_fired__2_0" in event_ids

        # Verify workflow names
        workflow_names = {h.workflow_name for h in handlers}
        assert "qualytics-app.handle-anomaly" in workflow_names
        assert "qualytics-app.handle-alert" in workflow_names


class TestEventHandlerWorkflowsRegisteredInAppRegistry:
    """Verify the dynamic workflow classes are in AppRegistry."""

    def test_event_handler_workflows_registered_in_app_registry(self) -> None:
        class EventApp(App):
            name = "event-app"

            @on_event(topic="events", event_name="thing_happened")
            async def handle_thing(self, input: AnomalyInput) -> AnomalyOutput:
                return AnomalyOutput(handled=True)

            async def run(self, input: MainInput) -> MainOutput:
                return MainOutput(result="ok")

        app_registry = AppRegistry.get_instance()

        # The parent app should be registered
        parent_meta = app_registry.get("event-app")
        assert parent_meta.name == "event-app"

        # The dynamic event workflow should also be registered
        event_meta = app_registry.get("event-app.handle-thing")
        assert event_meta.name == "event-app.handle-thing"
        assert event_meta.app_cls is not EventApp  # It's a dynamic subclass
        assert issubclass(event_meta.app_cls, EventApp)


class TestEventHandlerWorkflowHasCorrectInputOutputTypes:
    """Verify input/output types match the event handler's signature."""

    def test_event_handler_workflow_has_correct_input_output_types(self) -> None:
        class TypedApp(App):
            name = "typed-app"

            @on_event(topic="events", event_name="anomaly")
            async def handle_anomaly(self, input: AnomalyInput) -> AnomalyOutput:
                return AnomalyOutput(handled=True)

            async def run(self, input: MainInput) -> MainOutput:
                return MainOutput(result="ok")

        app_registry = AppRegistry.get_instance()

        # Parent app has MainInput/MainOutput
        parent_meta = app_registry.get("typed-app")
        assert parent_meta.input_type is MainInput
        assert parent_meta.output_type is MainOutput

        # Event handler workflow has AnomalyInput/AnomalyOutput
        event_meta = app_registry.get("typed-app.handle-anomaly")
        assert event_meta.input_type is AnomalyInput
        assert event_meta.output_type is AnomalyOutput


class TestAppWithoutEventsStillWorks:
    """Plain App with no @on_event methods still registers normally."""

    def test_app_without_events_still_works(self) -> None:
        class PlainApp(App):
            name = "plain-app"

            async def run(self, input: MainInput) -> MainOutput:
                return MainOutput(result="ok")

        app_registry = AppRegistry.get_instance()
        event_registry = EventRegistry.get_instance()

        # App should be registered normally
        meta = app_registry.get("plain-app")
        assert meta.name == "plain-app"
        assert meta.input_type is MainInput
        assert meta.output_type is MainOutput

        # No event handlers should be registered
        handlers = event_registry.get_by_app("plain-app")
        assert len(handlers) == 0
