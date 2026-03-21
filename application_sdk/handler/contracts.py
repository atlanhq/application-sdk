"""Typed contracts for Handler operations.

Provides Input/Output dataclasses for the three core handler operations:
- Authentication (test_auth)
- Preflight checks (preflight_check)
- Metadata discovery (fetch_metadata)

Plus supporting types for credentials, log streaming, and file uploads.

TODO(v3-refactor): migrate all types in this module from plain dataclasses to
``pydantic.BaseModel`` (HTTP API zone rule — see ``application_sdk/contracts/base.py``).
These are HTTP boundary types and should use Pydantic for boundary validation,
direct JSON serialization via ``model_dump_json()``, and OpenAPI schema
generation. The corresponding endpoints in ``service.py`` manually unpack
``request.json()`` and use ``_serialize_output`` / ``dataclasses.asdict``
precisely because they lack Pydantic validation — that boilerplate goes away
once these types are Pydantic models.
Tracked as part of the v3 SDK refactor; defer until after manifest.py is stable.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from application_sdk.contracts.base import Input, Output, SerializableEnum
from application_sdk.contracts.types import MaxItems


@dataclasses.dataclass(frozen=True)
class HandlerCredential:
    """A single credential key-value pair for HTTP handler inputs.

    Credentials are always transmitted as opaque key/value strings.
    Interpretation (e.g., as OAuth token, API key, password) is the
    handler's responsibility.
    """

    key: str
    """Credential key (e.g., 'api_key', 'username')."""

    value: str
    """Credential value (sensitive — never log this directly)."""


# Backward-compatible alias — will be removed in a future release
Credential = HandlerCredential


class AuthStatus(SerializableEnum):
    """Result of an authentication attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"
    INVALID_CREDENTIALS = "invalid_credentials"


@dataclasses.dataclass
class AuthInput(Input):
    """Input for the test_auth handler operation."""

    credentials: Annotated[list[HandlerCredential], MaxItems(50)] = dataclasses.field(
        default_factory=list
    )
    """Credentials to authenticate with."""

    connection_id: str = ""
    """Optional connection ID for context."""

    timeout_seconds: int = 30
    """Maximum seconds to wait for auth response."""


@dataclasses.dataclass
class AuthOutput(Output):
    """Output from the test_auth handler operation."""

    status: AuthStatus
    """Authentication result status."""

    message: str = ""
    """Human-readable status message."""

    identities: Annotated[list[str], MaxItems(20)] = dataclasses.field(
        default_factory=list
    )
    """Verified identities (e.g., usernames, roles)."""

    scopes: Annotated[list[str], MaxItems(100)] = dataclasses.field(
        default_factory=list
    )
    """Authorized scopes or permissions."""

    expires_at: str = ""
    """ISO-8601 expiry timestamp (empty if no expiry)."""


class PreflightStatus(SerializableEnum):
    """Overall result of a preflight check."""

    READY = "ready"
    NOT_READY = "not_ready"
    PARTIAL = "partial"


@dataclasses.dataclass
class PreflightCheck:
    """Result of a single preflight check."""

    name: str
    """Check name (e.g., 'connectivity', 'permissions')."""

    passed: bool = False
    """Whether the check passed."""

    message: str = ""
    """Details about the check result."""

    duration_ms: float = 0.0
    """How long the check took in milliseconds."""


@dataclasses.dataclass
class PreflightInput(Input, allow_unbounded_fields=True):
    """Input for the preflight_check handler operation."""

    credentials: Annotated[list[HandlerCredential], MaxItems(50)] = dataclasses.field(
        default_factory=list
    )
    """Credentials to use during preflight."""

    connection_config: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Connection configuration (host, port, database, etc.)."""

    checks_to_run: Annotated[list[str], MaxItems(50)] = dataclasses.field(
        default_factory=list
    )
    """Specific checks to run (empty = run all)."""

    timeout_seconds: int = 60
    """Maximum seconds to wait for all checks."""


@dataclasses.dataclass
class PreflightOutput(Output):
    """Output from the preflight_check handler operation."""

    status: PreflightStatus
    """Overall preflight result."""

    checks: Annotated[list[PreflightCheck], MaxItems(100)] = dataclasses.field(
        default_factory=list
    )
    """Individual check results."""

    message: str = ""
    """Human-readable summary."""

    total_duration_ms: float = 0.0
    """Total time for all checks in milliseconds."""


@dataclasses.dataclass
class MetadataField:
    """A field/column within a metadata object."""

    name: str
    """Field name."""

    field_type: str = ""
    """Data type (e.g., 'VARCHAR', 'INTEGER')."""

    nullable: bool = True
    """Whether the field allows null values."""

    description: str = ""
    """Optional field description."""


@dataclasses.dataclass
class MetadataObject:
    """A discoverable object (table, view, schema, etc.)."""

    name: str
    """Object name."""

    object_type: str = ""
    """Object type (e.g., 'TABLE', 'VIEW', 'SCHEMA')."""

    schema: str = ""
    """Parent schema name."""

    database: str = ""
    """Parent database name."""

    description: str = ""
    """Optional description."""

    fields: Annotated[list[MetadataField], MaxItems(10000)] = dataclasses.field(
        default_factory=list
    )
    """Fields/columns within this object."""


@dataclasses.dataclass
class MetadataInput(Input, allow_unbounded_fields=True):
    """Input for the fetch_metadata handler operation."""

    credentials: Annotated[list[HandlerCredential], MaxItems(50)] = dataclasses.field(
        default_factory=list
    )
    """Credentials to use for metadata discovery."""

    connection_config: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Connection configuration."""

    object_filter: str = ""
    """Filter pattern (e.g., 'public.*', 'mydb.myschema.*')."""

    include_fields: bool = True
    """Whether to include field/column details."""

    max_objects: int = 1000
    """Maximum number of objects to return."""

    timeout_seconds: int = 120
    """Maximum seconds to wait for metadata fetch."""


