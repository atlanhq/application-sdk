"""Typed contracts for built-in App implementations."""

from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    FetchColumnsInput,
    FetchColumnsOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesInput,
    FetchTablesOutput,
    TransformInput,
    TransformOutput,
)
from application_sdk.templates.contracts.sql_query import (
    QueryBatchInput,
    QueryBatchOutput,
    QueryExtractionInput,
    QueryExtractionOutput,
    QueryFetchInput,
    QueryFetchOutput,
)

__all__ = [
    # SQL metadata
    "ExtractionInput",
    "ExtractionOutput",
    "FetchColumnsInput",
    "FetchColumnsOutput",
    "FetchDatabasesInput",
    "FetchDatabasesOutput",
    "FetchSchemasInput",
    "FetchSchemasOutput",
    "FetchTablesInput",
    "FetchTablesOutput",
    "TransformInput",
    "TransformOutput",
    # SQL query
    "QueryBatchInput",
    "QueryBatchOutput",
    "QueryExtractionInput",
    "QueryExtractionOutput",
    "QueryFetchInput",
    "QueryFetchOutput",
]
