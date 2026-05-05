"""Typed Pydantic models for agent-shape credential payloads.

Two base classes are exposed:

* :class:`AgentCredentialSpec` — the original envelope.  Has
  ``extra="allow"`` to accept connector-specific dotted keys
  (``basic.username``, ``noauth.extra.security_protocol``, …) without
  declaring them up-front.  **The ``extra="allow"`` behavior will be
  deprecated soon** — connectors should migrate to a typed subclass.

* :class:`TypedAgentCredentialSpec` — the recommended base for all
  new and migrating connectors.  Has ``extra="forbid"``, so subclasses
  must declare every dotted key they expect.  Unknown keys raise a
  validation error at parse time, catching contract drift early.

Both accept the same input shapes (JSON string / dict / instance) and
flow through the resolver identically via :meth:`to_raw_dict`.
"""

from __future__ import annotations

from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AgentCredentialSpec(BaseModel):
    """Typed envelope for an agent-shape credential payload.

    Covers the fields that are consistent across all connectors.
    Connector-specific and dotted keys (``basic.username``,
    ``noauth.extra.security_protocol``, …) are captured by
    ``extra="allow"`` and are available via :meth:`to_raw_dict`.

    .. warning::

        ``extra="allow"`` here will be **deprecated soon**.  It is kept
        only for connectors whose dotted-key schemas have not yet been
        declared explicitly.  New connectors should subclass
        :class:`TypedAgentCredentialSpec` instead — that base disallows
        extras, so unknown dotted keys fail validation at parse time
        and contract drift is caught early.  Existing connectors should
        plan to migrate; the resolver flow (:meth:`to_raw_dict` →
        secret-bundle substitution → flat dict) is unchanged whether
        the input is a base or a typed subclass.
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

    # ---- Wire-format acceptance ----

    @model_validator(mode="before")
    @classmethod
    def _accept_string_or_dict(cls, data: Any) -> Any:
        """Accept a JSON string, dict, or existing spec instance."""
        if isinstance(data, str):
            if not data or data.strip() in ("", "{}"):
                return {"agent-name": ""}
            try:
                parsed = orjson.loads(data)
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
            return parsed
        return data

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
        """Return True if this spec carries a real agent payload."""
        return bool(self.agent_name)


class TypedAgentCredentialSpec(AgentCredentialSpec):
    """Forward-looking base for agent-shape credential payloads.

    Subclass this (instead of :class:`AgentCredentialSpec`) to declare
    every connector-specific dotted key explicitly.  ``extra="forbid"``
    means unknown keys raise a validation error at parse time, so
    contract drift is caught early instead of failing in some
    downstream consumer.

    Example — subclass with explicit dotted-key fields::

        from pydantic import Field
        from application_sdk.credentials.spec import TypedAgentCredentialSpec

        class MyAppAgentSpec(TypedAgentCredentialSpec):
            api_token: str = Field(default="", alias="api.token")
            api_endpoint: str = Field(default="", alias="api.endpoint")
            extra_region: str = Field(default="", alias="extra.region")

    Wire it into the extraction input by overriding the field type::

        from application_sdk.templates.contracts.sql_metadata import ExtractionInput

        class MyAppExtractionInput(ExtractionInput):
            agent_json: MyAppAgentSpec | None = None

    The resolver flow is unchanged — :meth:`to_raw_dict` returns the
    same flat dict shape, with dotted keys preserved via aliases.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
    )
