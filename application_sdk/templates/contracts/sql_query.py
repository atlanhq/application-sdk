"""Typed contracts for SQL query extraction.

These replace the ``Dict[str, Any]`` interfaces used by
``SQLQueryExtractionWorkflow`` and ``SQLQueryExtractionActivities``.
"""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import MaxItems


@dataclasses.dataclass
class QueryExtractionInput(Input, allow_unbounded_fields=True):
    """Top-level input for a SQL query extraction run."""

    workflow_id: str = ""
    """Temporal workflow ID for this run."""

    connection: dict[str, Any] = dataclasses.field(default_factory=dict)
    """Connection metadata (qualified name, name, etc.)."""

    credential_guid: str = ""
    """GUID of credentials stored in the secret store."""

    output_prefix: str = ""
    """Object store prefix for output artifacts."""

    output_path: str = ""
    """Local or object store path for output files."""

    lookback_days: int = 30
    """Number of days of query history to extract."""

    batch_size: int = 100000
    """Number of queries per batch."""

    exclude_users: Annotated[list[str], MaxItems(1000)] = dataclasses.field(
        default_factory=list
    )
    """Users whose queries should be excluded."""


@dataclasses.dataclass
class QueryExtractionOutput(Output):
    """Top-level output from a SQL query extraction run."""

    workflow_id: str = ""
    success: bool = False
    total_batches: int = 0
    total_queries: int = 0
    records_uploaded: int = 0
    error: str = ""


@dataclasses.dataclass
class QueryBatchInput(Input, allow_unbounded_fields=True):
    """Input for the get_query_batches task."""

    workflow_args: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class QueryBatchOutput(Output):
    """Output from the get_query_batches task."""

    total_batches: int = 0
    batch_size: int = 0
    total_count: int = 0


@dataclasses.dataclass
class QueryFetchInput(Input, allow_unbounded_fields=True):
    """Input for the fetch_queries task."""

    workflow_args: dict[str, Any] = dataclasses.field(default_factory=dict)
    batch_number: int = 0
    batch_size: int = 100000


@dataclasses.dataclass
class QueryFetchOutput(Output):
    """Output from the fetch_queries task."""

    batch_number: int = 0
    queries_fetched: int = 0
    chunk_count: int = 0
