"""Typed Pydantic v2 mustache-substitution hierarchy for e2e harness.

Field aliases ARE the manifest mustache literals — ``model_dump(by_alias=True)``
yields the exact dict the ``_apply_mustache_subs`` walker consumes (it matches
whole ``{{...}}`` strings as dict keys). No hand-written ``to_mustache_map()``.

Hierarchy:

* :class:`MustacheSubstitutions` — universal three every AE-orchestrated
  connector needs: credential, credential-guid, connection.
* :class:`SQLMustacheSubstitutions` — SQL additions: extraction-method,
  agent-json, filters, preflight-check.
* Per-connector subclasses live in ``app/generated/_e2e_substitutions.py``
  (codegen'd from ``contract/app.pkl`` via ``contract-toolkit``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e.credential import CredentialBody


class MustacheSubstitutions(BaseModel):
    """Universal AE-orchestrated substitutions every connector needs.

    Field aliases ARE the manifest mustache literals — calling
    ``model_dump(by_alias=True)`` yields the exact dict the substitution
    walker consumes (``_apply_mustache_subs`` in base.py matches whole
    strings against the dict keys). Subclasses extend with their own
    mustache keys via aliased fields; never touch raw dicts above the
    walker boundary.
    """

    model_config = ConfigDict(
        frozen=True,
        populate_by_name=True,
        serialize_by_alias=True,
        arbitrary_types_allowed=True,
    )

    credential: CredentialBody | None = Field(default=None, alias="{{credential}}")
    credential_guid: str = Field(
        default="{{credentialGuid}}", alias="{{credential-guid}}"
    )
    connection: ConnectionRef = Field(alias="{{connection}}")


class SQLMustacheSubstitutions(MustacheSubstitutions):
    """SQL-flavour additions: filters, agent-json, extraction-method.

    Used by :class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest`;
    subclassable further for SQL connectors whose manifest declares
    additional mustache keys (driven by the connector's ``app.pkl`` via
    codegen — see ``contract-toolkit/src/E2EOutput.pkl``).
    """

    extraction_method: str = Field(default="", alias="{{extraction-method}}")
    agent_json: dict[str, Any] | None = Field(default=None, alias="{{agent-json}}")
    include_filter: str = Field(default="", alias="{{include-filter}}")
    exclude_filter: str = Field(default="", alias="{{exclude-filter}}")
    exclude_table_regex: str = Field(default="", alias="{{exclude-table-regex}}")
    preflight_check: bool = Field(default=True, alias="{{preflight-check}}")
