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

from pydantic import AliasChoices, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from application_sdk.contracts.base import SerializableEnum


class _DictLikeConfigBase(BaseModel):
    """Shared implementation for the connection / metadata config bases.

    Provides the dict / Mapping protocol so existing callers that consumed
    the previous ``dict[str, Any]`` field do not need to change.  Supported::

        cfg["k"]                  cfg.get("k", default)   "k" in cfg
        cfg.keys()                cfg.values()            cfg.items()
        len(cfg)                  for k, v in cfg: ...

    ``__getitem__`` lookups try declared field names, then aliases, then
    ``model_extra``.  Iteration delegates to Pydantic's native
    :meth:`BaseModel.__iter__`, which yields all declared fields plus
    extras.

    Not intended to be subclassed directly — use
    :class:`BaseConnectionConfig` or :class:`BaseMetadataConfig`.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __getitem__(self, key: str) -> Any:
        if key in type(self).model_fields:
            return getattr(self, key)
        for name, field_info in type(self).model_fields.items():
            if field_info.alias == key:
                return getattr(self, name)
        if self.model_extra and key in self.model_extra:
            return self.model_extra[key]
        raise KeyError(key)  # stdlib-interop: __getitem__ protocol requires KeyError

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-style accessor with default — mirrors ``dict.get``."""
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        try:
            self[key]
        except KeyError:
            return False
        return True

    def keys(self) -> list[str]:
        """Mirrors ``dict.keys`` over declared fields plus extras."""
        return [k for k, _ in self]

    def values(self) -> list[Any]:
        """Mirrors ``dict.values`` over declared fields plus extras."""
        return [v for _, v in self]

    def items(self) -> list[tuple[str, Any]]:
        """Mirrors ``dict.items`` over declared fields plus extras."""
        return [(k, v) for k, v in self]

    def __len__(self) -> int:
        return sum(1 for _ in self)


class BaseConnectionConfig(_DictLikeConfigBase):
    """Base type for preflight and metadata connection configuration.

    Replaces ``dict[str, Any]`` as the type of ``PreflightInput.connection_config``
    and ``MetadataInput.connection_config``.  ``extra="allow"`` keeps raw-dict
    inputs from breaking on ingress; apps should subclass and declare fields
    with **real types** instead of carrying forward stringified JSON::

        class MyAppConnectionConfig(BaseConnectionConfig):
            include_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="include-filter"
            )
            exclude_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="exclude-filter"
            )

    The subclass can then be used directly in the handler::

        async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
            config = MyAppConnectionConfig.model_validate(
                input.connection_config.model_dump()
            )

    If the UI is generated from the contract toolkit (app.pkl), the subclass
    fields should mirror the ``uiConfig.tasks[*].inputs`` entries so the
    generated preflight metadata contract stays aligned with the UI contract.

    .. note::

        **Consumption is dict-compatible.**  In addition to attribute access
        (``cfg.host``), the full Mapping protocol from the old
        ``dict[str, Any]`` type continues to work — no caller-side
        migration needed::

            cfg["host"]               # KeyError if absent
            cfg.get("host", default)  # safe accessor with default
            "host" in cfg             # membership test
            cfg.keys() / .values() / .items()
            len(cfg)
            for k, v in cfg: ...

        Lookups try declared field names first, then aliases, then extras.
    """


class BaseMetadataConfig(_DictLikeConfigBase):
    """Base type for form-level metadata forwarded alongside preflight credentials.

    Captures the UI form state sent by the frontend (extraction type, filter
    keys, user-entered prefixes, etc.).  Apps subclass this to declare fields
    with **real types**::

        class MyAppMetadataConfig(BaseMetadataConfig):
            include_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="include-filter"
            )
            exclude_filter: dict[str, list[str]] = Field(
                default_factory=dict, alias="exclude-filter"
            )

    If the UI is generated from the contract toolkit (app.pkl), the subclass
    fields should mirror the ``uiConfig.tasks[*].inputs`` entries so the
    generated metadata contract stays aligned with the UI contract.

    Dict-style consumption (``cfg["k"]``, ``cfg.get(...)``, ``"k" in cfg``) is
    backward-compatible — see :class:`BaseConnectionConfig` for details.
    """


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


class AuthStatus(SerializableEnum):
    """Result of an authentication attempt."""

    SUCCESS = "success"
    FAILED = "failed"
    EXPIRED = "expired"
    INVALID_CREDENTIALS = "invalid_credentials"

    @property
    def http_status(self) -> int:
        """HTTP status code that should accompany this auth result."""
        return _AUTH_STATUS_HTTP_CODES[self]

    @property
    def is_success(self) -> bool:
        """Whether this status represents a successful authentication."""
        return self.http_status < 400


# Placed outside the class because StrEnum treats class-level dicts as
# member values.  Kept right next to AuthStatus so that adding a new
# member without updating this map fails loudly at runtime.
_AUTH_STATUS_HTTP_CODES: dict[AuthStatus, int] = {
    AuthStatus.SUCCESS: 200,
    AuthStatus.FAILED: 401,
    AuthStatus.EXPIRED: 401,
    AuthStatus.INVALID_CREDENTIALS: 401,
}


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

    name: str = Field(..., min_length=1)
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

    connection_config: BaseConnectionConfig = Field(
        default_factory=BaseConnectionConfig
    )
    """Connection configuration (host, port, database, etc.).

    Pass a :class:`BaseConnectionConfig` subclass for strong typing.  Raw dicts
    are accepted for backward compatibility via ``extra="allow"``.
    """

    metadata: BaseMetadataConfig = Field(default_factory=BaseMetadataConfig)
    """Form-level metadata forwarded by heracles alongside the credential.

    Contains the full UI form state (extraction type, source, user-entered
    prefixes, filter keys, etc.) as sent by the frontend.  Handlers that need
    form fields unavailable in the credential body can read them here.
    All other handlers can ignore this field safely.

    Pass a :class:`BaseMetadataConfig` subclass for strong typing.  Raw dicts
    are accepted for backward compatibility via ``extra="allow"``.
    """

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

    metadata_template_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "metadata_template_key",
            "metadataTemplateKey",
            "type",
        ),
    )
    """Metadata source routing key for multi-source metadata widgets.

    The ``type`` alias is accepted for legacy platform metadata requests.
    """

    connection_config: BaseConnectionConfig = Field(
        default_factory=BaseConnectionConfig
    )
    """Connection configuration.

    Pass a :class:`BaseConnectionConfig` subclass for strong typing.  Raw dicts
    are accepted for backward compatibility via ``extra="allow"``.
    """

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

    objects: list[Any] = []
    """Metadata objects. Subclasses narrow this type."""


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

    objects: list[SqlMetadataObject] = []  # type: ignore[assignment]
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

    objects: list[ApiMetadataObject] = []  # type: ignore[assignment]
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
