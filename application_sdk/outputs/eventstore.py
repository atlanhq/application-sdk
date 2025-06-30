"""Event store module for handling application events.

This module provides classes and utilities for handling various types of events
in the application, including workflow and activity events.
"""

import json
from abc import ABC
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List

from dapr import clients
from pydantic import BaseModel, Field
from temporalio import activity, workflow

from application_sdk.constants import APPLICATION_NAME, EVENT_STORE_NAME
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger


class EventTypes(Enum):
    APPLICATION_EVENT = "application_event"


class ApplicationEventNames(Enum):
    WORKFLOW_END = "workflow_end"
    WORKFLOW_START = "workflow_start"
    ACTIVITY_START = "activity_start"
    ACTIVITY_END = "activity_end"


class WorkflowStates(Enum):
    UNKNOWN = "unknown"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EventMetadata(BaseModel):
    application_name: str = Field(init=True, default=APPLICATION_NAME)
    event_published_client_timestamp: int = Field(init=True, default=0)

    # Workflow information
    workflow_type: str | None = Field(init=True, default=None)
    workflow_id: str | None = Field(init=True, default=None)
    workflow_run_id: str | None = Field(init=True, default=None)
    workflow_state: str | None = Field(init=True, default=WorkflowStates.UNKNOWN.value)

    # Activity information (Only when in an activity flow)
    activity_type: str | None = Field(init=True, default=None)
    activity_id: str | None = Field(init=True, default=None)
    attempt: int | None = Field(init=True, default=None)

    topic_name: str | None = Field(init=False, default=None)


class EventFilter(BaseModel):
    path: str
    operator: str
    value: str


class Consumes(BaseModel):
    event_id: str = Field(alias="eventId")
    event_type: str = Field(alias="eventType")
    event_name: str = Field(alias="eventName")
    version: str = Field()
    filters: List[EventFilter] = Field(init=True, default=[])


class EventRegistration(BaseModel):
    consumes: List[Consumes] = Field(init=True, default=[])
    produces: List[Dict[str, Any]] = Field(init=True, default=[])


class Event(BaseModel, ABC):
    """Base class for all events.

    Attributes:
        event_type (str): Type of the event.
    """

    metadata: EventMetadata = Field(init=True, default_factory=EventMetadata)

    event_type: str
    event_name: str

    data: Dict[str, Any]

    def get_topic_name(self):
        return self.event_type + "_topic"

    class Config:
        extra = "allow"


class EventStore:
    """Event store for publishing application events.

    This class provides functionality to publish events to a pub/sub system.
    """

    @classmethod
    def enrich_event_metadata(cls, event: Event):
        """Enrich the event metadata with the workflow and activity information.

        Args:
            event (Event): Event data.

        """
        if not event.metadata:
            event.metadata = EventMetadata()

        event.metadata.application_name = APPLICATION_NAME
        event.metadata.event_published_client_timestamp = int(
            datetime.now().timestamp()
        )
        event.metadata.topic_name = event.get_topic_name()

        try:
            workflow_info = workflow.info()
            if workflow_info:
                event.metadata.workflow_type = workflow_info.workflow_type
                event.metadata.workflow_id = workflow_info.workflow_id
                event.metadata.workflow_run_id = workflow_info.run_id
        except Exception:
            logger.debug("Not in workflow context, cannot set workflow metadata")

        try:
            activity_info = activity.info()
            if activity_info:
                event.metadata.activity_type = activity_info.activity_type
                event.metadata.activity_id = activity_info.activity_id
                event.metadata.attempt = activity_info.attempt
                event.metadata.workflow_type = activity_info.workflow_type
                event.metadata.workflow_id = activity_info.workflow_id
                event.metadata.workflow_run_id = activity_info.workflow_run_id
                event.metadata.workflow_state = WorkflowStates.RUNNING.value
        except Exception:
            logger.debug("Not in activity context, cannot set activity metadata")

        return event

    @classmethod
    def publish_event(cls, event: Event, enrich_metadata: bool = True):
        """Create a new generic event.

        Args:
            event (Event): Event data.
            topic_name (str, optional): Topic name to publish the event to. Defaults to TOPIC_NAME.

        Example:
            >>> EventStore.create_generic_event(Event(event_type="test", data={"test": "test"}))
        """
        try:
            if enrich_metadata:
                event = cls.enrich_event_metadata(event)

            with clients.DaprClient() as client:
                client.publish_event(
                    pubsub_name=EVENT_STORE_NAME,
                    topic_name=event.get_topic_name(),
                    data=json.dumps(event.model_dump(mode="json")),
                    data_content_type="application/json",
                )
                logger.info(f"Published event to {event.get_topic_name()}")
        except Exception as e:
            logger.error(f"Error publishing event to {event.get_topic_name()}: {e}")
