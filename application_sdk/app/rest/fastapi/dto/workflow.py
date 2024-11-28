# Request/Response DTOs for workflows

from typing import Any, Dict, List

from pydantic import BaseModel, Field


class TestAuthRequest(BaseModel):
    host: str
    port: int
    user: str
    password: str
    database: str


class TestAuthResponse(BaseModel):
    success: bool
    message: str


class FetchMetadataRequest(BaseModel):
    credential: Dict[str, Any]


class FetchMetadataResponse(BaseModel):
    success: bool
    metadata: List[Dict[str, str]]


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
                },
            }
        }


class PreflightCheckStatus(BaseModel):
    success: bool = Field(
        ..., description="Indicates if the specific check was successful"
    )
    successMessage: str = Field(
        ..., description="Message describing the success details"
    )
    failureMessage: str = Field(
        ..., description="Message describing the failure details (empty if successful)"
    )


class PreflightCheckDataSchema(BaseModel):
    databaseSchemaCheck: PreflightCheckStatus = Field(
        ..., description="Result of the database schema check"
    )
    tablesCheck: PreflightCheckStatus = Field(
        ..., description="Result of the tables check"
    )


class PreflightCheckResponse(BaseModel):
    success: bool = Field(
        ..., description="Indicates if the overall operation was successful"
    )
    data: PreflightCheckDataSchema = Field(
        ..., description="Details about the database schema and tables check"
    )

    class Config:
        schema_extra = {
            "example": {
                "success": True,
                "data": {
                    "databaseSchemaCheck": {
                        "success": True,
                        "successMessage": "Schemas and Databases check successful",
                        "failureMessage": "",
                    },
                    "tablesCheck": {
                        "success": True,
                        "successMessage": "Tables check successful. Table count: 2",
                        "failureMessage": "",
                    },
                },
            }
        }


class StartWorkflowRequest(BaseModel):
    credentials: Dict[str, Any] = Field(
        ..., description="Required JSON field containing database credentials"
    )
    connection: Dict[str, Any] = Field(
        ..., description="Required JSON field containing connection details"
    )
    metadata: Dict[str, Any] = Field(
        ..., description="Required JSON field containing metadata configuration"
    )

    class Config:
        schema_extra = {
            "example": {
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
                },
            }
        }


class WorkflowData(BaseModel):
    workflow_id: str = Field(..., description="Unique identifier for the workflow")
    run_id: str = Field(..., description="Unique identifier for the workflow run")


class StartWorkflowResponse(BaseModel):
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
