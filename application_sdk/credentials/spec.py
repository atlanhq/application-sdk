"""AgentCredentialSpec — typed Pydantic envelope for agent-shape credential payloads.

The base accepts arbitrary connector-specific dotted keys
(``basic.username``, ``noauth.extra.security_protocol``, …) via
``extra="allow"``.  **This permissiveness will be removed in a future
breaking release.**  Connectors should subclass :class:`AgentCredentialSpec`,
declare every dotted key as an explicit field with an alias, and
override ``model_config`` with ``extra="forbid"`` so unknown keys fail
validation at parse time.  See :class:`AgentCredentialSpec` for an
example.
"""

from __future__ import annotations

from typing import Any, cast

import orjson
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    GetJsonSchemaHandler,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import CoreSchema

#: ``agent_json`` wire format in which every field declares its own source
#: (FND-4361). Without it, ``key-type: single-key`` has to look every value up
#: as a secret name and guess from the store's answer.
DECLARED_SOURCES_FORMAT = 2

#: Reserved key carrying the declared ``secretKeyRef`` sources of a normalized
#: format-2 payload: ``{field: {"name": ..., "key": ...}}``. Present (even
#: empty) means every field declared its source; absent means a legacy payload.
VALUE_SOURCES_KEY = "value-sources"

_FORMAT_KEY = "format"

#: Keys that describe the agent rather than a credential field. Format 2 takes
#: them as plain values; every other key must declare a source.
_ENVELOPE_KEYS: frozenset[str] = frozenset(
    {
        "agent-name",
        "secret-manager",
        "aws-region",
        "aws-auth-method",
        "azure-auth-method",
        "auth-type",
        "agent-type",
        "connectBy",
    }
)

#: Legacy fetch anchors. Format 2 replaces both with per-field sources, so a
#: payload carrying either would be ambiguous.
_LEGACY_ANCHORS: tuple[str, ...] = ("key-type", "secret-path")


