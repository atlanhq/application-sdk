"""Event store module for handling application events.

This module provides classes and utilities for handling various types of events
in the application, including workflow and activity events.
"""

import json
from datetime import datetime
from typing import Any, Dict

from dapr import clients
from pydantic import BaseModel, Field
from temporalio import activity

from application_sdk.common.logger_adaptors import get_logger

logger = get_logger(__name__)
activity.logger = logger

WORKFLOW_END_EVENT = "workflow_end"
WORKFLOW_START_EVENT = "workflow_start"
ACTIVITY_START_EVENT = "activity_start"
ACTIVITY_END_EVENT = "activity_end"
CUSTOM_EVENT = "custom"


class Event(BaseModel):
    """Base class for all events.

    Attributes:
        event_type (str): Type of the event.
    """

    event_type: str = Field(init=False)


class ActivityStartEvent(Event):
    """Event emitted when an activity starts.

    Attributes:
        event_type (str): Always set to ACTIVITY_START_EVENT.
        activity_type (str | None): Type of the activity.
        activity_id (str | None): Unique identifier for the activity.
    """

    event_type: str = Field(default=ACTIVITY_START_EVENT, init=False)

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class ActivityEndEvent(Event):
    """Event emitted when an activity ends.

    Attributes:
        event_type (str): Always set to ACTIVITY_END_EVENT.
        activity_type (str | None): Type of the activity.
        activity_id (str | None): Unique identifier for the activity.
    """

    event_type: str = Field(default=ACTIVITY_END_EVENT, init=False)

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)


class WorkflowEndEvent(Event):
    """Event emitted when a workflow ends.

    Attributes:
        event_type (str): Always set to WORKFLOW_END_EVENT.
        workflow_name (str | None): Name of the workflow.
        workflow_id (str | None): Unique identifier for the workflow.
        workflow_run_id (str | None): Run identifier for the workflow.
        workflow_output (Dict[str, Any]): Output data from the workflow.
    """

    event_type: str = Field(default=WORKFLOW_END_EVENT, init=False)

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)

    workflow_output: Dict[str, Any] = Field(default_factory=dict)


class WorkflowStartEvent(Event):
    """Event emitted when a workflow starts.

    Attributes:
        event_type (str): Always set to WORKFLOW_START_EVENT.
        workflow_name (str | None): Name of the workflow.
        workflow_id (str | None): Unique identifier for the workflow.
        workflow_run_id (str | None): Run identifier for the workflow.
    """

    event_type: str = Field(default=WORKFLOW_START_EVENT, init=False)

    # Workflow information (required)
    workflow_name: str | None = Field(default=None)
    workflow_id: str | None = Field(default=None)
    workflow_run_id: str | None = Field(default=None)


class CustomEvent(Event):
    """Custom event for application-specific events.

    Attributes:
        event_type (str): Always set to CUSTOM_EVENT.
        data (Dict[str, Any]): Custom event data.
    """

    event_type: str = Field(default=CUSTOM_EVENT, init=False)
    data: Dict[str, Any] = Field(default_factory=dict)


class AtlanEvent(BaseModel):
    """Container for Atlan events with metadata.

    Attributes:
        data (Union[WorkflowEndEvent, ActivityEndEvent, ActivityStartEvent, CustomEvent]): Event data.
        datacontenttype (str): Content type of the event data.
        id (str): Unique identifier for the event.
        pubsubname (str): Name of the pub/sub system.
        source (str): Source of the event.
        specversion (str): Version of the event specification.
        time (datetime): Timestamp of the event.
        topic (str): Topic the event was published to.
        traceid (str): Trace identifier for distributed tracing.
        traceparent (str): Parent trace information.
        tracestate (str): Additional trace state.
        type (str): Type of the event.
    """

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
    """Event store for publishing application events.

    This class provides functionality to publish events to a pub/sub system.

    Attributes:
        EVENT_STORE_NAME (str): Name of the event store binding.
        TOPIC_NAME (str): Default topic name for events.
    """

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

        logger.info(f"Published event to {topic_name}")
