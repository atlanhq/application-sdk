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

from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


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
