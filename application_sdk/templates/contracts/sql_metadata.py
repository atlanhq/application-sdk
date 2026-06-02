"""Typed contracts for SQL metadata extraction.

These replace the ``Dict[str, Any]`` interfaces used by
``BaseSQLMetadataExtractionWorkflow`` and ``BaseSQLMetadataExtractionActivities``.
"""

from __future__ import annotations

from typing import Annotated, Any

import orjson
from pydantic import (
    AliasChoices,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from application_sdk.common.sql_filters import (
    SAFE_FILTER_PATTERN,
    validate_filter_no_sql_injection,
)
from application_sdk.contracts.base import Input, Output, PublishInputMixin
from application_sdk.contracts.types import ConnectionRef, MaxItems
from application_sdk.credentials.ref import CredentialRef
from application_sdk.credentials.spec import AgentCredentialSpec

# Type alias for SQL filter maps: database-regex → list of schema-regexes.
# Example: {"^prod$": ["^analytics$", "^reporting$"]}
# Bounded: 100 databases × 1000 schemas-per-database — well above any
# realistic source while still keeping payload-size guarantees.
FilterMap = Annotated[
    dict[str, Annotated[list[str], MaxItems(1000)]],
    MaxItems(100),
]

# Backward-compatible aliases: the deny-list moved to ``common.sql_filters``
# in BLDX-518 so the same validation can be applied to raw-dict helper
# callers (``prepare_query``, ``prepare_filters``, ``get_database_names``)
# that bypass Pydantic. Keep the underscore-prefixed name re-exported so
# any in-tree import path remains valid.
_validate_filter_no_sql_injection = validate_filter_no_sql_injection


def _coerce_filter_value(v: Any) -> FilterMap | str:
    """Coerce filter input to FilterMap | str.

    - ``dict`` → pass through (structured filter map from AE)
    - ``list`` → wrap as ``{".*": <list>}``
    - ``str`` → pass through (JSON string or raw regex)
    - ``None`` → empty string
    """
    if v is None:
        return ""
    if isinstance(v, list):
        return {".*": v}
    return v


class ExtractionInput(Input):
    """Top-level input for a SQL metadata extraction run."""

    # Heracles injects the workflow creator as the kebab-case ``user-id`` DAG
    # arg. The base Input validator only *warns* on unknown keys (it does not
    # remap), so without this alias ``user-id`` would be silently dropped and
    # the publish-preflight activity would never fire. populate_by_name keeps
    # construction by the ``user_id`` field name working too (tests, by-name
    # serde round-trips).
    model_config = ConfigDict(populate_by_name=True)

    user_id: str = Field(
        default="",
        validation_alias=AliasChoices("user_id", "user-id"),
    )
    """Keycloak user GUID of the workflow creator (HYP-829 publish preflight)."""

    workflow_id: str = ""
    """Temporal workflow ID for this run."""

    connection: ConnectionRef = Field(default_factory=ConnectionRef)
    """Typed connection reference (qualified name, name, admin users, etc.)."""

    credential_guid: str = ""
    """GUID of credentials stored in the secret store."""

    credential_ref: CredentialRef | None = None
    """Typed credential reference — preferred over credential_guid for new apps."""

    extraction_method: str = ""
    """``"agent"`` or ``"direct"``. Empty defaults to direct."""

    agent_json: AgentCredentialSpec | None = None
    """Typed agent credential spec. Non-None when extraction_method is agent.

    Accepts a JSON string, dict, or :class:`AgentCredentialSpec` on input —
    the spec's model validator normalises all three forms.
    """

    @model_validator(mode="before")
    @classmethod
    def _normalize_ae_payload(cls, data: Any) -> Any:
        """Lift fields from nested ``metadata{}`` and normalize hyphenated keys.

        AE payloads nest config fields (``extraction_method``, ``agent_json``,
        ``credential_guid``, filters, etc.) inside a ``metadata`` dict and may
        use hyphenated key names. This validator promotes them to the top level
        with underscored names so Pydantic can map them to fields.
        """
        if not isinstance(data, dict):
            return data

        # AE passes connection as a JSON string when {{connection}} is substituted.
        # Parse it to a dict so Pydantic can build ConnectionRef correctly.
        # Without this, connection.attributes.qualified_name stays empty and the
        # publish step receives an empty connection_qualified_name.
        raw_conn = data.get("connection")
        if isinstance(raw_conn, str) and raw_conn.strip().startswith("{"):
            try:
                data = {**data, "connection": orjson.loads(raw_conn)}
            except (orjson.JSONDecodeError, ValueError):
                pass  # connection field isn't JSON — leave as-is for Pydantic to handle

        field_names = set(cls.model_fields)
        updates: dict[str, Any] = {}

        metadata = data.get("metadata")
        if isinstance(metadata, dict):
            for key, value in metadata.items():
                underscore_key = key.replace("-", "_")
                if underscore_key in field_names and not data.get(underscore_key):
                    updates[underscore_key] = value

        for key, value in data.items():
            if "-" not in key:
                continue
            underscore_key = key.replace("-", "_")
            if underscore_key in field_names and not data.get(underscore_key):
                updates[underscore_key] = value

        if updates:
            data = {**data, **updates}
        return data

    @model_validator(mode="before")
    @classmethod
    def _skip_agent_json_for_direct(cls, data: Any) -> Any:
        """Null out agent_json when extraction_method is direct.

        In direct mode, agent_json may contain placeholder values (e.g.
        ``"port": "port"``) that fail AgentCredentialSpec validation.
        Since agent_json is only used for agent-based extraction, we
        discard it for direct mode.
        """
        if isinstance(data, dict):
            method = data.get("extraction_method", "")
            if method != "agent" and "agent_json" in data:
                data = {**data, "agent_json": None}
        return data

    output_prefix: str = ""
    """Object store prefix for all output artifacts."""

    output_path: str = ""
    """Local or object store path for output files."""

    exclude_filter: FilterMap | str = Field(default="")
    """Filter for excluding schemas/tables.

    Accepts:
    - ``dict[str, list[str]]`` — structured filter map from AE
    - ``str`` — JSON string or raw regex (backward compat)
    - ``None`` → empty string
    """

    include_filter: FilterMap | str = Field(default="")
    """Filter for including schemas/tables.

    Accepts:
    - ``dict[str, list[str]]`` — structured filter map from AE
    - ``str`` — JSON string or raw regex (backward compat)
    - ``None`` → empty string
    """

    temp_table_regex: Annotated[str, Field(pattern=SAFE_FILTER_PATTERN)] = ""
    """Regex pattern identifying temporary tables."""

    source_tag_prefix: str = ""
    """Tag prefix for source-level metadata."""

    @field_validator("include_filter", "exclude_filter", mode="before")
    @classmethod
    def _coerce_filter(cls, v: Any) -> FilterMap | str:
        return _coerce_filter_value(v)

    @field_validator("include_filter", "exclude_filter", mode="after")
    @classmethod
    def _validate_no_sql_injection(cls, v: FilterMap | str) -> FilterMap | str:
        return _validate_filter_no_sql_injection(v)

    @field_validator("temp_table_regex", mode="after")
    @classmethod
    def _validate_temp_table_no_sql_injection(cls, v: str) -> str:
        validate_filter_no_sql_injection(v)
        return v


class ExtractionOutput(Output, PublishInputMixin):
    """Top-level output from a SQL metadata extraction run."""

    workflow_id: str = ""
    success: bool = False
    databases_extracted: int = 0
    schemas_extracted: int = 0
    tables_extracted: int = 0
    views_extracted: int = 0
    columns_extracted: int = 0
    procedures_extracted: int = 0
    processes_extracted: int = 0
    records_uploaded: int = 0
    error: str = ""
    output_path: str = ""
    """Resolved local base path used during extraction. Subclasses that need
    additional output prefixes (e.g. lineage-specific dirs) can derive them
    from this field instead of re-calling workflow.info()."""


class ExtractionTaskInput(Input):
    """Fields shared by all per-task inputs derived from ExtractionInput.

    Rather than passing a workflow_args dict[str, Any] blob, each task receives
    exactly the typed fields it needs, constructed from the top-level ExtractionInput
    by the run() method.
    """

    workflow_id: str = ""
    connection: ConnectionRef = Field(default_factory=ConnectionRef)
    credential_guid: str = ""
    credential_ref: CredentialRef | None = None
    output_prefix: str = ""
    output_path: str = ""
    exclude_filter: FilterMap | str = Field(default="")
    include_filter: FilterMap | str = Field(default="")
    temp_table_regex: Annotated[str, Field(pattern=SAFE_FILTER_PATTERN)] = ""
    source_tag_prefix: str = ""

    @field_validator("include_filter", "exclude_filter", mode="before")
    @classmethod
    def _coerce_filter(cls, v: Any) -> FilterMap | str:
        return _coerce_filter_value(v)

    @field_validator("include_filter", "exclude_filter", mode="after")
    @classmethod
    def _validate_no_sql_injection(cls, v: FilterMap | str) -> FilterMap | str:
        return _validate_filter_no_sql_injection(v)

    @field_validator("temp_table_regex", mode="after")
    @classmethod
    def _validate_temp_table_no_sql_injection(cls, v: str) -> str:
        validate_filter_no_sql_injection(v)
        return v


class FetchDatabasesInput(ExtractionTaskInput):
    """Input for fetching databases from the source."""


class FetchDatabasesOutput(Output):
    """Output from fetching databases."""

    databases: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    chunk_count: int = 0
    total_record_count: int = 0


class FetchSchemasInput(ExtractionTaskInput):
    """Input for fetching schemas from the source."""


class FetchSchemasOutput(Output):
    """Output from fetching schemas."""

    schemas: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    chunk_count: int = 0
    total_record_count: int = 0


class FetchTablesInput(ExtractionTaskInput):
    """Input for fetching tables from the source."""


class FetchTablesOutput(Output):
    """Output from fetching tables."""

    tables: Annotated[list[str], MaxItems(100000)] = Field(default_factory=list)
    chunk_count: int = 0
    total_record_count: int = 0


class FetchColumnsInput(ExtractionTaskInput):
    """Input for fetching columns from the source."""


class FetchColumnsOutput(Output):
    """Output from fetching columns."""

    chunk_count: int = 0
    total_record_count: int = 0


class FetchProceduresInput(ExtractionTaskInput):
    """Input for fetching stored procedures from the source."""


class FetchProceduresOutput(Output):
    """Output from fetching stored procedures."""

    chunk_count: int = 0
    total_record_count: int = 0


class FetchViewsInput(ExtractionTaskInput):
    """Input for fetching views from the source."""


class FetchViewsOutput(Output):
    """Output from fetching views."""

    chunk_count: int = 0
    total_record_count: int = 0


class TransformInput(ExtractionTaskInput):
    """Input for the transform_data task."""

    typename: str = ""
    file_names: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    chunk_start: int = 0


class TransformOutput(Output):
    """Output from the transform_data task."""

    typename: str = ""
    total_record_count: int = 0
    chunk_count: int = 0
