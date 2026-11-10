"""Request/response DTOs of the v2 FastAPI server.

.. deprecated:: 3.56.0
    Every model here is deprecated and removed in v4.0. The v2 server that
    served them is gone; the v3 handler service owns those routes and their
    typed contracts in :mod:`application_sdk.handler.contracts` (see each
    model's notice for its replacement).

Nothing in the SDK imports this module. ``FileUploadResponse`` was the last
model in use, and :func:`application_sdk.common.utils.download_file_from_upload_response`
now takes any pydantic model with a ``key`` field instead
(:class:`application_sdk.handler.contracts.FileUploadResponse` is the one the
handler returns).
"""

from enum import Enum
from typing import Any, Callable, Coroutine, Dict, Optional, Union

from pydantic import BaseModel, Field, RootModel
from typing_extensions import deprecated

from application_sdk.contracts.events import Event


@deprecated(
    "application_sdk.server.fastapi.models.TestAuthRequest is deprecated; use application_sdk.handler.contracts.AuthInput instead "
    "— will be removed in v4.0."
)
class TestAuthRequest(RootModel[Dict[str, Any]]):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing database credentials"
    )


@deprecated(
    "application_sdk.server.fastapi.models.TestAuthResponse is deprecated; use application_sdk.handler.contracts.AuthOutput instead "
    "— will be removed in v4.0."
)
class TestAuthResponse(BaseModel):
    success: bool
    message: str


@deprecated(
    "application_sdk.server.fastapi.models.MetadataType is deprecated; use application_sdk.handler.contracts.MetadataInput instead "
    "— will be removed in v4.0."
)
class MetadataType(str, Enum):
    DATABASE = "database"
    SCHEMA = "schema"
    ALL = "all"


@deprecated(
    "application_sdk.server.fastapi.models.FetchMetadataRequest is deprecated; use application_sdk.handler.contracts.MetadataInput instead "
    "— will be removed in v4.0."
)
class FetchMetadataRequest(RootModel[Dict[str, Any]]):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing the metadata and credentials"
    )


@deprecated(
    "application_sdk.server.fastapi.models.FetchMetadataResponse is deprecated; use application_sdk.handler.contracts.MetadataOutput instead "
    "— will be removed in v4.0."
)
class FetchMetadataResponse(BaseModel):
    success: bool
    data: Any


