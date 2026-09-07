"""Typed Pydantic v2 mustache-substitution hierarchy for e2e harness.

Field aliases ARE the manifest mustache literals — ``model_dump(by_alias=True)``
yields the exact dict the ``_apply_mustache_subs`` walker consumes (it matches
whole ``{{...}}`` strings as dict keys). No hand-written ``to_mustache_map()``.

Hierarchy:

* :class:`MustacheSubstitutions` — universal three every AE-orchestrated
  connector needs: credential, credential-guid, connection.
* :class:`ConnectionDeleteSubstitutions` — what a *teardown* submit carries, so
  the delete app's manifest tokens resolve if Heracles publishes that manifest
  over the harness's DAG.
* :class:`SQLMustacheSubstitutions` — SQL additions: extraction-method,
  agent-json, filters, preflight-check.
* Per-connector subclasses live in ``app/generated/_e2e_substitutions.py``
  (codegen'd from ``contract/app.pkl`` via ``generateE2ESubstitutionsPy()``
  in ``contract-toolkit/src/App.pkl``).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from application_sdk.contracts.types import ConnectionRef
from application_sdk.testing.e2e.credential import CredentialBody

# Module-level, and it has to be this direction: ``harness`` imports nothing
# from ``e2e`` at module scope, so this edge is one-way. The mirror import — a
# ``harness.teardown`` module importing this one at module scope — closes a
# cycle through ``e2e/__init__`` → ``base`` → ``harness.teardown``, which is why
# ``_dag`` reaches this module from inside a function instead.
from application_sdk.testing.harness.teardown._dag import DeleteType


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


class ConnectionDeleteSubstitutions(MustacheSubstitutions):
    """The three mustache keys ``atlan-connection-delete-app``'s manifest declares.

    Not decoration, and not for a connector: this is what a *teardown* submit
    carries. At submit Heracles may publish the delete app's own manifest over
    the DAG the harness published (see
    :mod:`application_sdk.testing.harness.teardown._dag` for why that is a race
    the harness cannot win, only defuse), and that manifest's ``args`` are
    ``{{connection-qualified-name}}``, ``{{delete-type}}`` and
    ``{{delete-assets}}``, substituted from these parameter rows. Without them
    the app falls back to its own manifest defaults — and its ``delete_type``
    default is ``SOFT``, which archives every e2e run's assets instead of
    removing them and leaves them answering searches on a shared tenant.

    Aliases are the manifest's literals, as everywhere in this hierarchy, so
    what a reviewer diffs against the app's ``manifest.json`` is a declaration
    rather than rows assembled at a call site.

    Attributes:
        connection_qualified_name: The connection to delete. Must be one the run
            minted for itself.
        delete_type: How thoroughly. ``PURGE`` for teardown — see
            :class:`~application_sdk.testing.harness.teardown.DeleteType`.
        delete_assets: Whether to drain the connection's assets before deleting
            the Connection entity.
    """

    connection_qualified_name: str = Field(alias="{{connection-qualified-name}}")
    delete_type: DeleteType = Field(default=DeleteType.PURGE, alias="{{delete-type}}")
    delete_assets: bool = Field(default=True, alias="{{delete-assets}}")


class SQLMustacheSubstitutions(MustacheSubstitutions):
    """SQL-flavour additions: filters, agent-json, extraction-method.

    Used by :class:`~application_sdk.testing.e2e.sql_app.SQLAppE2ETest`;
    subclassable further for SQL connectors whose manifest declares
    additional mustache keys (driven by the connector's ``app.pkl`` via
    codegen — see ``generateE2ESubstitutionsPy()`` in
    ``contract-toolkit/src/App.pkl``).
    """

    extraction_method: str = Field(default="", alias="{{extraction-method}}")
    agent_json: dict[str, Any] | None = Field(default=None, alias="{{agent-json}}")
    include_filter: str = Field(default="", alias="{{include-filter}}")
    exclude_filter: str = Field(default="", alias="{{exclude-filter}}")
    exclude_table_regex: str = Field(default="", alias="{{exclude-table-regex}}")
    # Rendered as a STRING to match the generated ExtractionInput contract, which
    # the contract-toolkit types ``preflight_check: str`` (a bool here fails
    # pydantic validation on str-typed connectors: glue/dremio/iceberg/clickhouse).
    # The value is inert on the extract path (preflight runs via a separate handler).
    preflight_check: str = Field(default="true", alias="{{preflight-check}}")
