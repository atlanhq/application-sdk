"""Tests for EventRegistry singleton."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from application_sdk.app.event import EventHandlerMetadata
from application_sdk.app.event_registry import EventRegistry, RegisteredEventHandler
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.events import EventFilter

# -- Dummy Input/Output for metadata ----------------------------------------


@dataclass
class _DummyInput(Input):
    value: str = ""


@dataclass
class _DummyOutput(Output):
    result: str = ""


async def _dummy_handler(self, input: _DummyInput) -> _DummyOutput:
    return _DummyOutput()


# -- Fixtures ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_registry():
    """Reset EventRegistry before each test."""
    EventRegistry.reset()
    yield
    EventRegistry.reset()


def _make_metadata(
    *,
    topic: str = "test-events",
    event_name: str = "test_event",
    version: str = "1.0",
    pre_filters: list[EventFilter] | None = None,
) -> EventHandlerMetadata:
    return EventHandlerMetadata(
        topic=topic,
        event_name=event_name,
        version=version,
        func=_dummy_handler,
        input_type=_DummyInput,
        output_type=_DummyOutput,
        pre_filters=pre_filters or [],
    )


# -- Singleton ---------------------------------------------------------------


class TestSingleton:
    def test_get_instance_returns_same_object(self):
        a = EventRegistry.get_instance()
        b = EventRegistry.get_instance()
        assert a is b

    def test_reset_clears_handlers(self):
        reg = EventRegistry.get_instance()
        reg.register("app", _make_metadata(), "app.workflow")
        assert len(reg.list_all()) == 1
        EventRegistry.reset()
        assert len(reg.list_all()) == 0


# -- register + get ----------------------------------------------------------


class TestRegisterAndGet:
    def test_register_and_get(self):
        reg = EventRegistry.get_instance()
        meta = _make_metadata(event_name="anomaly_detected", version="2.0")
        entry = reg.register("qualytics-app", meta, "qualytics-app.handle-anomaly")

        assert isinstance(entry, RegisteredEventHandler)
        assert entry.app_name == "qualytics-app"
        assert entry.workflow_name == "qualytics-app.handle-anomaly"
        assert entry.event_id == "anomaly_detected__2_0"
        assert entry.topic == "test-events"
        assert entry.event_name == "anomaly_detected"
        assert entry.version == "2.0"
        assert entry.pre_filters == []

        retrieved = reg.get("anomaly_detected__2_0")
        assert retrieved is entry

    def test_get_returns_none_for_unknown(self):
        reg = EventRegistry.get_instance()
        assert reg.get("nonexistent__1_0") is None

    def test_duplicate_event_id_raises(self):
        reg = EventRegistry.get_instance()
        meta = _make_metadata(event_name="dup_event", version="1.0")
        reg.register("app-a", meta, "app-a.workflow")

        meta2 = _make_metadata(event_name="dup_event", version="1.0")
        with pytest.raises(ValueError, match="already registered"):
            reg.register("app-b", meta2, "app-b.workflow")


# -- get_by_app --------------------------------------------------------------


class TestGetByApp:
    def test_get_by_app(self):
        reg = EventRegistry.get_instance()
        reg.register("app-a", _make_metadata(event_name="ev1"), "app-a.wf1")
        reg.register("app-a", _make_metadata(event_name="ev2"), "app-a.wf2")
        reg.register("app-b", _make_metadata(event_name="ev3"), "app-b.wf3")

        app_a_handlers = reg.get_by_app("app-a")
        assert len(app_a_handlers) == 2
        assert all(h.app_name == "app-a" for h in app_a_handlers)

        app_b_handlers = reg.get_by_app("app-b")
        assert len(app_b_handlers) == 1

    def test_get_by_app_empty(self):
        reg = EventRegistry.get_instance()
        assert reg.get_by_app("nonexistent") == []


# -- list_all ----------------------------------------------------------------


class TestListAll:
    def test_list_all(self):
        reg = EventRegistry.get_instance()
        reg.register("a", _make_metadata(event_name="e1"), "a.w1")
        reg.register("b", _make_metadata(event_name="e2"), "b.w2")
        reg.register("a", _make_metadata(event_name="e3"), "a.w3")

        all_handlers = reg.list_all()
        assert len(all_handlers) == 3

    def test_list_all_empty(self):
        reg = EventRegistry.get_instance()
        assert reg.list_all() == []


# -- generate_dapr_subscriptions ---------------------------------------------


class TestGenerateDaprSubscriptions:
    def test_basic_subscription(self):
        reg = EventRegistry.get_instance()
        reg.register(
            "qualytics-app",
            _make_metadata(
                topic="qualytics-events",
                event_name="anomaly_detected",
                version="2.0",
            ),
            "qualytics-app.handle-anomaly",
        )

        subs = reg.generate_dapr_subscriptions()
        assert len(subs) == 1

        sub = subs[0]
        assert sub["pubsubname"] == "eventstore"
        assert sub["topic"] == "qualytics-events"
        assert sub["routes"]["default"] == "/events/v1/drop"

        rules = sub["routes"]["rules"]
        assert len(rules) == 1
        assert rules[0]["path"] == "/events/v1/event/anomaly_detected__2_0"
        assert "event.data.event_name == 'anomaly_detected'" in rules[0]["match"]
        assert "event.data.version == '2.0'" in rules[0]["match"]

    def test_subscription_with_pre_filters(self):
        reg = EventRegistry.get_instance()
        filters = [
            EventFilter(path="source", operator="==", value="qualytics"),
            EventFilter(path="severity", operator=">=", value="high"),
        ]
        reg.register(
            "my-app",
            _make_metadata(
                topic="events",
                event_name="alert",
                version="1.0",
                pre_filters=filters,
            ),
            "my-app.handle-alert",
        )

        subs = reg.generate_dapr_subscriptions()
        rule = subs[0]["routes"]["rules"][0]
        assert "event.data.source == 'qualytics'" in rule["match"]
        assert "event.data.severity >= 'high'" in rule["match"]
        # All conditions joined with &&
        assert rule["match"].count("&&") == 3  # 4 conditions, 3 &&'s

    def test_multiple_handlers_same_topic(self):
        reg = EventRegistry.get_instance()
        reg.register(
            "app",
            _make_metadata(topic="shared-topic", event_name="ev1", version="1.0"),
            "app.wf1",
        )
        reg.register(
            "app",
            _make_metadata(topic="shared-topic", event_name="ev2", version="1.0"),
            "app.wf2",
        )

        subs = reg.generate_dapr_subscriptions()
        assert len(subs) == 1  # grouped by topic
        assert len(subs[0]["routes"]["rules"]) == 2

    def test_multiple_topics(self):
        reg = EventRegistry.get_instance()
        reg.register(
            "app",
            _make_metadata(topic="topic-a", event_name="ev1"),
            "app.wf1",
        )
        reg.register(
            "app",
            _make_metadata(topic="topic-b", event_name="ev2"),
            "app.wf2",
        )

        subs = reg.generate_dapr_subscriptions()
        assert len(subs) == 2
        topics = {s["topic"] for s in subs}
        assert topics == {"topic-a", "topic-b"}

    def test_empty_registry_returns_empty(self):
        reg = EventRegistry.get_instance()
        assert reg.generate_dapr_subscriptions() == []
