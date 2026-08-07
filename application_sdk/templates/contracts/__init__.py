"""Typed contracts for built-in App implementations.

BOOT-TIME: lazy re-exports (PEP 562) — eager version imported every SQL
contract module even when a caller needed a single class.
"""

_LAZY_EXPORTS = {
    "ExecuteColumnBatchInput": "application_sdk.templates.contracts.incremental_sql",
    "ExecuteColumnBatchOutput": "application_sdk.templates.contracts.incremental_sql",
    "ExtractionInput": "application_sdk.templates.contracts.sql_metadata",
    "ExtractionOutput": "application_sdk.templates.contracts.sql_metadata",
    "ExtractionTaskInput": "application_sdk.templates.contracts.sql_metadata",
    "ExtractionTaskOutput": "application_sdk.templates.contracts.sql_metadata",
    "FetchColumnsIncrementalInput": "application_sdk.templates.contracts.incremental_sql",
    "FetchColumnsInput": "application_sdk.templates.contracts.sql_metadata",
    "FetchColumnsOutput": "application_sdk.templates.contracts.sql_metadata",
    "FetchDatabasesInput": "application_sdk.templates.contracts.sql_metadata",
    "FetchDatabasesOutput": "application_sdk.templates.contracts.sql_metadata",
    "FetchIncrementalMarkerInput": "application_sdk.templates.contracts.incremental_sql",
    "FetchIncrementalMarkerOutput": "application_sdk.templates.contracts.incremental_sql",
    "FetchProceduresInput": "application_sdk.templates.contracts.sql_metadata",
    "FetchProceduresOutput": "application_sdk.templates.contracts.sql_metadata",
    "FetchSchemasInput": "application_sdk.templates.contracts.sql_metadata",
    "FetchSchemasOutput": "application_sdk.templates.contracts.sql_metadata",
    "FetchTablesIncrementalInput": "application_sdk.templates.contracts.incremental_sql",
    "FetchTablesInput": "application_sdk.templates.contracts.sql_metadata",
    "FetchTablesOutput": "application_sdk.templates.contracts.sql_metadata",
    "FetchViewsInput": "application_sdk.templates.contracts.sql_metadata",
    "FetchViewsOutput": "application_sdk.templates.contracts.sql_metadata",
    "IncrementalExtractionInput": "application_sdk.templates.contracts.incremental_sql",
    "IncrementalExtractionOutput": "application_sdk.templates.contracts.incremental_sql",
    "IncrementalRunContext": "application_sdk.templates.contracts.incremental_sql",
    "IncrementalTaskInput": "application_sdk.templates.contracts.incremental_sql",
    "PrepareColumnQueriesInput": "application_sdk.templates.contracts.incremental_sql",
    "PrepareColumnQueriesOutput": "application_sdk.templates.contracts.incremental_sql",
    "PrimeAuthOutput": "application_sdk.templates.contracts.sql_metadata",
    "QueryBatchInput": "application_sdk.templates.contracts.sql_query",
    "QueryBatchOutput": "application_sdk.templates.contracts.sql_query",
    "QueryExtractionInput": "application_sdk.templates.contracts.sql_query",
    "QueryExtractionOutput": "application_sdk.templates.contracts.sql_query",
    "QueryFetchInput": "application_sdk.templates.contracts.sql_query",
    "QueryFetchOutput": "application_sdk.templates.contracts.sql_query",
    "ReadCurrentStateInput": "application_sdk.templates.contracts.incremental_sql",
    "ReadCurrentStateOutput": "application_sdk.templates.contracts.incremental_sql",
    "TransformInput": "application_sdk.templates.contracts.sql_metadata",
    "TransformOutput": "application_sdk.templates.contracts.sql_metadata",
    "UpdateMarkerInput": "application_sdk.templates.contracts.incremental_sql",
    "UpdateMarkerOutput": "application_sdk.templates.contracts.incremental_sql",
    "UploadInput": "application_sdk.templates.contracts.base_metadata_extraction",
    "UploadOutput": "application_sdk.templates.contracts.base_metadata_extraction",
    "WriteCurrentStateInput": "application_sdk.templates.contracts.incremental_sql",
    "WriteCurrentStateOutput": "application_sdk.templates.contracts.incremental_sql",
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str):
    module_path = _LAZY_EXPORTS.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_path), name)
    globals()[name] = value
    return value