class AgentCredentialSpec(BaseModel):
    """Typed envelope for an agent-shape credential payload.

    Covers the fields that are consistent across all connectors.
    Connector-specific dotted keys (``basic.username``,
    ``noauth.extra.security_protocol``, …) are currently accepted as
    extras via ``extra="allow"`` and are available via
    :meth:`to_raw_dict`.

    .. warning::

        ``extra="allow"`` will be **removed in a future breaking
        release**.  Connectors must migrate by subclassing this class,
        declaring every dotted key they expect as an explicit field
        with an alias, and setting ``extra="forbid"`` in
        ``model_config``.  Unknown keys then fail validation at parse
        time and contract drift is caught early.  The resolver flow
        (:meth:`to_raw_dict` → secret-bundle substitution → flat dict)
        is unchanged for any subclass.

        Example — connector-specific subclass::

            from pydantic import ConfigDict, Field
            from application_sdk.credentials import AgentCredentialSpec

            class MyAppAgentSpec(AgentCredentialSpec):
                model_config = ConfigDict(
                    extra="forbid", frozen=True, populate_by_name=True,
                )

                api_token: str = Field(default="", alias="api.token")
                api_endpoint: str = Field(default="", alias="api.endpoint")
                extra_region: str = Field(default="", alias="extra.region")

        Wire it into the extraction input by overriding the field type::

            from application_sdk.templates.contracts.sql_metadata import (
                ExtractionInput,
            )

            class MyAppExtractionInput(ExtractionInput):
                agent_json: MyAppAgentSpec | None = None
    """

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        populate_by_name=True,
    )

    # ---- Envelope fields (all optional, default to empty) ----

    agent_name: str = Field(default="", alias="agent-name")
    """Name of the Secure Agent instance. Used by ``is_populated()`` to
    determine if this spec carries a real agent payload."""

    secret_manager: str = Field(default="", alias="secret-manager")
    """Secret store backend: ``awssecretmanager``, ``azurekeyvault``,
    ``gcpsecretmanager``, ``kubernetes``, ``custom``, etc."""

    secret_path: str = Field(default="", alias="secret-path")
    """Path / ARN / name of the secret in the external secret manager."""

    @field_validator("secret_path", mode="before")
    @classmethod
    def _strip_secret_path(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v

    auth_type: str = Field(default="", alias="auth-type")
    """Authentication strategy: ``basic``, ``noauth``, ``gcp-wif``,
    ``iam_role``, ``jwt``, ``client_credentials``, etc."""

    # ---- Common optional fields ----

    host: str = ""
    """Database / service hostname. Required for JDBC connectors."""

    port: int = 0
    """Database / service port."""

    connect_by: str = Field(default="", alias="connectBy")
    """Connection method hint (``host``, ``url``, etc.)."""

    agent_type: str = Field(default="", alias="agent-type")
    """Agent framework version. ``new-app-framework`` for SA 2.0 agents."""

    key_type: str = Field(default="", alias="key-type")
    """Secret key layout: ``multi-key``, ``single-key``, etc."""

    aws_region: str = Field(default="", alias="aws-region")
    """AWS region for the secret manager."""

    aws_auth_method: str = Field(default="", alias="aws-auth-method")
    """AWS auth method: ``iam``, ``iam-assume-role``, ``access-key``."""

    azure_auth_method: str = Field(default="", alias="azure-auth-method")
    """Azure auth method: ``managed_identity``, ``service_principal``."""

    value_sources: dict[str, dict[str, str]] | None = Field(
        default=None, alias=VALUE_SOURCES_KEY
    )
    """Declared ``secretKeyRef`` sources, ``{field: {"name", "key"}}``, of a
    format-2 payload. ``None`` for a legacy payload; an empty dict when every
    field is a literal or a file reference. Resolution looks up only these
    fields and never probes the rest."""

    # ---- Wire-format acceptance ----

    @model_validator(mode="before")
    @classmethod
    def _accept_string_or_dict(cls, data: Any) -> Any:
        """Accept a JSON string, dict, or existing spec instance.

        The type layer of the field's ingress, and the only one: this is what
        :func:`application_sdk.credentials.ingress.normalize_agent_json` calls,
        and that function is the only lenient entry point — it turns the
        exception raised here into "no agent reference". Readers take the typed
        field; a reader catching a parse error is a guard that should have been
        an ingress call.
        """
        if isinstance(data, str):
            if not data or data.strip() in ("", "{}"):
                return {"agent-name": ""}
            try:
                parsed = orjson.loads(data)
            # conformance: ignore[E004] convert-and-rethrow: orjson parse failure is immediately re-raised as typed CredentialParseError
            except Exception as exc:
                from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                    CredentialParseError,
                )

                raise CredentialParseError(
                    f"agent_json is not valid JSON: {exc}",
                    cause=exc,
                ) from exc
            if not isinstance(parsed, dict):
                from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                    CredentialParseError,
                )

                raise CredentialParseError(
                    f"agent_json must be a JSON object, got {type(parsed).__name__}",
                )
            data = parsed
        if isinstance(data, dict):
            if _FORMAT_KEY in data:
                return _normalize_declared(data)
            _check_value_sources(data)
        return data

    @model_serializer(mode="wrap")
    def _omit_absent_sources(self, handler: SerializerFunctionWrapHandler) -> Any:
        """Leave a legacy payload's serialized form exactly as it was: apps and
        older SDKs in child workflows read it, and must not see a new key."""
        data = handler(self)
        if self.value_sources is None and isinstance(data, dict):
            data.pop(VALUE_SOURCES_KEY, None)
            data.pop("value_sources", None)
        return data

    @classmethod
    def __get_pydantic_json_schema__(
        cls, core_schema: CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        # The wrap serializer above returns ``Any``, which would blank the
        # serialization schema of every input model embedding this spec. It
        # only omits a ``None`` field, so publish the field-derived schema.
        return handler(_without_serializer(core_schema))

    # ---- Convenience methods ----

    def to_raw_dict(self) -> dict[str, Any]:
        """Serialize to a flat dict using original hyphenated key names.

        Combines typed envelope fields (by alias) with all extra
        connector-specific keys. This is the dict the resolution
        pipeline operates on.
        """
        d = self.model_dump(by_alias=True)
        # model_dump with by_alias already includes extras with their
        # original keys — no extra work needed.
        return d

    def is_populated(self) -> bool:
        """True if this spec can resolve: a name plus a fetch anchor
        (secret_path, key_type "single-key", or declared value sources).
        Rejects the name-only AE placeholder that would otherwise resolve to
        empty credentials.
        """
        return bool(self.agent_name) and (
            bool(self.secret_path)
            or self.key_type == "single-key"
            or self.value_sources is not None
        )

    def declares_sources(self) -> bool:
        """True for a format-2 payload, whose fields each declare their source."""
        return self.value_sources is not None

    def __hash__(self) -> int:
        # pydantic's frozen hash hashes field values, and ``value_sources`` is
        # a dict. Hash the canonical dump instead: equal specs dump equally.
        return hash(
            orjson.dumps(self.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)
        )


def _without_serializer(core_schema: CoreSchema) -> CoreSchema:
    """*core_schema* minus its custom serializer, at whichever level it sits."""
    schema: dict[str, Any] = {
        k: v for k, v in core_schema.items() if k != "serialization"
    }
    inner = schema.get("schema")
    if isinstance(inner, dict) and "serialization" in inner:
        schema["schema"] = {k: v for k, v in inner.items() if k != "serialization"}
    return cast("CoreSchema", schema)


def _parse_error(message: str) -> Exception:
    from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
        CredentialParseError,
    )

    return CredentialParseError(message)


