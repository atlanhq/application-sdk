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

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic.alias_generators import to_camel

from application_sdk.contracts.base import SerializableEnum

logger = logging.getLogger(__name__)

# Fields that belong to v3 input models — everything else in a v2 payload is a credential
_V3_KNOWN_FIELDS: frozenset[str] = frozenset(
    {
        "credentials",
        "connection_id",
        "timeout_seconds",
        "connection_config",
        "checks_to_run",
        "object_filter",
        "include_fields",
        "max_objects",
    }
)


def _normalize_v2_credentials(data: dict[str, Any]) -> dict[str, Any]:
    """Convert v2 flat credential dict to v3 HandlerCredential list.

    If ``credentials`` is already a list, the payload is v3-native and passes
    through unchanged.  Otherwise every top-level key not in
    ``_V3_KNOWN_FIELDS`` is extracted as a credential.  The ``extra`` dict
    (common in v2) is flattened — each key inside becomes its own credential.
    """
    creds = data.get("credentials")
    if isinstance(creds, list):
        return data  # Already v3

    # v2 format detected — extract credentials from top-level keys
    credential_pairs: dict[str, str] = {}
    keys_to_remove: list[str] = []

    for key, value in data.items():
        if key in _V3_KNOWN_FIELDS:
            continue
        if key == "extra" and isinstance(value, dict):
            # Flatten extra dict — each key becomes its own credential
            for ek, ev in value.items():
                credential_pairs[ek] = _serialize_credential_value(ev)
        else:
            credential_pairs[key] = _serialize_credential_value(value)
        keys_to_remove.append(key)

    if not credential_pairs:
        return data

    logger.info(
        "Detected v2 flat credential format, normalizing to v3: keys=%s",
        list(credential_pairs.keys()),
    )

    # Build new data dict without the extracted keys
    normalized = {k: v for k, v in data.items() if k not in keys_to_remove}
    normalized["credentials"] = [
        {"key": k, "value": v} for k, v in credential_pairs.items()
    ]
    return normalized


def _serialize_credential_value(value: Any) -> str:
    """Serialize a v2 credential value to string."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


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

    @model_validator(mode="before")
    @classmethod
    def _normalize_v2(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _normalize_v2_credentials(data)
        return data


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

    @model_validator(mode="before")
    @classmethod
    def _normalize_v2(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _normalize_v2_credentials(data)
        return data


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
    """A field/column within a metadata object."""

    name: str
    """Field name."""

    field_type: str = ""
    """Data type (e.g., 'VARCHAR', 'INTEGER')."""

    nullable: bool = True
    """Whether the field allows null values."""

    description: str = ""
    """Optional field description."""


class MetadataObject(BaseModel):
    """A discoverable object (table, view, schema, etc.)."""

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

    @model_validator(mode="before")
    @classmethod
    def _normalize_v2(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return _normalize_v2_credentials(data)
        return data


class MetadataOutput(BaseModel):
    """Output from the fetch_metadata handler operation."""

    objects: list[MetadataObject] = []
    """Discovered metadata objects."""

    total_count: int = 0
    """Total number of objects found (may exceed len(objects) if truncated)."""

    truncated: bool = False
    """Whether results were truncated due to max_objects limit."""

    fetch_duration_ms: float = 0.0
    """Total fetch time in milliseconds."""


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