@dataclasses.dataclass
class MetadataOutput(Output):
    """Output from the fetch_metadata handler operation."""

    objects: Annotated[list[MetadataObject], MaxItems(10000)] = dataclasses.field(
        default_factory=list
    )
    """Discovered metadata objects."""

    total_count: int = 0
    """Total number of objects found (may exceed len(objects) if truncated)."""

    truncated: bool = False
    """Whether results were truncated due to max_objects limit."""

    fetch_duration_ms: float = 0.0
    """Total fetch time in milliseconds."""


@dataclasses.dataclass(frozen=True)
class EventFilterRule:
    """A single filter rule for matching incoming Dapr cloud events."""

    path: str
    """CEL path to evaluate (e.g., 'event.data.type')."""

    operator: str
    """Comparison operator (e.g., '==')."""

    value: str
    """Expected value (e.g., 'metadata_extraction')."""


@dataclasses.dataclass(frozen=True)
class EventTriggerConfig:
    """Configuration for an event-triggered workflow."""

    event_id: str
    """Unique identifier used as the route segment (e.g., 'my-trigger')."""

    event_type: str
    """Dapr topic / event type (e.g., 'metadata_extraction')."""

    event_name: str
    """Logical event name used in subscription filter rules."""

    event_filters: list[EventFilterRule] = dataclasses.field(default_factory=list)
    """Additional CEL filter rules applied to the event."""


@dataclasses.dataclass(frozen=True)
class SubscriptionConfig:
    """Configuration for a Dapr pub/sub subscription with a custom handler."""

    component_name: str
    """Dapr pubsub component name."""

    topic: str
    """Topic to subscribe to."""

    route: str
    """Route path segment served at /subscriptions/v1/{route}."""

    handler: Callable[..., Awaitable[Any]]
    """Async callback invoked when a message arrives on this topic."""

    bulk_enabled: bool = False
    """Enable bulk subscribe for higher throughput."""

    bulk_max_messages: int = 100
    """Maximum messages per bulk batch."""

    bulk_max_await_ms: int = 40
    """Maximum milliseconds to wait for a full bulk batch."""

    dead_letter_topic: str | None = None
    """Optional dead-letter topic for failed messages."""


@dataclasses.dataclass
class CloudEventEnvelope:
    """Minimal representation of a Dapr CloudEvent envelope."""

    id: str
    source: str
    specversion: str
    type: str
    time: str
    topic: str
    data: dict[str, Any]
    datacontenttype: str = "application/json"


@dataclasses.dataclass
class FileUploadResponse:
    """Response from a file upload operation."""

    id: str = ""
    version: str = "1"
    is_active: bool = True
    created_at: int = 0
    updated_at: int = 0
    file_name: str = ""
    raw_name: str = ""
    key: str = ""
    extension: str = ""
    content_type: str = ""
    file_size: int = 0
    is_uploaded: bool = False
    uploaded_at: str = ""

    def to_wire_dict(self) -> dict[str, Any]:
        """Convert to camelCase wire format."""
        return {
            "id": self.id,
            "version": self.version,
            "isActive": self.is_active,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "fileName": self.file_name,
            "rawName": self.raw_name,
            "key": self.key,
            "extension": self.extension,
            "contentType": self.content_type,
            "fileSize": self.file_size,
            "isUploaded": self.is_uploaded,
            "uploadedAt": self.uploaded_at,
        }
