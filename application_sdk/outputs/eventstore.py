"""Event store module for handling application events.

This module provides classes and utilities for handling various types of events
in the application, including workflow and activity events.
"""

import asyncio
import json
from abc import ABC
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List

from dapr import clients
from pydantic import BaseModel, Field
from temporalio import activity, workflow

from application_sdk.clients.atlanauth import AtlanAuthClient
from application_sdk.constants import (
    APPLICATION_NAME,
    EVENT_STORE_NAME,
    HTTP_BINDING_NAME,
    IS_EXTERNAL_DEPLOYMENT,
)
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
    WORKER_CREATED = "worker_created"


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
    def _publish_to_external(cls, event: Event):
        """Publish event to external deployment via HTTP binding."""
        payload = json.dumps(event.model_dump(mode="json"))

        # Get authentication token
        auth_token = ""
        try:
            auth_client = AtlanAuthClient()

            # Run async function from sync context
            try:
                auth_token = asyncio.run(auth_client.get_access_token())
            except RuntimeError:
                # Event loop already running, use thread executor
                with ThreadPoolExecutor() as executor:
                    future = executor.submit(
                        lambda: asyncio.run(auth_client.get_access_token())
                    )
                    auth_token = future.result(timeout=5)

        except Exception as e:
            logger.warning(f"Failed to get auth token: {e}")
            auth_token = ""

        # Prepare binding metadata
        binding_metadata = {"content-type": "application/json"}
        if auth_token:
            binding_metadata["Authorization"] = f"Bearer {auth_token}"
            logger.debug(f"Using auth token: {auth_token[:10]}...")
        else:
            logger.warning("No auth token available")

        # Send via DAPR HTTP binding
        with clients.DaprClient() as client:
            client.invoke_binding(
                binding_name=HTTP_BINDING_NAME,
                operation="post",
                data=payload,
                binding_metadata=binding_metadata,
            )
            logger.info(
                f"Published event to external deployment: {event.get_topic_name()}"
            )

    @classmethod
    def _publish_to_internal(cls, event: Event):
        """Publish event to internal pub/sub system."""
        with clients.DaprClient() as client:
            client.publish_event(
                pubsub_name=EVENT_STORE_NAME,
                topic_name=event.get_topic_name(),
                data=json.dumps(event.model_dump(mode="json")),
                data_content_type="application/json",
            )
            logger.info(f"Published event to internal pubsub: {event.get_topic_name()}")

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
        """Publish an event to the appropriate backend.

        Args:
            event (Event): Event data to publish.
            enrich_metadata (bool): Whether to enrich event with context metadata.
        """
        try:
            if enrich_metadata:
                event = cls.enrich_event_metadata(event)

            # Choose publishing method based on deployment type
            if IS_EXTERNAL_DEPLOYMENT:
                cls._publish_to_external(event)
            else:
                cls._publish_to_internal(event)

        except Exception as e:
            logger.error(f"Error publishing event to {event.get_topic_name()}: {e}")
