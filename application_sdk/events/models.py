"""Base event classes and models.

This module contains the base classes and models for all events in the application.
"""

from abc import ABC
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

from application_sdk.constants import APPLICATION_NAME, WORKER_START_EVENT_VERSION


class EventTypes(Enum):
    """Enumeration of event types."""

    APPLICATION_EVENT = "application_events"


class ApplicationEventNames(Enum):
    """Enumeration of application event names."""

    WORKFLOW_END = "workflow_end"
    WORKFLOW_START = "workflow_start"
    ACTIVITY_START = "activity_start"
    ACTIVITY_END = "activity_end"
    WORKER_START = "worker_start"
    WORKER_END = "worker_end"
    APPLICATION_START = "application_start"
    APPLICATION_END = "application_end"
    TOKEN_REFRESH = "token_refresh"


class WorkflowStates(Enum):
    """Enumeration of workflow states."""

    UNKNOWN = "unknown"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class EventMetadata(BaseModel):
    """Metadata for events.

    Attributes:
        application_name: Name of the application the event belongs to.
        created_timestamp: Timestamp when the event was created.
        workflow_type: Type of the workflow.
        workflow_id: ID of the workflow.
        workflow_run_id: Run ID of the workflow.
        workflow_state: State of the workflow.
        activity_type: Type of the activity.
        activity_id: ID of the activity.
        attempt: Attempt number for the activity.
        topic_name: Name of the topic for the event.
    """

    application_name: str = Field(init=True, default=APPLICATION_NAME)
    created_timestamp: int = Field(init=True, default=0)

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
    """Filter for events.

    Attributes:
        path: Path to filter on.
        operator: Operator to use for filtering.
        value: Value to filter by.
    """

    path: str
    operator: str
    value: str


class Consumes(BaseModel):
    """Model for event consumption configuration.

    Attributes:
        event_id: ID of the event.
        event_type: Type of the event.
        event_name: Name of the event.
        version: Version of the event.
        filters: List of filters to apply.
    """

    event_id: str = Field(alias="eventId")
    event_type: str = Field(alias="eventType")
    event_name: str = Field(alias="eventName")
    version: str = Field()
    filters: List[EventFilter] = Field(init=True, default=[])


class EventRegistration(BaseModel):
    """Model for event registration.

    Attributes:
        consumes: List of events to consume.
        produces: List of events to produce.
    """

    consumes: List[Consumes] = Field(init=True, default=[])
    produces: List[Dict[str, Any]] = Field(init=True, default=[])


class Event(BaseModel, ABC):
    """Base class for all events.

    Attributes:
        metadata: Metadata for the event.
        event_type: Type of the event.
        event_name: Name of the event.
        data: Data payload of the event.
    """

    metadata: EventMetadata = Field(init=True, default_factory=EventMetadata)

    event_type: str
    event_name: str

    data: Dict[str, Any]

    def get_topic_name(self):
        """Get the topic name for this event.

        Returns:
            str: The topic name.
        """
        return self.event_type

    class Config:
        extra = "allow"


class WorkerStartEventData(BaseModel):
    """Model for worker creation event data.

    This model represents the data structure used when publishing worker creation events.
    It contains information about the worker configuration and environment.

    Attributes:
        application_name: Name of the application the worker belongs to.
        deployment_name: Name of the deployment the worker belongs to.
        task_queue: Task queue name for the worker.
        namespace: Temporal namespace for the worker.
        host: Host address of the Temporal server.
        port: Port number of the Temporal server.
        connection_string: Connection string for the Temporal server.
        max_concurrent_activities: Maximum number of concurrent activities.
        workflow_count: Number of workflow classes registered.
        activity_count: Number of activity functions registered.
    """

    version: str = WORKER_START_EVENT_VERSION
    application_name: str
    deployment_name: str
    task_queue: str
    namespace: str
    host: str
    port: str
    connection_string: str
    max_concurrent_activities: Optional[int]
    workflow_count: int
    activity_count: int


class WorkerTokenRefreshEventData(BaseModel):
    """Model for token refresh event data.

    This model represents the data structure used when publishing token refresh events.
    It contains information about the token refresh operation and agent status.

    Attributes:
        application_name: Name of the application the token belongs to.
        deployment_name: Name of the deployment (e.g., dev, staging, prod).
        force_refresh: Whether this was a forced refresh or automatic.
        token_expiry_time: Unix timestamp when the new token expires.
        time_until_expiry: Seconds until token expires.
        refresh_timestamp: Unix timestamp when the refresh occurred.
    """

    application_name: str
    deployment_name: str
    force_refresh: bool
    token_expiry_time: float
    time_until_expiry: float
    refresh_timestamp: float