def _source_error(field: str, problem: str) -> Exception:
    # Names the field only: field names aren't secret, their values may be.
    return _parse_error(f"agent_json field '{field}' {problem}")


def _non_empty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _secret_key_ref(field: str, ref: Any) -> dict[str, str]:
    if (
        not isinstance(ref, dict)
        or set(ref) != {"name", "key"}
        or not _non_empty_str(ref["name"])
        or not _non_empty_str(ref["key"])
    ):
        raise _source_error(
            field, "has a 'secretKeyRef' without a non-empty 'name' and 'key'"
        )
    return {"name": ref["name"], "key": ref["key"]}


def _read_source(field: str, declared: Any) -> tuple[Any, dict[str, str] | None]:
    """``(flat value, secretKeyRef or None)`` for one format-2 field."""
    one_of = "must be an object with exactly one of 'value' or 'valueFrom'"
    if not isinstance(declared, dict) or len(declared) != 1:
        raise _source_error(field, one_of)
    ((kind, body),) = declared.items()
    if kind == "value":
        return body, None
    if kind != "valueFrom":
        raise _source_error(field, one_of)
    if not isinstance(body, dict) or len(body) != 1:
        raise _source_error(
            field, "must hold exactly one of 'secretKeyRef' or 'fileRef' in 'valueFrom'"
        )
    ((source, ref),) = body.items()
    if source == "secretKeyRef":
        secret_ref = _secret_key_ref(field, ref)
        # The flat value is the secret's name, exactly what a legacy payload
        # carried in this field, until resolution replaces it.
        return secret_ref["name"], secret_ref
    if source == "fileRef":
        if (
            not isinstance(ref, dict)
            or set(ref) != {"path"}
            or not _non_empty_str(ref["path"])
        ):
            raise _source_error(field, "has a 'fileRef' without a non-empty 'path'")
        return ref["path"], None
    raise _source_error(
        field, "has an unsupported 'valueFrom' source; use 'secretKeyRef' or 'fileRef'"
    )


def _normalize_declared(data: dict[str, Any]) -> dict[str, Any]:
    """Flatten a format-2 payload into the legacy shape plus ``value-sources``.

    Apps keep reading the same flat keys and types (``port`` stays an int, a
    typed ``extra="forbid"`` subclass validates unchanged); only resolution
    reads the declared sources.
    """
    fmt = data[_FORMAT_KEY]
    if isinstance(fmt, bool) or fmt != DECLARED_SOURCES_FORMAT:
        raise _parse_error(
            f"agent_json format {fmt!r} is not supported; "
            f"expected {DECLARED_SOURCES_FORMAT}"
        )
    flat: dict[str, Any] = {}
    sources: dict[str, dict[str, str]] = {}
    for key, declared in data.items():
        if key == _FORMAT_KEY:
            continue
        if key in _LEGACY_ANCHORS:
            raise _parse_error(
                f"agent_json format {DECLARED_SOURCES_FORMAT} does not take "
                f"'{key}': each field declares its own source"
            )
        if key in (VALUE_SOURCES_KEY, "value_sources"):
            raise _parse_error(f"agent_json key '{key}' is reserved")
        if key in _ENVELOPE_KEYS:
            flat[key] = declared
            continue
        flat[key], secret_ref = _read_source(key, declared)
        if secret_ref is not None:
            sources[key] = secret_ref
    flat[VALUE_SOURCES_KEY] = sources
    return flat


def _check_value_sources(data: dict[str, Any]) -> None:
    """Validate the ``value-sources`` of an already-normalized payload.

    That is the round-trip shape (``to_raw_dict``, a serialized workflow
    input) that a later activity or child workflow re-parses.
    """
    for key in (VALUE_SOURCES_KEY, "value_sources"):
        sources = data.get(key)
        if sources is None:
            continue
        if not isinstance(sources, dict):
            raise _parse_error(f"agent_json '{key}' must be an object")
        for field, ref in sources.items():
            _secret_key_ref(field, ref)
            if not isinstance(data.get(field), str):
                raise _source_error(field, f"is named in '{key}' but carries no value")
