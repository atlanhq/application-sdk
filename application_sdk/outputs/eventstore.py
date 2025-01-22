"""Event store for the application."""

import json
import logging
from datetime import datetime
from typing import Any, Dict

from dapr import clients
from pydantic import BaseModel, Field
from temporalio import activity

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter

activity.logger = AtlanLoggerAdapter(logging.getLogger(__name__))

WORKFLOW_END_EVENT = "workflow_end"
WORKFLOW_START_EVENT = "workflow_start"
ACTIVITY_START_EVENT = "activity_start"
ACTIVITY_END_EVENT = "activity_end"
CUSTOM_EVENT = "custom"


class Event(BaseModel):
    event_type: str = Field(init=False)


class ActivityStartEvent(Event):
    event_type: str = Field(default=ACTIVITY_START_EVENT, init=False)

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class ActivityEndEvent(Event):
    event_type: str = Field(default=ACTIVITY_END_EVENT, init=False)

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class WorkflowEndEvent(Event):
    event_type: str = Field(default=WORKFLOW_END_EVENT, init=False)

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)

    workflow_output: Dict[str, Any] = Field(default_factory=dict)


class WorkflowStartEvent(Event):
    event_type: str = Field(default=WORKFLOW_START_EVENT, init=False)

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)


class CustomEvent(Event):
    event_type: str = Field(default=CUSTOM_EVENT, init=False)
    data: Dict[str, Any] = Field(default_factory=dict)


class AtlanEvent(BaseModel):
    data: WorkflowEndEvent | ActivityEndEvent | ActivityStartEvent | CustomEvent
    datacontenttype: str = Field()
    id: str = Field()
    pubsubname: str = Field()
    source: str = Field()
    specversion: str = Field()
    time: datetime = Field()
    topic: str = Field()
    traceid: str = Field()
    traceparent: str = Field()
    tracestate: str = Field()
    type: str = Field()


class EventStore:
    EVENT_STORE_NAME = "eventstore"
    TOPIC_NAME = "app_events"

    @classmethod
    def create_event(cls, event: Event, topic_name: str = TOPIC_NAME):
        """Create a new generic event.

        Args:
            event (Event): Event data.
            topic_name (str, optional): Topic name to publish the event to. Defaults to TOPIC_NAME.

        Example:
            >>> EventStore.create_generic_event(Event(event_type="test", data={"test": "test"}))
        """
        with clients.DaprClient() as client:
            client.publish_event(
                pubsub_name=cls.EVENT_STORE_NAME,
                topic_name=topic_name,
                data=json.dumps(event.model_dump(mode="json")),
                data_content_type="application/json",
            )

        activity.logger.info(f"Published event to {topic_name}")
