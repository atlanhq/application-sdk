"""Contract lookup table for v2→v3 method signature migrations.

Used by codemods A3, A4, A5 and the F3 fingerprinter to resolve
method names to their typed Input/Output contracts and timeouts.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MethodContract:
    input_type: str
    output_type: str
    import_module: str
    timeout_seconds: int | None  # None = not a @task (e.g. run())


# Full mapping: (connector_type, method_name) → MethodContract
_CONTRACT_TABLE: dict[tuple[str, str], MethodContract] = {
    # ── sql_metadata ─────────────────────────────────────────────────────────
    ("sql_metadata", "fetch_databases"): MethodContract(
        input_type="FetchDatabasesInput",
        output_type="FetchDatabasesOutput",
        import_module="application_sdk.templates.contracts.sql_metadata",
        timeout_seconds=1800,
    ),
    ("sql_metadata", "fetch_schemas"): MethodContract(
        input_type="FetchSchemasInput",
        output_type="FetchSchemasOutput",
        import_module="application_sdk.templates.contracts.sql_metadata",
        timeout_seconds=1800,
    ),
    ("sql_metadata", "fetch_tables"): MethodContract(
        input_type="FetchTablesInput",
        output_type="FetchTablesOutput",
        import_module="application_sdk.templates.contracts.sql_metadata",
        timeout_seconds=1800,
    ),
    ("sql_metadata", "fetch_columns"): MethodContract(
        input_type="FetchColumnsInput",
        output_type="FetchColumnsOutput",
        import_module="application_sdk.templates.contracts.sql_metadata",
        timeout_seconds=1800,
    ),
    ("sql_metadata", "fetch_procedures"): MethodContract(
        input_type="FetchProceduresInput",
        output_type="FetchProceduresOutput",
        import_module="application_sdk.templates.contracts.sql_metadata",
        timeout_seconds=1800,
    ),
    ("sql_metadata", "transform_data"): MethodContract(
        input_type="TransformInput",
        output_type="TransformOutput",
        import_module="application_sdk.templates.contracts.sql_metadata",
        timeout_seconds=1800,
    ),
    ("sql_metadata", "run"): MethodContract(
        input_type="ExtractionInput",
        output_type="ExtractionOutput",
        import_module="application_sdk.templates.contracts.sql_metadata",
        timeout_seconds=None,
    ),
    # ── sql_query ─────────────────────────────────────────────────────────────
    ("sql_query", "get_query_batches"): MethodContract(
        input_type="QueryBatchInput",
        output_type="QueryBatchOutput",
        import_module="application_sdk.templates.contracts.sql_query",
        timeout_seconds=600,
    ),
    ("sql_query", "fetch_queries"): MethodContract(
        input_type="QueryFetchInput",
        output_type="QueryFetchOutput",
        import_module="application_sdk.templates.contracts.sql_query",
        timeout_seconds=3600,
    ),
    ("sql_query", "run"): MethodContract(
        input_type="QueryExtractionInput",
        output_type="QueryExtractionOutput",
        import_module="application_sdk.templates.contracts.sql_query",
        timeout_seconds=None,
    ),
    # ── handler ───────────────────────────────────────────────────────────────
    ("handler", "test_auth"): MethodContract(
        input_type="AuthInput",
        output_type="AuthOutput",
        import_module="application_sdk.handler.contracts",
        timeout_seconds=None,
    ),
    ("handler", "preflight_check"): MethodContract(
        input_type="PreflightInput",
        output_type="PreflightOutput",
        import_module="application_sdk.handler.contracts",
        timeout_seconds=None,
    ),
    ("handler", "fetch_metadata"): MethodContract(
        input_type="MetadataInput",
        output_type="MetadataOutput",
        import_module="application_sdk.handler.contracts",
        timeout_seconds=None,
    ),
}

CONNECTOR_TYPES: frozenset[str] = frozenset(
    {"sql_metadata", "sql_query", "handler", "incremental_sql"}
)

# V2 class name → connector type (used by F3 fingerprinter)
V2_CLASS_TO_CONNECTOR_TYPE: dict[str, str] = {
    "BaseSQLMetadataExtractionWorkflow": "sql_metadata",
    "BaseSQLMetadataExtractionActivities": "sql_metadata",
    "BaseSQLMetadataExtractionApplication": "sql_metadata",
    "SQLQueryExtractionWorkflow": "sql_query",
    "SQLQueryExtractionActivities": "sql_query",
    "IncrementalSQLMetadataExtractionWorkflow": "incremental_sql",
}

# V3 class name → connector type (for "already migrated" detection)
V3_CLASS_TO_CONNECTOR_TYPE: dict[str, str] = {
    "SqlMetadataExtractor": "sql_metadata",
    "IncrementalSqlMetadataExtractor": "incremental_sql",
    "SqlQueryExtractor": "sql_query",
}


def resolve_method_contract(
    method_name: str,
    connector_type: str | None = None,
) -> MethodContract | None:
    """Resolve a method name to its typed contract.

    When *connector_type* is None, scans all connector types and returns
    the first match found (sql_metadata → sql_query → handler priority).
    """
    if connector_type is not None:
        return _CONTRACT_TABLE.get((connector_type, method_name))

    for ct in ("sql_metadata", "sql_query", "handler"):
        result = _CONTRACT_TABLE.get((ct, method_name))
        if result is not None:
            return result
    return None
