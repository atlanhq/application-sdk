"""AgentCredentialSpec — typed Pydantic model for agent-shape credential payloads.

Replaces the untyped ``str`` / ``dict[str, Any]`` representation of
``agent_json`` with a model that captures the ~80 % of the structure
that is consistent across all connectors, while allowing connector-
and auth-type-specific dotted keys (``basic.username``,
``gcp-wif.extra.project_id``, …) to pass through as ``extra`` fields.

The model accepts three input shapes:

* **JSON string** — the wire format from Argo / Temporal.
* **dict** — an already-parsed dict (e.g. from ``input.model_dump()``).
* **AgentCredentialSpec instance** — returned as-is.

Connector-specific keys (everything not in the typed envelope) land in
:pyattr:`pydantic.BaseModel.model_extra` and are accessible via
:meth:`to_raw_dict`.
"""

from __future__ import annotations

from typing import Any

import orjson
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AgentCredentialSpec(BaseModel):
    """Typed envelope for an agent-shape credential payload.

    Covers the fields that are consistent across all connectors.
    Connector-specific and dotted keys (``basic.username``,
    ``noauth.extra.security_protocol``, …) are captured by
    ``extra="allow"`` and are available via :meth:`to_raw_dict`.

    Examples of payloads this model handles::

        # JDBC — CloudSQL Postgres (basic auth)
        {"agent-name": "cloudsql-postgres-agent", "secret-manager": "awssecretmanager",
         "secret-path": "atlan/dev/cloudsql", "auth-type": "basic",
         "host": "34.x.x.x", "port": 5432,
         "basic.username": "username", "basic.password": "password",
         "extra.database": "postgres"}

        # API — BigQuery (GCP WIF auth)
        {"agent-name": "bigquery-gke-bq-agent", "secret-manager": "gcpsecretmanager",
         "secret-path": "...", "auth-type": "gcp-wif",
         "host": "https://bigquery.googleapis.com", "port": 443,
         "gcp-wif.extra.project_id": "my-project", ...}

        # Streaming — Kafka (noauth)
        {"agent-name": "kafka-agent", "secret-manager": "awssecretmanager",
         "secret-path": "atlan-dev-kafka", "auth-type": "noauth",
         "host": "broker:9092", "port": 9092,
         "noauth.extra.security_protocol": "PLAINTEXT", ...}
    """

    model_config = ConfigDict(
        extra="allow",
        frozen=True,
        populate_by_name=True,
    )

    # ---- Always present (the "envelope") ----

    agent_name: str = Field(default="", alias="agent-name")
    """Name of the Secure Agent instance."""

    secret_manager: str = Field(default="", alias="secret-manager")
    """Secret store backend: ``awssecretmanager``, ``azurekeyvault``,
    ``gcpsecretmanager``, ``kubernetes``, ``custom``, etc."""

    secret_path: str = Field(default="", alias="secret-path")
    """Path / ARN / name of the secret in the external secret manager."""

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
                from application_sdk.credentials.errors import CredentialParseError

                raise CredentialParseError(
                    f"agent_json is not valid JSON: {exc}",
                    cause=exc,
                ) from exc
            if not isinstance(parsed, dict):
                from application_sdk.credentials.errors import CredentialParseError

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
