"""Typed contracts for the three server handler operations.

The auth / preflight / metadata boundary types the SQL-connector server path
uses. These models define the canonical request/response wire shape for Atlan
app servers — changing a field name or type changes the wire contract. Pydantic
gives ingress validation (``model_validate``) and egress serialization
(``model_dump``).
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from server_sdk.contracts.base import SerializableEnum
from server_sdk.errors.base import AppError
from server_sdk.errors.wire import FailureDetails
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Connection / metadata config — dict-like Pydantic models (extra allowed)
# ---------------------------------------------------------------------------


class _DictLikeConfigBase(BaseModel):
    """Dict/Mapping protocol over a Pydantic model so handlers can use either
    ``cfg.host`` or ``cfg["host"]`` / ``cfg.get("host")`` / ``"host" in cfg``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def __getitem__(self, key: str) -> Any:
        if key in type(self).model_fields:
            return getattr(self, key)
        for name, field_info in type(self).model_fields.items():
            if field_info.alias == key:
                return getattr(self, name)
        if self.model_extra and key in self.model_extra:
            return self.model_extra[key]
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
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
        return [k for k, _ in self]

    def values(self) -> list[Any]:
        return [v for _, v in self]

    def items(self) -> list[tuple[str, Any]]:
        return [(k, v) for k, v in self]

    def __len__(self) -> int:
        return sum(1 for _ in self)


class BaseConnectionConfig(_DictLikeConfigBase):
    """Connection configuration (host, port, database, ...). Apps may subclass
    to declare typed fields; raw dicts pass through via ``extra='allow'``."""


class BaseMetadataConfig(_DictLikeConfigBase):
    """Form-level metadata forwarded alongside credentials."""


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


class HandlerCredential(BaseModel):
    """A single opaque credential key/value pair."""

    model_config = ConfigDict(frozen=True)

    key: str
    value: str

    @classmethod
    def list_from_raw(cls, creds_dict: dict[str, Any]) -> list["HandlerCredential"]:
        return [
            cls(key=p["key"], value=p["value"])
            for p in flatten_credentials_to_pairs(creds_dict)
        ]


def _serialize_credential_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value)


def flatten_credentials_to_pairs(creds_dict: dict[str, Any]) -> list[dict[str, str]]:
    """Flatten a credential dict to v3 ``[{key, value}]`` pairs.

    Nested ``extra`` is hoisted to ``extra.<k>`` keys, always appended after the
    top-level keys. ``None`` values are dropped at both levels; non-str values
    are ``json.dumps``-serialized.
    """
    pairs: list[dict[str, str]] = []
    extra = creds_dict.get("extra")
    for key, value in creds_dict.items():
        if key == "extra" or value is None:
            continue
        pairs.append({"key": key, "value": _serialize_credential_value(value)})
    if isinstance(extra, dict):
        for key, value in extra.items():
            if value is not None:
                pairs.append(
                    {"key": f"extra.{key}", "value": _serialize_credential_value(value)}
                )
    return pairs


# Credential-shaped keys a v2 flat body carries at the top level.
_CREDENTIAL_KEYS = frozenset(
    {
        "host",
        "port",
        "authType",
        "username",
        "password",
        "connectorType",
        "connectorConfigName",
        "extra",
    }
)


def normalize_credentials(body: dict[str, Any]) -> dict[str, Any]:
    """Normalize any accepted credential shape to v3 ``list[{key, value}]``.

    Handles three inbound shapes: a v3 ``credentials`` list (passthrough), a v2
    nested dict under ``credentials``, and v2 flat top-level keys. In every case
    credential material ends up **only** under ``credentials`` and nowhere else
    in the body — the flat top-level keys are removed — so a caller that must
    not forward credentials (``/start`` → Temporal history) can strip them by
    deleting that single key.
    """
    creds = body.get("credentials")
    if isinstance(creds, list):
        return body
    if isinstance(creds, dict):
        logger.info(
            "Converting v2 nested-dict credentials to v3 list, keys=%s",
            list(creds.keys()),
        )
        rest = {k: v for k, v in body.items() if k != "credentials"}
        return {**rest, "credentials": flatten_credentials_to_pairs(dict(creds))}
    if creds is None and _CREDENTIAL_KEYS & body.keys():
        flat = {k: v for k, v in body.items() if k in _CREDENTIAL_KEYS}
        rest = {k: v for k, v in body.items() if k not in _CREDENTIAL_KEYS}
        logger.info(
            "Converting v2 flat top-level credentials to v3 list, keys=%s",
            list(flat.keys()),
        )
        return {**rest, "credentials": flatten_credentials_to_pairs(flat)}
    return body


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


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


