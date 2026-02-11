# Request/Response DTOs for workflows

from enum import Enum
from typing import Any, Callable, Coroutine, Dict, List, Optional, Type, Union

from pydantic import BaseModel, Field, RootModel

from application_sdk.interceptors.models import Event, EventFilter
from application_sdk.workflows import WorkflowInterface


class TestAuthRequest(RootModel[Dict[str, Any]]):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing database credentials"
    )


class TestAuthResponse(BaseModel):
    success: bool
    message: str


class MetadataType(str, Enum):
    DATABASE = "database"
    SCHEMA = "schema"
    ALL = "all"


class FetchMetadataRequest(RootModel[Dict[str, Any]]):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing the metadata and credentials"
    )


class FetchMetadataResponse(BaseModel):
    success: bool
    data: Any


class PreflightCheckRequest(BaseModel):
    credentials: Dict[str, Any] = Field(
        ..., description="Required JSON field containing database credentials"
    )
    metadata: Dict[str, Any] = Field(
        ...,
        description="Required JSON field containing form data for filtering and configuration",
    )

    class Config:
        schema_extra = {
            "example": {
                "credentials": {
                    "authType": "basic",
                    "host": "host",
                    "port": 5432,
                    "username": "username",
                    "password": "password",
                    "database": "databasename",
                },
                "metadata": {
                    "include-filter": '{"^dbengine$":["^public$","^airflow$"]}',
                    "exclude-filter": "{}",
                    "temp-table-regex": "",
                },
            }
        }


class PreflightCheckResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates if the overall operation was successful"
    )
    data: Dict[str, Any] = Field(..., description="Response data")

    class Config:
        schema_extra = {
            "example": {
                "success": True,
                "data": {
                    "successMessage": "Successfully checked",
                    "failureMessage": "",
                },
            }
        }


class WorkflowRequest(RootModel[Dict[str, Any]]):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing workflow configuration"
    )

    class Config:
        schema_extra = {
            "example": {
                "miner_args": {},
                "credentials": {
                    "authType": "basic",
                    "host": "",
                    "port": 5432,
                    "username": "username",
                    "password": "password",
                    "database": "databasename",
                },
                "connection": {"connection": "dev"},
                "metadata": {
                    "include-filter": '{"^dbengine$":["^public$","^airflow$"]}',
                    "exclude-filter": "{}",
                    "temp-table-regex": "",
                },
            }
        }


class EventWorkflowRequest(BaseModel):
    event: Event = Field(alias="data", description="Event object")
    datacontenttype: str = Field(
        alias="datacontenttype", description="Data content type"
    )
    id: str = Field(alias="id", description="Event ID")
    source: str = Field(alias="source", description="Event source")
    specversion: str = Field(alias="specversion", description="Event spec version")
    time: str = Field(alias="time", description="Event time")
    type: str = Field(alias="type", description="Event type")
    topic: str = Field(alias="topic", description="Event topic")


class WorkflowData(BaseModel):
    workflow_id: str = Field(..., description="Unique identifier for the workflow")
    run_id: str = Field(..., description="Unique identifier for the workflow run")


class WorkflowResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: WorkflowData = Field(..., description="Details about the workflow and run")

    class Config:
        schema_extra = {
            "example": {
                "success": True,
                "message": "Workflow started successfully",
                "data": {
                    "workflow_id": "4b805f36-48c5-4dd3-942f-650e06f75bbc",
                    "run_id": "efe16ffe-24b2-4391-a7ec-7000c32c5893",
                },
            }
        }


class EventWorkflowResponse(WorkflowResponse):
    class Status(str, Enum):
        SUCCESS = "SUCCESS"
        RETRY = "RETRY"
        DROP = "DROP"

    # This should be a string enum of the status of the workflow, based on the Dapr docs
    # https://docs.dapr.io/reference/api/pubsub_api/#expected-http-response
    status: Status = Field(..., description="Status of the workflow")


class WorkflowConfigRequest(RootModel[Dict[str, Any]]):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing workflow configuration"
    )


class WorkflowConfigResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: Dict[str, Any] = Field(..., description="Workflow configuration")

    class Config:
        schema_extra = {
            "example": {
                "success": True,
                "message": "Workflow configuration fetched successfully",
                "data": {
                    "credential_guid": "credential_test-uuid",
                    "connection": {"connection": "dev"},
                    "metadata": {
                        "include-filter": '{"^dbengine$":["^public$","^airflow$"]}',
                        "exclude-filter": "{}",
                        "temp-table-regex": "",
                    },
                },
            }
        }


class ConfigMapResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: Dict[str, Any] = Field(..., description="Configuration map object")

    class Config:
        schema_extra = {
            "example": {
                "success": True,
                "message": "Configuration map fetched successfully",
                "data": {
                    "config_map_id": "pikachu-config-001",
                    "name": "Pikachu Configuration",
                    "settings": {
                        "electric_type": True,
                        "level": 25,
                        "moves": ["Thunderbolt", "Quick Attack"],
                    },
                },
            }
        }