@deprecated(
    "application_sdk.server.fastapi.models.PreflightCheckRequest is deprecated; use application_sdk.handler.contracts.PreflightInput instead "
    "— will be removed in v4.0."
)
class PreflightCheckRequest(BaseModel):
    model_config = {
        "json_schema_extra": {
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
    }

    credentials: Dict[str, Any] = Field(
        ..., description="Required JSON field containing database credentials"
    )
    metadata: Dict[str, Any] = Field(
        ...,
        description="Required JSON field containing form data for filtering and configuration",
    )


@deprecated(
    "application_sdk.server.fastapi.models.PreflightCheckResponse is deprecated; use application_sdk.handler.contracts.PreflightOutput instead "
    "— will be removed in v4.0."
)
class PreflightCheckResponse(BaseModel):
    model_config = {
        "json_schema_extra": {
            "example": {
                "success": True,
                "data": {
                    "successMessage": "Successfully checked",
                    "failureMessage": "",
                },
            }
        }
    }

    success: bool = Field(
        ..., description="Indicates if the overall operation was successful"
    )
    data: Dict[str, Any] = Field(..., description="Response data")


@deprecated(
    "application_sdk.server.fastapi.models.WorkflowRequest is deprecated; use the App's typed Input contract instead "
    "(an application_sdk.contracts.base.Input subclass, which the handler's "
    "/workflows/v1 routes accept) — will be removed in v4.0."
)
class WorkflowRequest(RootModel[Dict[str, Any]]):
    model_config = {
        "json_schema_extra": {
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
    }

    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing workflow configuration"
    )


@deprecated(
    "application_sdk.server.fastapi.models.EventWorkflowRequest is deprecated; use application_sdk.handler.contracts.CloudEventEnvelope instead "
    "— will be removed in v4.0."
)
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


@deprecated(
    "application_sdk.server.fastapi.models.WorkflowData is deprecated; use the handler's POST /workflows/v1/start response instead "
    "(it carries workflow_id and run_id) — will be removed in v4.0."
)
class WorkflowData(BaseModel):
    workflow_id: str = Field(..., description="Unique identifier for the workflow")
    run_id: str = Field(..., description="Unique identifier for the workflow run")


class _WorkflowResponseFields(BaseModel):
    # The fields WorkflowResponse and EventWorkflowResponse share. A private,
    # undecorated base because subclassing a @deprecated class warns at class
    # definition — EventWorkflowResponse(WorkflowResponse) would fire on every
    # import of this module, not on use.
    model_config = {
        "json_schema_extra": {
            "example": {
                "success": True,
                "message": "Workflow started successfully",
                "data": {
                    "workflow_id": "4b805f36-48c5-4dd3-942f-650e06f75bbc",
                    "run_id": "efe16ffe-24b2-4391-a7ec-7000c32c5893",
                },
            }
        }
    }

    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: WorkflowData = Field(..., description="Details about the workflow and run")


@deprecated(
    "application_sdk.server.fastapi.models.WorkflowResponse is deprecated; use the handler's POST "
    "/workflows/v1/start response instead — will be removed in v4.0."
)
class WorkflowResponse(_WorkflowResponseFields):
    """Response of the v2 server's workflow-start route."""


@deprecated(
    "application_sdk.server.fastapi.models.EventWorkflowResponse is deprecated; use the handler's "
    "/events/v1/event/{event_id} route instead — will be removed in v4.0."
)
class EventWorkflowResponse(_WorkflowResponseFields):
    class Status(str, Enum):
        SUCCESS = "SUCCESS"
        RETRY = "RETRY"
        DROP = "DROP"

    # This should be a string enum of the status of the workflow, based on the Dapr docs
    # https://docs.dapr.io/reference/api/pubsub_api/#expected-http-response
    status: Status = Field(..., description="Status of the workflow")


@deprecated(
    "application_sdk.server.fastapi.models.WorkflowConfigRequest is deprecated; use the App's typed Input contract instead "
    "(an application_sdk.contracts.base.Input subclass, which the handler's "
    "/workflows/v1 routes accept) — will be removed in v4.0."
)
class WorkflowConfigRequest(RootModel[Dict[str, Any]]):
    root: Dict[str, Any] = Field(
        ..., description="Root JSON object containing workflow configuration"
    )


@deprecated(
    "application_sdk.server.fastapi.models.WorkflowConfigResponse is deprecated; use the handler's GET /workflows/v1/config/{config_id} route instead "
    "— will be removed in v4.0."
)
class WorkflowConfigResponse(BaseModel):
    model_config = {
        "json_schema_extra": {
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
    }

    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: Dict[str, Any] = Field(..., description="Workflow configuration")


@deprecated(
    "application_sdk.server.fastapi.models.ConfigMapResponse is deprecated; use the handler's GET /workflows/v1/configmap/{config_map_id} route instead "
    "— will be removed in v4.0."
)
class ConfigMapResponse(BaseModel):
    model_config = {
        "json_schema_extra": {
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
    }

    success: bool = Field(
        ..., description="Indicates whether the operation was successful"
    )
    message: str = Field(
        ..., description="Message describing the result of the operation"
    )
    data: Dict[str, Any] = Field(..., description="Configuration map object")


@deprecated(
    "application_sdk.server.fastapi.models.Subscription is deprecated; use application_sdk.handler.contracts.SubscriptionConfig instead "
    "— will be removed in v4.0."
)
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


@deprecated(
    "application_sdk.server.fastapi.models.FileUploadResponse is deprecated; use application_sdk.handler.contracts.FileUploadResponse instead "
    "(snake_case attributes, serialised with the same camelCase aliases) — will be removed in v4.0."
)
class FileUploadResponse(BaseModel):
    """Response model for file upload endpoint matching heracles format.

    Field names use camelCase to stay consistent with the upstream
    Atlan ``/files`` endpoint contract.
    """

    model_config = {
        "json_schema_extra": {
            "example": {
                "id": "977f156b-9c78-4bfc-bd74-f603f18c078a",
                "version": "weathered-firefly-9025",
                "isActive": True,
                "createdAt": 1764265919324,
                "updatedAt": 1764265919324,
                "fileName": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
                "rawName": "ddls_export.csv",
                "key": "28bb016e-329f-46e1-b817-3fd315bdd7f0.csv",
                "extension": ".csv",
                "contentType": "text/csv",
                "fileSize": 39144,
                "isEncrypted": False,
                "redirectUrl": "",
                "isUploaded": True,
                "uploadedAt": "2024-01-01T00:00:00Z",
                "isArchived": False,
            }
        }
    }

    id: str = Field(..., description="UUID of the file")
    version: str = Field(..., description="Human-readable version string")
    isActive: bool = Field(..., description="Whether the file is active")
    createdAt: int = Field(..., description="Unix timestamp in milliseconds")
    updatedAt: int = Field(..., description="Unix timestamp in milliseconds")
    fileName: str = Field(..., description="UUID + extension")
    rawName: str = Field(..., description="Original filename")
    key: str = Field(..., description="Object store key (uuid+extension)")
    extension: str = Field(..., description="File extension (e.g., '.csv')")
    contentType: str = Field(..., description="Content type of the file")
    fileSize: int = Field(..., description="File size in bytes")
    isEncrypted: bool = Field(..., description="Whether the file is encrypted")
    redirectUrl: str = Field(..., description="Redirect URL (empty for now)")
    isUploaded: bool = Field(..., description="Whether the file is uploaded")
    uploadedAt: str = Field(
        ..., description="ISO timestamp or '0001-01-01T00:00:00Z' default"
    )
    isArchived: bool = Field(..., description="Whether the file is archived")
