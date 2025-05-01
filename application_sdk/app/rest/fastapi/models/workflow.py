# Request/Response DTOs for workflows

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, RootModel


class TestAuthRequest(RootModel):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing database credentials"
    )


class TestAuthResponse(BaseModel):
    success: bool
    message: str


class FetchMetadataRequest(RootModel):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing database credentials"
    )

    class Config:
        schema_extra = {
            "example": {
                "extraction_method": "",
            }
        }


class FetchMetadataResponse(BaseModel):
    success: bool
    data: List[Dict[str, str]]


class PreflightCheckRequest(BaseModel):
    credentials: Dict[str, Any] = Field(
        ..., description="Required JSON field containing database credentials"
    )
    form_data: Dict[str, Any] = Field(
        ...,
        description="Required JSON field containing form data for filtering and configuration",
    )

    class Config:
        schema_extra = {
            "example": {
                "credentials": {
                    "host": "host",
                    "port": 5432,
                    "user": "username",
                    "password": "password",
                    "database": "databasename",
                },
                "form_data": {
                    "include_filter": '{"^dbengine$":["^public$","^airflow$"]}',
                    "exclude_filter": "{}",
                    "temp_table_regex": "",
                    "extraction_method": "",
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


class WorkflowRequest(RootModel):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing workflow configuration"
    )

    class Config:
        schema_extra = {
            "example": {
                "miner_args": {},
                "credentials": {
                    "host": "",
                    "port": 5432,
                    "user": "username",
                    "password": "password",
                    "database": "databasename",
                },
                "connection": {"connection": "dev"},
                "metadata": {
                    "include_filter": '{"^dbengine$":["^public$","^airflow$"]}',
                    "exclude_filter": "{}",
                    "temp_table_regex": "",
                    "advanced_config_strategy": "default",
                    "use_source_schema_filtering": "false",
                    "use_jdbc_internal_methods": "true",
                    "authentication": "BASIC",
                    "extraction_method": "",
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
                        "include_filter": '{"^dbengine$":["^public$","^airflow$"]}',
                        "exclude_filter": "{}",
                        "temp_table_regex": "",
                        "advanced_config_strategy": "default",
                        "use_source_schema_filtering": "false",
                        "use_jdbc_internal_methods": "true",
                        "authentication": "BASIC",
                    },
                },
            }
        }
