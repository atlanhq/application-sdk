# Request/Response DTOs for workflows

from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Type

from pydantic import BaseModel, Field, RootModel

from application_sdk.outputs.eventstore import WorkflowEndEvent
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
    data: List[Dict[str, str]]


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


class WorkflowConfigRequest(BaseModel):
    credential_guid: Optional[str] = Field(
        default=None, description="Optional GUID field containing database credentials"
    )
    connection: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional JSON field containing connection configuration",
    )
    metadata: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Optional JSON field containing metadata configuration",
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


class WorkflowTrigger(BaseModel):
    workflow_class: Optional[Type[WorkflowInterface]] = None
    model_config = {"arbitrary_types_allowed": True}


class HttpWorkflowTrigger(WorkflowTrigger):
    endpoint: str = "/start"
    methods: List[str] = ["POST"]


class EventWorkflowTrigger(WorkflowTrigger):
    should_trigger_workflow: Callable[[Any], bool]


class WorkflowEndEventTrigger(EventWorkflowTrigger):
    finished_workflow_name: str
    finished_workflow_state: str

    should_trigger_workflow: Callable[[Any], bool]

    def __init__(
        self,
        finished_workflow_name: str | None,
        finished_workflow_state: str | None,
        *args,
        **kwargs,
    ):
        def should_trigger(event: WorkflowEndEvent):
            if (
                finished_workflow_name is not None
                and finished_workflow_name != event.metadata.workflow_name
            ):
                return False

            if (
                finished_workflow_state is not None
                and finished_workflow_state != event.metadata.workflow_state
            ):
                return False

            return True

        super().__init__(
            *args,
            should_trigger_workflow=should_trigger,
            finished_workflow_name=finished_workflow_name,  # type: ignore
            finished_workflow_state=finished_workflow_state,  # type: ignore
            **kwargs,
        )
