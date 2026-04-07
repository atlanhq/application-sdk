"""Typed contracts for Handler operations.

Provides Pydantic models for the three core handler operations:
- Authentication (test_auth)
- Preflight checks (preflight_check)
- Metadata discovery (fetch_metadata)

Plus supporting types for credentials, log streaming, and file uploads.

These are HTTP boundary types — Pydantic BaseModel gives boundary validation
on ingress (``model_validate``), direct JSON serialization on egress
(``model_dump``), and automatic OpenAPI schema generation.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from application_sdk.contracts.base import SerializableEnum


class HandlerCredential(BaseModel):
    """A single credential key-value pair for HTTP handler inputs.

    Credentials are always transmitted as opaque key/value strings.
    Interpretation (e.g., as OAuth token, API key, password) is the
    handler's responsibility.
    """

    model_config = ConfigDict(frozen=True)

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


class AuthInput(BaseModel):
    """Input for the test_auth handler operation."""

    credentials: list[HandlerCredential] = []
    """Credentials to authenticate with."""

    connection_id: str = ""
    """Optional connection ID for context."""

    timeout_seconds: int = 30
    """Maximum seconds to wait for auth response."""


class AuthOutput(BaseModel):
    """Output from the test_auth handler operation."""

    status: AuthStatus
    """Authentication result status."""

    message: str = ""
    """Human-readable status message."""

    identities: list[str] = []
    """Verified identities (e.g., usernames, roles)."""

    scopes: list[str] = []
    """Authorized scopes or permissions."""

    expires_at: str = ""
    """ISO-8601 expiry timestamp (empty if no expiry)."""


class PreflightStatus(SerializableEnum):
    """Overall result of a preflight check."""

    READY = "ready"
    NOT_READY = "not_ready"
    PARTIAL = "partial"


class PreflightCheck(BaseModel):
    """Result of a single preflight check."""

    name: str
    """Check name (e.g., 'connectivity', 'permissions')."""

    passed: bool = False
    """Whether the check passed."""

    message: str = ""
    """Details about the check result."""

    duration_ms: float = 0.0
    """How long the check took in milliseconds."""


class PreflightInput(BaseModel):
    """Input for the preflight_check handler operation."""

    credentials: list[HandlerCredential] = []
    """Credentials to use during preflight."""

    connection_config: dict[str, Any] = {}
    """Connection configuration (host, port, database, etc.)."""

    checks_to_run: list[str] = []
    """Specific checks to run (empty = run all)."""

    timeout_seconds: int = 60
    """Maximum seconds to wait for all checks."""


class PreflightOutput(BaseModel):
    """Output from the preflight_check handler operation."""

    status: PreflightStatus
    """Overall preflight result."""

    checks: list[PreflightCheck] = []
    """Individual check results."""

    message: str = ""
    """Human-readable summary."""

    total_duration_ms: float = 0.0
    """Total time for all checks in milliseconds."""


class MetadataField(BaseModel):
    """A field/column within a metadata object.

    .. deprecated::
        Retained for backward compatibility. Not used by the new
        ``SqlMetadataObject`` / ``ApiMetadataObject`` contracts.
    """

    name: str
    """Field name."""

    field_type: str = ""
    """Data type (e.g., 'VARCHAR', 'INTEGER')."""

    nullable: bool = True
    """Whether the field allows null values."""

    description: str = ""
    """Optional field description."""


class MetadataObject(BaseModel):
    """A discoverable object (table, view, schema, etc.).

    .. deprecated::
        Use ``SqlMetadataObject`` for SQL connectors or
        ``ApiMetadataObject`` for BI/API connectors instead.
    """

    name: str
    """Object name."""

    object_type: str = ""
    """Object type (e.g., 'TABLE', 'VIEW', 'SCHEMA')."""

    schema: str = ""  # pyright: ignore[reportIncompatibleMethodOverride]
    """Parent schema name."""

    database: str = ""
    """Parent database name."""

    description: str = ""
    """Optional description."""

    fields: list[MetadataField] = []
    """Fields/columns within this object."""


# ---------------------------------------------------------------------------
# Metadata object models — one per frontend widget type
# ---------------------------------------------------------------------------


class SqlMetadataObject(BaseModel):
    """A row for the **sqltree** frontend widget.

    The sqltree widget expects a flat list of catalog/schema pairs.
    The frontend groups them into a tree (catalogs → schemas).
    """

    TABLE_CATALOG: str
    """Database / catalog name."""

    TABLE_SCHEMA: str
    """Schema name."""


class ApiMetadataObject(BaseModel):
    """A node for the **apitree** frontend widget.

    Supports arbitrarily nested hierarchies — each node can contain
    child nodes via the ``children`` field.
    """

    value: str
    """Unique identifier for this node (used as the selection value)."""

    title: str
    """Display label shown in the tree UI."""

    node_type: str = ""
    """Optional type discriminator (e.g., 'tag', 'project', 'folder')."""

    children: list[ApiMetadataObject] = []
    """Child nodes (empty for leaf nodes)."""


# Resolve the recursive forward reference for ApiMetadataObject.children
ApiMetadataObject.model_rebuild()


# ---------------------------------------------------------------------------
# Metadata input / output contracts
# ---------------------------------------------------------------------------


class MetadataInput(BaseModel):
    """Input for the fetch_metadata handler operation."""

    credentials: list[HandlerCredential] = []
    """Credentials to use for metadata discovery."""

    connection_config: dict[str, Any] = {}
    """Connection configuration."""

    object_filter: str = ""
    """Filter pattern (e.g., 'public.*', 'mydb.myschema.*')."""

    include_fields: bool = True
    """Whether to include field/column details."""

    max_objects: int = 1000
    """Maximum number of objects to return."""

    timeout_seconds: int = 120
    """Maximum seconds to wait for metadata fetch."""


class MetadataOutput(BaseModel):
    """Base output from the fetch_metadata handler operation.

    Do not instantiate directly — use ``SqlMetadataOutput`` or
    ``ApiMetadataOutput`` instead.  This base class exists so the
    handler return type (``MetadataOutput``) covers both subtypes
    via ``isinstance``.
    """


class SqlMetadataOutput(MetadataOutput):
    """Metadata output for SQL connectors (sqltree widget).

    Each object is a flat ``{TABLE_CATALOG, TABLE_SCHEMA}`` row.
    The frontend groups rows into a catalog → schema tree.

    Example::

        SqlMetadataOutput(objects=[
            SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="FINANCE"),
            SqlMetadataObject(TABLE_CATALOG="DEFAULT", TABLE_SCHEMA="SALES"),
        ])
    """

    objects: list[SqlMetadataObject] = []
    """Discovered catalog/schema pairs."""


class ApiMetadataOutput(MetadataOutput):
    """Metadata output for BI / API connectors (apitree widget).

    Each object is a ``{value, title, node_type, children}`` tree node.
    The backend builds the full hierarchy; the frontend renders it as-is.

    Example::

        ApiMetadataOutput(objects=[
            ApiMetadataObject(
                value="tag-1", title="Finance", node_type="tag",
                children=[
                    ApiMetadataObject(value="tag-1a", title="Revenue", node_type="tag"),
                ],
            ),
        ])
    """

    objects: list[ApiMetadataObject] = []
    """Top-level tree nodes."""


class EventFilterRule(BaseModel):
    """A single filter rule for matching incoming Dapr cloud events."""

    model_config = ConfigDict(frozen=True)

    path: str
    """CEL path to evaluate (e.g., 'event.data.type')."""

    operator: str
    """Comparison operator (e.g., '==')."""

    value: str
    """Expected value (e.g., 'metadata_extraction')."""


class EventTriggerConfig(BaseModel):
    """Configuration for an event-triggered workflow."""

    model_config = ConfigDict(frozen=True)

    event_id: str
    """Unique identifier used as the route segment (e.g., 'my-trigger')."""

    event_type: str
    """Dapr topic / event type (e.g., 'metadata_extraction')."""

    event_name: str
    """Logical event name used in subscription filter rules."""

    event_filters: list[EventFilterRule] = []
    """Additional CEL filter rules applied to the event."""


class SubscriptionConfig(BaseModel):
    """Configuration for a Dapr pub/sub subscription with a custom handler."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

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


class CloudEventEnvelope(BaseModel):
    """Minimal representation of a Dapr CloudEvent envelope."""

    id: str
    source: str
    specversion: str
    type: str
    time: str
    topic: str
    data: dict[str, Any]
    datacontenttype: str = "application/json"


class FileUploadResponse(BaseModel):
    """Response from a file upload operation."""

    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

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
