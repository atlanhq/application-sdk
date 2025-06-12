"""Event store module for handling application events.

This module provides classes and utilities for handling various types of events
in the application, including workflow and activity events.
"""

import json
from abc import ABC
from enum import Enum
from typing import Any, Dict

from dapr import clients
from pydantic import BaseModel, Field
from temporalio import activity

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class EventTypes(Enum):
    APPLICATION_EVENT = "application_events"


class ApplicationEventNames(Enum):
    WORKFLOW_END = "workflow_end"
    WORKFLOW_START = "workflow_start"
    ACTIVITY_START = "activity_start"
    ACTIVITY_END = "activity_end"


class WorkflowStates(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ActivityStates(Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EventMetadata(BaseModel):
    application_name: str = Field()
    event_published_client_timestamp: int = Field()

    # Workflow information
    workflow_name: str | None = Field()
    workflow_id: str | None = Field()
    workflow_run_id: str | None = Field()
    workflow_state: str | None = Field()


class Event(BaseModel, ABC):
    """Base class for all events.

    Attributes:
        event_type (str): Type of the event.
    """

    metadata: EventMetadata

    event_type: str
    event_name: str

    def get_topic_name(self):
        return self.event_type + "_topic"


class ApplicationEvent(Event):
    event_type: str = EventTypes.APPLICATION_EVENT.value


class ActivityStartEvent(ApplicationEvent):
    """Event emitted when an activity starts.

    Attributes:
        event_type (str): Always set to ACTIVITY_START_EVENT.
        activity_type (str | None): Type of the activity.
        activity_id (str | None): Unique identifier for the activity.
    """

    event_name: str = ApplicationEventNames.ACTIVITY_START.value

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)
    activity_state: str | None = Field(default=None, init=False)


class ActivityEndEvent(ApplicationEvent):
    """Event emitted when an activity ends.

    Attributes:
        event_type (str): Always set to ACTIVITY_END_EVENT.
        activity_type (str | None): Type of the activity.
        activity_id (str | None): Unique identifier for the activity.
    """

    event_name: str = ApplicationEventNames.ACTIVITY_END.value

    # Activity information (required)
    activity_type: str | None = Field(default=None, init=False)
    activity_id: str | None = Field(default=None, init=False)
    activity_state: str | None = Field(default=None, init=False)


class WorkflowEndEvent(ApplicationEvent):
    """Event emitted when a workflow ends.

    Attributes:
        event_type (str): Always set to WORKFLOW_END_EVENT.
        workflow_name (str | None): Name of the workflow.
        workflow_id (str | None): Unique identifier for the workflow.
        workflow_run_id (str | None): Run identifier for the workflow.
        workflow_output (Dict[str, Any]): Output data from the workflow.
    """

    event_name: str = ApplicationEventNames.WORKFLOW_END.value

    workflow_output: Dict[str, Any] = Field(default_factory=dict)


class WorkflowStartEvent(ApplicationEvent):
    """Event emitted when a workflow starts.

    Attributes:
        event_type (str): Always set to WORKFLOW_START_EVENT.
        workflow_name (str | None): Name of the workflow.
        workflow_id (str | None): Unique identifier for the workflow.
        workflow_run_id (str | None): Run identifier for the workflow.
    """

    event_name: str = ApplicationEventNames.WORKFLOW_START.value


class EventStore:
    """Event store for publishing application events.

    This class provides functionality to publish events to a pub/sub system.

    Attributes:
        EVENT_STORE_NAME (str): Name of the event store binding.
        TOPIC_NAME (str): Default topic name for events.
    """

    EVENT_STORE_NAME = "eventstore"

    @classmethod
    def publish_event(cls, event: Event):
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
                topic_name=event.get_topic_name(),
                data=json.dumps(event.model_dump(mode="json")),
                data_content_type="application/json",
            )

        logger.info(f"Published event to {event.get_topic_name()}")
