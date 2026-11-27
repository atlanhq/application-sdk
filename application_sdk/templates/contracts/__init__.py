"""Typed contracts for built-in App implementations."""

from typing import TYPE_CHECKING

from application_sdk import _deprecation
from application_sdk.templates.contracts.base_metadata_extraction import (
    UploadToAtlanInput,
    UploadToAtlanOutput,
)
from application_sdk.templates.contracts.incremental_sql import (
    ExecuteColumnBatchInput,
    ExecuteColumnBatchOutput,
    FetchColumnsIncrementalInput,
    FetchIncrementalMarkerInput,
    FetchIncrementalMarkerOutput,
    FetchTablesIncrementalInput,
    IncrementalExtractionInput,
    IncrementalExtractionOutput,
    IncrementalRunContext,
    IncrementalTaskInput,
    PrepareColumnQueriesInput,
    PrepareColumnQueriesOutput,
    ReadCurrentStateInput,
    ReadCurrentStateOutput,
    UpdateMarkerInput,
    UpdateMarkerOutput,
    WriteCurrentStateInput,
    WriteCurrentStateOutput,
)
from application_sdk.templates.contracts.sql_metadata import (
    ExtractionInput,
    ExtractionOutput,
    ExtractionTaskInput,
    ExtractionTaskOutput,
    FetchColumnsInput,
    FetchColumnsOutput,
    FetchDatabasesInput,
    FetchDatabasesOutput,
    FetchProceduresInput,
    FetchProceduresOutput,
    FetchSchemasInput,
    FetchSchemasOutput,
    FetchTablesInput,
    FetchTablesOutput,
    FetchViewsInput,
    FetchViewsOutput,
    FilterMap,
    PrimeAuthOutput,
    TransformInput,
    TransformOutput,
    TreeSelectionExtractionInput,
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
    # Base metadata extraction (deprecated upload_to_atlan task)
    "UploadInput",
    "UploadOutput",
    "UploadToAtlanInput",
    "UploadToAtlanOutput",
    # SQL metadata — base
    "ExtractionInput",
    "ExtractionOutput",
    "ExtractionTaskInput",
    "ExtractionTaskOutput",
    "FetchColumnsInput",
    "FetchColumnsOutput",
    "FetchDatabasesInput",
    "FetchDatabasesOutput",
    "FetchProceduresInput",
    "FetchProceduresOutput",
    "FetchSchemasInput",
    "FetchSchemasOutput",
    "FetchTablesInput",
    "FetchTablesOutput",
    "FetchViewsInput",
    "FetchViewsOutput",
    "FilterMap",
    "TreeSelectionExtractionInput",
    "PrimeAuthOutput",
    "TransformInput",
    "TransformOutput",
    # Incremental SQL
    "ExecuteColumnBatchInput",
    "ExecuteColumnBatchOutput",
    "FetchColumnsIncrementalInput",
    "FetchIncrementalMarkerInput",
    "FetchIncrementalMarkerOutput",
    "FetchTablesIncrementalInput",
    "IncrementalExtractionInput",
    "IncrementalExtractionOutput",
    "IncrementalRunContext",
    "IncrementalTaskInput",
    "PrepareColumnQueriesInput",
    "PrepareColumnQueriesOutput",
    "ReadCurrentStateInput",
    "ReadCurrentStateOutput",
    "UpdateMarkerInput",
    "UpdateMarkerOutput",
    "WriteCurrentStateInput",
    "WriteCurrentStateOutput",
    # SQL query
    "QueryBatchInput",
    "QueryBatchOutput",
    "QueryExtractionInput",
    "QueryExtractionOutput",
    "QueryFetchInput",
    "QueryFetchOutput",
]


#: name -> (replacement, why); served with a warning by ``__getattr__`` below.
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "UploadInput": (
        "application_sdk.templates.contracts.UploadToAtlanInput",
        "the same class, renamed so it no longer shares a name with application_sdk.contracts.storage.UploadInput",
    ),
    "UploadOutput": (
        "application_sdk.templates.contracts.UploadToAtlanOutput",
        "the same class, renamed so it no longer shares a name with application_sdk.contracts.storage.UploadOutput",
    ),
}

if TYPE_CHECKING:
    UploadInput = UploadToAtlanInput
    UploadOutput = UploadToAtlanOutput

__getattr__ = _deprecation.deprecated_module_getattr(
    __name__, _DEPRECATED_CONSTANTS, remove_in="4.0.0"
)
