"""Typed contracts for the three server handler operations.

The auth / preflight / metadata boundary types the SQL-connector server path
uses. These models define the canonical request/response wire shape for Atlan
app servers — changing a field name or type changes the wire contract. Pydantic
gives ingress validation (``model_validate``) and egress serialization
(``model_dump``).
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)
from server_sdk.contracts.base import SerializableEnum
from server_sdk.errors.base import AppError
from server_sdk.errors.redaction import redact_secrets
from server_sdk.errors.wire import FailureDetails
from server_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Connection / metadata config — dict-like Pydantic models (extra allowed)
# ---------------------------------------------------------------------------


def _pairs_to_mapping(items: Sequence[Any], origin: str) -> tuple[dict[str, Any], bool]:
    """Fold a v3-style ``[{key, value}]`` sequence into a mapping.

    The credential plane on the same request uses that shape, so a caller that
    builds one config block the same way it builds credentials is a shape
    confusion we can absorb rather than 500 on. Entries that are not
    ``{"key": ...}`` mappings are skipped; ``extra.<k>`` keys are hoisted the
    same way :func:`flatten_credentials_to_pairs` writes them.

    Returns the mapping and whether anything was dropped on the way.
    """
    folded: dict[str, Any] = {}
    extra: dict[str, Any] = {}
    skipped = 0
    for item in items:
        if not isinstance(item, Mapping) or "key" not in item:
            skipped += 1
            continue
        key = str(item["key"])
        value = item.get("value")
        if key.startswith("extra."):
            extra[key[len("extra.") :]] = value
        else:
            folded[key] = value
    if extra:
        existing = folded.get("extra")
        folded["extra"] = (
            {**existing, **extra} if isinstance(existing, Mapping) else extra
        )
    if skipped:
        # Count only — a config block can carry customer-identifying values, so
        # nothing from the payload itself is ever logged.
        logger.warning(
            "%s: skipped %d sequence entr%s with no 'key' field",
            origin,
            skipped,
            "y" if skipped == 1 else "ies",
        )
    return folded, bool(skipped)


class _DictLikeConfigBase(BaseModel):
    """Dict/Mapping protocol over a Pydantic model so handlers can use either
    ``cfg.host`` or ``cfg["host"]`` / ``cfg.get("host")`` / ``"host" in cfg``."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    # True when the wire carried a config block we could not read. Distinct
    # from "carried nothing": see :meth:`wire_degraded`.
    _wire_degraded: bool = PrivateAttr(default=False)

    @model_validator(mode="wrap")
    @classmethod
    def _coerce_wire_shape(cls, value: Any, handler: Any) -> Any:
        """Normalize whatever the wire carried into a mapping before validating.

        These config blocks are **not** shape-stable across the callers that
        POST them. Observed on this route: a mapping (the documented shape), a
        JSON *string* (the setup form serializes nested form state), an explicit
        JSON ``null``, and a ``[{key, value}]`` list copied from the credential
        plane. Only the first validated; the rest raised a pydantic
        ``ValidationError`` from ``model_validate`` in the route, which runs
        *outside* the route's ``try``, so they surfaced as a bare HTTP 500 with
        the handler never invoked — taking down the whole preflight, including
        the checks that need no config at all.

        The coercion itself never raises. A subclass that declares typed fields
        still validates them normally.
        """
        coerced, degraded = cls._normalize_wire_shape(value)
        instance = handler(coerced)
        if degraded and isinstance(instance, _DictLikeConfigBase):
            instance._wire_degraded = True
        return instance

    @classmethod
    def _normalize_wire_shape(cls, value: Any) -> tuple[Any, bool]:
        """Return ``(mapping, degraded)``. ``degraded`` means data was lost.

        Nothing from the payload is ever logged — only its type and length. A
        config block is form state and can carry customer-identifying values.
        """
        if value is None:
            # "Sent nothing" — not a degradation, just an absent block.
            return {}, False
        if isinstance(value, (_DictLikeConfigBase, Mapping)):
            return value, False
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return {}, False
            try:
                parsed = json.loads(text)
            except (ValueError, RecursionError) as exc:
                # RecursionError as well as ValueError: this string was *inside*
                # the request body, so starlette's own json parse never looked
                # at it, and a deeply-nested one ("[[[[...") reaches json.loads
                # unbounded. Letting it escape would be the 500 this coercion
                # exists to prevent.
                logger.warning(
                    "%s: ignoring unreadable JSON string config (%d chars, %s)",
                    cls.__name__,
                    len(text),
                    type(exc).__name__,
                    exc_info=True,
                )
                return {}, True
            if isinstance(parsed, Mapping):
                return parsed, False
            if isinstance(parsed, (list, tuple)):
                return _pairs_to_mapping(parsed, cls.__name__)
            logger.warning(
                "%s: ignoring JSON string config decoding to %s, not an object",
                cls.__name__,
                type(parsed).__name__,
            )
            return {}, True
        if isinstance(value, (list, tuple)):
            return _pairs_to_mapping(value, cls.__name__)
        logger.warning(
            "%s: ignoring config of unsupported type %s",
            cls.__name__,
            type(value).__name__,
        )
        return {}, True

    @property
    def wire_degraded(self) -> bool:
        """True when the wire sent a config block that could not be read.

        The difference matters to any check that authorizes *against* this
        block. An empty config is ambiguous on its own: a check that iterates
        the include filter passes **vacuously** when the filter is empty, so
        "the form sent no filter" and "the form sent a filter I could not parse"
        would otherwise both render as a green authorization row. They are not
        the same claim — the second one verified nothing. A blocking tier should
        treat this as a failed check, not a passed one::

            cfg = input.connection_config
            if cfg.wire_degraded:
                return PreflightCheck(
                    name="databaseSchemaCheck",
                    passed=False,
                    message="Could not read the connection config to verify access",
                )
        """
        return self._wire_degraded

    def as_dict(self) -> dict[str, Any]:
        """Plain ``dict`` view, including ``extra`` keys.

        For helpers annotated ``dict[str, Any]`` that would otherwise need a
        type-checker suppression at the call site even though the dict protocol
        above already satisfies them at runtime.
        """
        return self.model_dump()

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
    duration_ms: float = -1.0
    """How long the check took in milliseconds. ``-1.0`` means not measured --
    the default is a sentinel, not an elapsed time, so an unset value is never
    mistaken for an instant check. Kept non-optional (not ``None``) so the key
    survives ``exclude_none`` dumps and stays numeric for ClickHouse readers."""

    @field_validator("error", mode="before")
    @classmethod
    def _coerce_error(cls, value: Any) -> Any:
        if isinstance(value, AppError):
            return value.to_failure_details()
        return value

    @field_validator("message")
    @classmethod
    def _scrub_message(cls, v: str) -> str:
        # Does not go through FailureDetails, so it needs its own scrub: the
        # documented fallback for a SQL connector is `message=str(exc)`, and a
        # driver's str() embeds the DSN.
        return redact_secrets(v)

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
    """Input for the preflight_check handler operation.

    ``connection_config`` carries the setup form's own state (filters, control
    config, advanced options) and is what a tiered/authorization check reads to
    know *what* to authorize against — see
    :meth:`server_sdk.handler.sql.SQLHandler.preflight_tiers`. It accepts the
    camelCase wire spelling too, and any unreadable shape degrades to an empty
    config rather than failing the request (see
    :meth:`_DictLikeConfigBase._coerce_wire_shape`).

    ``populate_by_name`` keeps keyword construction by field name working for
    every field that declares a validation alias, so handler unit tests can
    build one directly: ``PreflightInput(connection_config={...})``.
    """

    model_config = ConfigDict(populate_by_name=True)

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
        default_factory=BaseConnectionConfig,
        validation_alias=AliasChoices("connection_config", "connectionConfig"),
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

    model_config = ConfigDict(populate_by_name=True)

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
        default_factory=BaseConnectionConfig,
        validation_alias=AliasChoices("connection_config", "connectionConfig"),
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
