"""Typed contracts for SQL metadata extraction.

These replace the ``Dict[str, Any]`` interfaces used by
``BaseSQLMetadataExtractionWorkflow`` and ``BaseSQLMetadataExtractionActivities``.
"""

from __future__ import annotations

from typing import Annotated, Any

import orjson
from pydantic import Field, field_validator, model_validator

from application_sdk.common.sql_filters import (
    SAFE_FILTER_PATTERN,
    normalize_legacy_filter_value,
    validate_filter_no_sql_injection,
)
from application_sdk.contracts.base import Input, Output, PublishInputMixin
from application_sdk.contracts.types import ConnectionRef, FileReference, MaxItems
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
    - ``str`` → normalise the legacy quoted-CSV shape (``'"A","B"'`` →
      ``'A|B'``) so migrated workflow specs from the pre-v3 SaaS-agent
      world still validate; JSON-encoded filter strings and plain
      regex values pass through unchanged (see
      :func:`application_sdk.common.sql_filters.normalize_legacy_filter_value`).
    - ``None`` → empty string
    """
    if v is None:
        return ""
    if isinstance(v, list):
        return {".*": v}
    if isinstance(v, str):
        return normalize_legacy_filter_value(v)
    return v


class ExtractionInput(Input):
    """Top-level input for a SQL metadata extraction run."""

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

    @field_validator("temp_table_regex", mode="before")
    @classmethod
    def _normalize_temp_table_legacy(cls, v: Any) -> Any:
        # Translate the pre-v3 quoted-CSV shape (``'"A","B"'``) to a
        # valid v3 alternation regex (``'A|B'``) before the
        # ``Field(pattern=SAFE_FILTER_PATTERN)`` check runs.
        # Non-string inputs and v3-shape strings pass through.
        return normalize_legacy_filter_value(v)

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

    @field_validator("temp_table_regex", mode="before")
    @classmethod
    def _normalize_temp_table_legacy(cls, v: Any) -> Any:
        return normalize_legacy_filter_value(v)

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


class PrimeAuthOutput(Output):
    """Output from the ``prime_sql_auth`` task (BLDX-1295).

    The task itself is a single serial probe — it issues one ``SELECT 1``
    to populate the SQL server's auth cache (notably MySQL 8's
    ``caching_sha2_password``) before the parallel ``_extract_entity``
    burst.

    Failure semantics — *raise, do not return*:
        ``prime_sql_auth`` raises a typed :class:`AppError` on failure
        (either propagating one the connector raised, or classifying a
        raw driver exception at the catch site).  The standard ``@task``
        wrapper translates the raised ``AppError`` into a Temporal
        ``ApplicationError`` carrying the full ``to_failure_details()``
        envelope, preserving audience / category / code on the wire and
        setting ``non_retryable`` from the leaf's effective retryability.
        Retry stacking is bounded by ``retry_max_attempts=1`` on the
        ``@task`` decorator — the only failure mode that can re-run the
        activity is a Temporal-level event (start-to-close timeout,
        worker eviction, OOM), and those re-run regardless of whether
        the body raises or returns failure data, so there is no
        observability or safety benefit to a bespoke failure envelope.

    This output only carries success-path observability; on failure the
    workflow never receives it because the activity has already raised.
    """

    duration_ms: float = 0.0
    """Wall-clock time spent on the probe connection + ``SELECT 1`` + close."""


class ExtractionTaskOutput(Output):
    """Output from a per-entity ``extract_*`` task.

    Returned by ``SqlApp._extract_entity`` so the matching ``transform_*``
    activity can consume the raw output via the ``FileReference``
    contract rather than reading from local FS directly.

    Cross-worker contract:
        ``raw_file`` is an ephemeral ``FileReference`` pointing at the
        locally-written raw output. The activity interceptor
        auto-uploads it to the object store after the extract activity
        completes and marks it durable. ``run()`` then threads that
        durable ref into the matching transform's ``TransformInput`` —
        the interceptor materialises it onto the transform-worker's
        local filesystem before the transform activity runs, with
        SHA-256 sidecar verification (so the transform sees a
        verified-fresh local copy even when it lands on a different
        pod than the extract).

    File vs directory: a ``FileReference`` can point at either a single
    file or a directory — the interceptor handles both shapes. The v3
    ``SqlApp.template's _extract_entity`` writes exactly one JSONL
    output (``raw/<entity>/records.json``) per entity per run, so the
    ref it produces is a single-file ref. A future connector that
    needs multi-file output (chunked extracts, partitioned writes)
    should write its files under a run-scoped directory (e.g.
    ``raw/<entity>/<run_id>/``) and return a ``FileReference`` pointing
    at that directory — no new contract field needed, the interceptor
    already supports directory refs.
    """

    typename: str = ""
    total_record_count: int = 0
    raw_file: FileReference | None = None
    """``FileReference`` to the extract's raw output.

    For the v3 ``SqlApp`` template, this is a single-file ref pointing
    at ``raw/<entity>/records.json``. Other connectors may point this
    at a directory containing multiple raw output files; the activity
    interceptor handles both shapes transparently.

    ``None`` when the extract returned zero rows — preserves the
    'genuine zero-row extract' signal that publish relies on (the
    matching transform then returns count=0 cleanly without spurious
    asset archival).
    """


class TransformInput(ExtractionTaskInput):
    """Input for transform tasks.

    Extends :class:`ExtractionTaskInput` with the ``raw_file``
    reference threaded in by extract tasks via the ``FileReference``
    interceptor handshake. The activity interceptor auto-materialises
    the referenced data onto the transform worker before the
    transform activity runs.

    ``raw_file`` can point at a single file or a directory — the
    interceptor handles both shapes transparently. The v3 ``SqlApp``
    template's per-entity flow uses single-file refs
    (``raw/<entity>/records.json``); a connector with multi-file
    output should point ``raw_file`` at a run-scoped directory
    instead (no contract change needed).

    Connectors implementing a custom ``transform_data`` (the legacy
    v2 activity) may read ``raw_file`` directly; v3's per-entity
    ``transform_*`` tasks consume it through the helper
    ``SqlApp._transform_entity``.

    The legacy ``file_names`` field remains on the schema as a no-op
    placeholder — it was never populated by the SDK and reading it
    has no effect. ``chunk_start`` / ``typename`` are still present
    for v3 consumers that dispatch by entity. See the field-level
    docstrings for the deprecation status of each.
    """

    typename: str = ""
    """**Deprecated** — kept for backward compatibility with existing
    v3 consumers that read ``input.typename`` to dispatch by entity.
    New connectors should infer typename from the activity name or a
    dedicated dispatch field on a connector-specific input subclass.
    """

    file_names: Annotated[list[str], MaxItems(10000)] = Field(default_factory=list)
    """**Deprecated and unused** — retained on the schema as a no-op
    placeholder for backward compatibility.

    Originally a multi-file batch hint that pre-dated the
    ``FileReference`` interceptor. No SDK code path ever populated
    it on the v3 extract → transform handoff, so any
    ``if input.file_names:`` read evaluates against the empty default
    — the branch was a behavioural no-op even before this deprecation
    note. Reading or writing this field has no effect on extract /
    transform behaviour.

    The modern replacement is :attr:`raw_file` — a ``FileReference``
    that can point at either a single file or a run-scoped directory
    of files. The activity interceptor handles both shapes, so
    connectors that need multi-file output should point ``raw_file``
    at a directory rather than adding a new field.

    This field will be removed in a future major version once a
    deprecation window has elapsed.
    """

    chunk_start: int = 0
    """**Deprecated** — chunk-offset hint used by the legacy
    ``file_names``-based batch flow. The v3 ``SqlApp`` per-entity
    flow streams a single ``records.json`` per entity, so this hint
    has no role; iterate raw data through :attr:`raw_file` instead.
    """

    raw_file: FileReference | None = None
    """Durable ``FileReference`` to the matching extract's raw output.

    Set by ``SqlApp.run()`` from the corresponding
    ``ExtractionTaskOutput.raw_file``. The activity interceptor
    downloads (or sidecar-verifies an existing local copy of) the
    referenced object before the transform activity runs, so
    ``input.raw_file.local_path`` always points to verified-fresh
    local data on whichever worker pod ends up running the transform.

    For the v3 ``SqlApp`` template, this is a single-file ref pointing
    at ``raw/<entity>/records.json``. Other connectors may point this
    at a directory of multiple files; the interceptor handles both.
    """


class TransformOutput(Output):
    """Output from the v3 ``transform_*`` tasks.

    Carries the transformed asset output as a ``FileReference`` so
    downstream publish / upload activities consume it via the same
    auto-materialise contract the framework uses for the
    extract → transform handshake.
    """

    typename: str = ""
    total_record_count: int = 0
    chunk_count: int = 0
    transformed_file: FileReference | None = None
    """``FileReference`` to the transformed asset output.

    Ephemeral on return (``is_durable=False``); the activity interceptor
    auto-uploads it after the transform activity finishes and marks it
    durable so downstream tasks can consume it without local-FS coupling.
    ``None`` when the transform processed zero rows.

    For the v3 ``SqlApp`` template, this is a single-file ref pointing
    at ``transformed/<entity>/entities.json``. A connector with
    multi-file transform output (e.g. partitioned writes) should point
    this at a directory instead — the interceptor handles both shapes.
    """
