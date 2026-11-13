"""Base event classes and models.

This module contains the base classes and models for all events in the application.
"""

from abc import ABC
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field
from typing_extensions import deprecated

from application_sdk.constants import (
    APPLICATION_NAME,
    RELEASE_ID,
    WORKER_START_EVENT_VERSION,
)
from application_sdk.version import __version__ as _SDK_VERSION


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
        release_id: Release UUID from Global Marketplace (empty if not injected).
        sdk_version: Installed application-sdk version of the running process
            (from ``application_sdk.__version__``). Always populated — does not
            depend on any env var being injected at image build time.
        created_timestamp: Timestamp when the event was created.
        workflow_type: Type of the workflow.
        workflow_id: ID of the workflow.
        workflow_run_id: Run ID of the workflow.
        workflow_state: State of the workflow.
        activity_type: Type of the activity.
        activity_id: ID of the activity.
        attempt: Attempt number for the activity.
        topic_name: Name of the topic for the event.
        event_id: Identity of the event for consumer-side dedupe, set when the
            event is published. Deterministic inside a workflow: a hash of
            ``workflow_id``, ``workflow_run_id`` and ``event_name`` (plus
            ``activity_id`` and ``attempt`` for ``activity_start`` /
            ``activity_end``, which each attempt emits afresh), so a retried
            ``publish_event`` activity sends the same id. ``workflow_start`` /
            ``workflow_end`` are at-least-once (the activity retries); consumers
            dedupe on this field. Activity events are sent inline and not
            retried. A random UUID outside
            a workflow (``worker_start``, ``token_refresh``).
    """

    application_name: str = Field(init=True, default=APPLICATION_NAME)
    release_id: str = Field(init=True, default=RELEASE_ID)
    sdk_version: str = Field(init=True, default=_SDK_VERSION)
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
    event_id: str | None = Field(init=True, default=None)


@deprecated(
    "application_sdk.contracts.events.EventFilter is deprecated; use "
    "application_sdk.handler.contracts.EventFilterRule instead — will be removed in v4.0."
)
class EventFilter(BaseModel):
    """Filter for events.

    .. deprecated:: 3.58.0
        A v2 event-registration model; nothing reads it. Use
        ``application_sdk.handler.contracts.EventFilterRule``. Will be removed in v4.0.

    Attributes:
        path: Path to filter on.
        operator: Operator to use for filtering.
        value: Value to filter by.
    """

    path: str
    operator: str
    value: str


@deprecated(
    "application_sdk.contracts.events.Consumes is deprecated; use "
    "application_sdk.handler.contracts.EventTriggerConfig instead — will be removed in v4.0."
)
class Consumes(BaseModel):
    """Model for event consumption configuration.

    .. deprecated:: 3.58.0
        A v2 event-registration model; nothing reads it. Use
        ``application_sdk.handler.contracts.EventTriggerConfig``. Will be removed in v4.0.

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
    filters: list[EventFilter] = Field(init=True, default=[])


@deprecated(
    "application_sdk.contracts.events.EventRegistration is deprecated; use "
    "application_sdk.handler.contracts.EventTriggerConfig, one per consumed event, "
    "instead — will be removed in v4.0."
)
class EventRegistration(BaseModel):
    """Model for event registration.

    .. deprecated:: 3.58.0
        A v2 event-registration model; nothing reads it. Use
        ``application_sdk.handler.contracts.EventTriggerConfig``, one per consumed
        event. Will be removed in v4.0.

    Attributes:
        consumes: List of events to consume.
        produces: List of events to produce.
    """

    consumes: list[Consumes] = Field(init=True, default=[])
    produces: list[dict[str, Any]] = Field(init=True, default=[])


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

    data: dict[str, Any]

    def get_topic_name(self):
        """Get the topic name for this event.

        Returns:
            str: The topic name.
        """
        return self.event_type

    model_config = {"extra": "allow"}


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
        max_concurrent_workflow_tasks: Maximum number of in-flight workflow
            task pollers. ``None`` means Temporal's default applies.
        workflow_count: Number of workflow classes registered.
        activity_count: Number of activity functions registered.
        app_version: Semantic version of the app release (ATLAN_APPLICATION_VERSION).
        release_id: Release UUID from Global Marketplace (ATLAN_RELEASE_ID).
        release_channel: Release channel (ATLAN_RELEASE_CHANNEL).
        sdk_version: SDK version used to build the app image (ATLAN_SDK_VERSION).
        app_type: App type from Global Marketplace (ATLAN_APP_TYPE).
        published_at: Release publication timestamp (ATLAN_PUBLISHED_AT).
        commit_sha: Git commit the image was built from, baked into the image
            at build time (app/atlan_build.json). Empty on images built before
            the CI started baking it.
        objectstore_binding_type: Dapr component type of the deployment object
            store (e.g. "bindings.aws.s3", "bindings.localstorage"), discovered
            at runtime from the Dapr sidecar metadata. Empty if undiscoverable.
        upstream_objectstore_binding_type: Dapr component type of the upstream
            (Atlan) object store the app uploads artifacts to, discovered the
            same way. Reported alongside the deployment store so a mis-wired
            upstream binding is visible. Empty if undiscoverable.
        secretstore_binding_type: Dapr component type of the secret store (e.g.
            "secretstores.hashicorp.vault", "secretstores.kubernetes"),
            discovered at runtime from the Dapr sidecar metadata.
    """

    version: str = WORKER_START_EVENT_VERSION
    application_name: str
    deployment_name: str
    task_queue: str
    namespace: str
    host: str
    port: str
    connection_string: str
    max_concurrent_activities: int | None
    max_concurrent_workflow_tasks: int | None = None
    workflow_count: int
    activity_count: int
    build_id: str | None = None
    use_worker_versioning: bool = False
    app_version: str = ""
    release_id: str = ""
    release_channel: str = ""
    sdk_version: str = ""
    app_type: str = ""
    published_at: str = ""
    commit_sha: str = ""
    objectstore_binding_type: str = ""
    upstream_objectstore_binding_type: str = ""
    secretstore_binding_type: str = ""


class WorkerTokenRefreshEventData(BaseModel):
    """Model for token refresh event data.

    The payload of every ``token_refresh`` event: the worker builds the event
    from this model (``execution/_temporal/auth.py``), so it is the wire shape.

    The flag and the three timing fields are strings on the wire, not ``bool``
    and ``float``: that is what the event has always carried, and consumers
    parse them. ``force_refresh`` is ``"true"`` (every refresh fetches a new
    token, so the worker always sends ``"true"``); the timing
    fields are ``str(float)`` renderings of Unix seconds (e.g.
    ``"1781519400.0"``), and ``token_expiry_time`` is ``"0.0"`` when the expiry
    is unknown.

    Attributes:
        application_name: Name of the application the token belongs to.
        deployment_name: Name of the deployment (e.g., dev, staging, prod).
        force_refresh: ``"true"``: the refresh fetched a new token.
        token_expiry_time: Unix timestamp when the new token expires, as a string.
        time_until_expiry: Seconds until the token expires, as a string.
        refresh_timestamp: Unix timestamp when the refresh occurred, as a string.
        app_version: Version of the running app image (see WorkerStartEventData).
            Repeated here so a fleet registry can heal a missed worker_start.
        commit_sha: Git commit of the running app image (see WorkerStartEventData).
    """

    application_name: str
    deployment_name: str
    force_refresh: str
    token_expiry_time: str
    time_until_expiry: str
    refresh_timestamp: str
    app_version: str = ""
    commit_sha: str = ""