class AddScheduleRequest(BaseModel):
    schedule_id: str = Field(..., description="Unique identifier for the schedule")
    cron_expression: str = Field(
        ..., description="Cron expression (e.g. '0 9 * * MON-FRI')"
    )
    workflow_args: Dict[str, Any] = Field(
        default_factory=dict, description="Arguments to pass to the workflow"
    )
    note: Optional[str] = Field(
        None, description="Human-readable note about this schedule"
    )
    start_at: Optional[str] = Field(
        None, description="ISO 8601 datetime to start the schedule"
    )
    end_at: Optional[str] = Field(
        None, description="ISO 8601 datetime to end the schedule"
    )
    jitter: Optional[int] = Field(
        None, description="Random jitter in seconds added to each trigger time"
    )
    workflow_class_name: Optional[str] = Field(
        None,
        description="Workflow class name (for multi-workflow apps, defaults to first registered)",
    )

    class Config:
        schema_extra = {
            "example": {
                "schedule_id": "daily-extraction",
                "cron_expression": "0 9 * * MON-FRI",
                "workflow_args": {"metadata": {"include-filter": "{}"}},
                "note": "Weekday morning extraction",
            }
        }


class ScheduleData(BaseModel):
    schedule_id: str = Field(..., description="Unique identifier for the schedule")


class ScheduleResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: ScheduleData = Field(..., description="Details about the schedule")


class ScheduleDetailsData(BaseModel):
    schedule_id: str = Field(..., description="Unique identifier for the schedule")
    cron_expression: str = Field(..., description="Cron expression for the schedule")
    paused: bool = Field(..., description="Whether the schedule is currently paused")
    note: Optional[str] = Field(None, description="Human-readable note")
    workflow_args: Dict[str, Any] = Field(
        default_factory=dict, description="Arguments passed to the workflow"
    )
    recent_actions: List[Dict[str, Any]] = Field(
        default_factory=list, description="Recent schedule executions"
    )
    next_action_times: List[str] = Field(
        default_factory=list, description="Upcoming scheduled execution times"
    )


class ScheduleDetailsResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: ScheduleDetailsData = Field(..., description="Schedule details")


class ScheduleListItem(BaseModel):
    schedule_id: str = Field(..., description="Unique identifier for the schedule")
    paused: bool = Field(..., description="Whether the schedule is currently paused")
    note: Optional[str] = Field(None, description="Human-readable note")
    cron_expression: Optional[str] = Field(None, description="Cron expression")


class ListSchedulesResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: List[ScheduleListItem] = Field(..., description="List of schedules")


class EditScheduleRequest(BaseModel):
    cron_expression: Optional[str] = Field(None, description="New cron expression")
    workflow_args: Optional[Dict[str, Any]] = Field(
        None, description="New workflow arguments"
    )
    note: Optional[str] = Field(None, description="New note")
    paused: Optional[bool] = Field(
        None, description="Set to true to pause, false to unpause"
    )

    class Config:
        schema_extra = {
            "example": {
                "cron_expression": "0 10 * * MON-FRI",
                "note": "Changed to 10am",
            }
        }


class DeleteScheduleResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )


class WorkflowTrigger(BaseModel):
    workflow_class: Optional[Type[WorkflowInterface]] = None
    model_config = {"arbitrary_types_allowed": True}


class HttpWorkflowTrigger(WorkflowTrigger):
    endpoint: str = "/start"
    methods: List[str] = ["POST"]


class EventWorkflowTrigger(WorkflowTrigger):
    event_id: str
    event_type: str
    event_name: str
    event_filters: List[EventFilter]

    def should_trigger_workflow(self, event: Event) -> bool:
        return True


class Subscription(BaseModel):
    """Subscription configuration for Dapr messaging.

    Attributes:
        component_name: Name of the Dapr pubsub component
        topic: Topic to subscribe to
        route: Route path for the message handler endpoint
        handler: Required callback function to handle incoming messages
        bulk_config: Optional bulk subscribe configuration
        dead_letter_topic: Optional dead letter topic for failed messages

    Nested Classes:
        BulkConfig: Configuration for bulk message processing
        MessageStatus: Status codes for handler responses (SUCCESS, RETRY, DROP)
    """

    class BulkConfig(BaseModel):
        """Bulk configuration for Dapr messaging.

        Attributes:
            enabled: Whether bulk subscribe is enabled
            max_messages_count: Maximum number of messages to receive in a batch
            max_await_duration_ms: Maximum time to wait for messages in milliseconds
        """

        enabled: bool = False
        max_messages_count: int = Field(
            default=100, serialization_alias="maxMessagesCount"
        )
        max_await_duration_ms: int = Field(
            default=40, serialization_alias="maxAwaitDurationMs"
        )

    class MessageStatus(str, Enum):
        """Status codes for Dapr pub/sub subscription message handler responses.

        Used in subscription handler responses to indicate how Dapr should handle the message.
        Based on Dapr docs: https://docs.dapr.io/reference/api/pubsub_api/#expected-http-response

        Attributes:
            SUCCESS: Message was processed successfully.
            RETRY: Message processing failed, should be retried.
            DROP: Message should be dropped (sent to dead letter topic if configured).
        """

        SUCCESS = "SUCCESS"
        RETRY = "RETRY"
        DROP = "DROP"

    model_config = {"arbitrary_types_allowed": True}

    component_name: str
    topic: str
    route: str
    handler: Union[
        Callable[[Any], Any], Callable[[Any], Coroutine[Any, Any, Any]]
    ]  # Required callback function (sync or async)
    bulk_config: Optional[BulkConfig] = None
    dead_letter_topic: Optional[str] = None
