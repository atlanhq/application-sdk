"""Agent-json v2: per-field value sources, declared explicitly.

A v1 agent-json is a flat ``dotted-key -> string`` map; in ``key-type:
single-key`` mode the SDK has to probe every string as a possible secret name
because nothing says which fields are references. A v2 payload (``"format":
2``) declares each field's source instead, in the Kubernetes ``value`` /
``valueFrom`` style::

    {"format": 2, "agent-name": "a", "secret-manager": "awssecretmanager",
     "host": {"value": "db.example.com"},
     "basic.password": {"valueFrom": {"secretKeyRef":
         {"name": "connections/example-db", "key": "db_password"}}},
     "extra.ca_cert": {"valueFrom": {"fileRef":
         {"path": "objectstore://certs/ca.pem"}}}}

This module only models and validates that shape. Resolution lives in
:mod:`application_sdk.credentials.agent`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from application_sdk.credentials.errors import CredentialParseError

#: Top-level keys that stay plain values in a v2 payload instead of being
#: field sources: the format marker, the agent/secret-manager envelope, and
#: the AE-injected workflow ``name``.
V2_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "format",
        "agent-name",
        "secret-manager",
        "aws-region",
        "aws-auth-method",
        "azure-auth-method",
        "agent-type",
        "auth-type",
        "connectBy",
        "name",
    }
)

#: v1 keys that describe where to fetch secrets from. A v2 payload carries
#: that per field, so combining them is ambiguous and rejected.
_V1_FETCH_KEYS: Final[tuple[str, ...]] = ("key-type", "secret-path")

JsonScalar = StrictStr | StrictInt | StrictFloat | StrictBool | None


class SecretKeyRef(BaseModel):
    """A key inside a named secret in the customer's secret store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    key: str = Field(min_length=1)


class FileRef(BaseModel):
    """A file the app fetches itself (e.g. ``objectstore://certs/ca.pem``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1)


class ValueFrom(BaseModel):
    """Exactly one of ``secretKeyRef`` / ``fileRef``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    secret_key_ref: SecretKeyRef | None = Field(default=None, alias="secretKeyRef")
    file_ref: FileRef | None = Field(default=None, alias="fileRef")

    @model_validator(mode="after")
    def _exactly_one(self) -> ValueFrom:
        if (self.secret_key_ref is None) == (self.file_ref is None):
            raise ValueError("valueFrom needs exactly one of secretKeyRef or fileRef")
        return self


class FieldSource(BaseModel):
    """Exactly one of ``value`` (a JSON scalar, type preserved) / ``valueFrom``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: JsonScalar = None
    value_from: ValueFrom | None = Field(default=None, alias="valueFrom")

    @model_validator(mode="after")
    def _exactly_one(self) -> FieldSource:
        # ``value: null`` is a legitimate literal, so presence is read off
        # ``model_fields_set`` rather than ``is None``.
        if ("value" in self.model_fields_set) == (self.value_from is not None):
            raise ValueError("a field needs exactly one of value or valueFrom")
        return self


def is_v2(raw: Mapping[str, Any]) -> bool:
    """True only for an int ``format`` of exactly 2 (not ``True``, ``"2"``, ``2.0``)."""
    fmt = raw.get("format")
    return type(fmt) is int and fmt == 2


def parse_v2_fields(raw: Mapping[str, Any]) -> dict[str, FieldSource]:
    """Parse every non-envelope key of a v2 payload as a :class:`FieldSource`.

    Pass only the keys the payload actually set (``exclude_unset``), not a
    defaults-filled dump.

    Raises:
        CredentialParseError: ``raw`` is not v2, mixes in ``key-type`` /
            ``secret-path``, or has a field that is not a valid source. The
            message names the field, never its value.
    """
    if not is_v2(raw):
        raise CredentialParseError("agent_json is not a format 2 payload")
    for key in _V1_FETCH_KEYS:
        if raw.get(key):
            raise CredentialParseError(
                f"agent_json format 2 cannot be combined with '{key}'; "
                "declare each field's source instead"
            )
    fields: dict[str, FieldSource] = {}
    for key, value in raw.items():
        if key in V2_ENVELOPE_KEYS or (key in _V1_FETCH_KEYS and not value):
            continue
        try:
            fields[key] = FieldSource.model_validate(value)
        except ValidationError as exc:
            # No input or cause in the message: it would quote the payload.
            reasons = "; ".join(
                f"{'.'.join(str(p) for p in e['loc']) or 'value'}: {e['msg']}"
                for e in exc.errors(
                    include_url=False, include_input=False, include_context=False
                )
            )
            raise CredentialParseError(
                f"agent_json field '{key}' is not a valid source ({reasons})"
            ) from None
    return fields