# Placed outside the class because a member-valued dict on the class body would
# be treated as an enum member. Kept next to AuthStatus so adding a member
# without updating this map fails loudly (KeyError) at runtime.
_AUTH_STATUS_HTTP_CODES: dict[AuthStatus, int] = {
    AuthStatus.SUCCESS: 200,
    AuthStatus.FAILED: 401,
    AuthStatus.EXPIRED: 401,
    AuthStatus.INVALID_CREDENTIALS: 401,
}

# Retained as a public alias for callers importing the old name.
AUTH_STATUS_HTTP_CODES = _AUTH_STATUS_HTTP_CODES


class AuthInput(BaseModel):
    """Input for the test_auth handler operation."""

    credentials: list[HandlerCredential] = []
    connection_id: str = ""
    entrypoint: str = ""
    entrypoint_ref: str = Field(
        default="",
        validation_alias=AliasChoices("entrypoint_ref", "connector"),
        serialization_alias="connector",
    )
    timeout_seconds: int = 30


class AuthOutput(BaseModel):
    """Output from the test_auth handler operation."""

    status: AuthStatus
    message: str = ""
    identities: list[str] = []
    scopes: list[str] = []
    expires_at: str = ""


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class PreflightStatus(SerializableEnum):
    """Overall preflight verdict — decides the gate.

    ``READY`` and ``PARTIAL`` always proceed; ``NOT_READY`` blocks only in hard
    mode. Display-only surfaces read ``.value``.
    """

    READY = "ready"
    NOT_READY = "not_ready"
    PARTIAL = "partial"


class PreflightCheck(BaseModel):
    """Result of a single preflight check."""

    name: str = Field(..., min_length=1)
    passed: bool = False
    message: str = ""
    error: FailureDetails | None = None
    duration_ms: float = 0.0

    @field_validator("error", mode="before")
    @classmethod
    def _coerce_error(cls, value: Any) -> Any:
        if isinstance(value, AppError):
            return value.to_failure_details()
        return value

    @property
    def resolved_message(self) -> str:
        """Message under the precedence rule: a failed check's ``error`` wins."""
        if self.error is not None and not self.passed:
            return self.error.message
        return self.message

    @property
    def resolved_suggested_action(self) -> str:
        """Suggested action from a failed check's ``error``; empty otherwise."""
        if self.error is not None and not self.passed:
            return self.error.suggested_action or ""
        return ""


class PreflightInput(BaseModel):
    """Input for the preflight_check handler operation."""

    credentials: list[HandlerCredential] = []
    credentials_by_name: dict[str, list[HandlerCredential]] = Field(
        default_factory=dict
    )
    entrypoint: str = ""
    entrypoint_ref: str = Field(
        default="",
        validation_alias=AliasChoices("entrypoint_ref", "connector"),
        serialization_alias="connector",
    )
    connection_config: BaseConnectionConfig = Field(
        default_factory=BaseConnectionConfig
    )
    metadata: BaseMetadataConfig = Field(default_factory=BaseMetadataConfig)
    checks_to_run: list[str] = []
    timeout_seconds: int = 60


class PreflightOutput(BaseModel):
    """Output from the preflight_check handler operation."""

    status: PreflightStatus
    checks: list[PreflightCheck] = []
    message: str = ""
    total_duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


class MetadataInput(BaseModel):
    """Input for the fetch_metadata handler operation."""

    credentials: list[HandlerCredential] = []
    entrypoint: str = ""
    entrypoint_ref: str = Field(
        default="",
        validation_alias=AliasChoices("entrypoint_ref", "connector"),
        serialization_alias="connector",
    )
    metadata_template_key: str = Field(
        default="",
        validation_alias=AliasChoices(
            "metadata_template_key", "metadataTemplateKey", "type"
        ),
    )
    connection_config: BaseConnectionConfig = Field(
        default_factory=BaseConnectionConfig
    )
    object_filter: str = ""
    include_fields: bool = True
    max_objects: int = 1000
    timeout_seconds: int = 120


class SqlMetadataObject(BaseModel):
    """A row for the sqltree widget — a catalog/schema pair."""

    TABLE_CATALOG: str
    TABLE_SCHEMA: str


class ApiMetadataObject(BaseModel):
    """A node for the apitree widget."""

    value: str
    title: str
    node_type: str = ""
    children: list["ApiMetadataObject"] = []


# Resolve the recursive forward reference for ApiMetadataObject.children.
ApiMetadataObject.model_rebuild()


class MetadataOutput(BaseModel):
    """Base output from the fetch_metadata handler operation."""

    objects: list[Any] = []


class SqlMetadataOutput(MetadataOutput):
    objects: list[SqlMetadataObject] = []  # type: ignore[assignment]


class ApiMetadataOutput(MetadataOutput):
    objects: list[ApiMetadataObject] = []  # type: ignore[assignment]
