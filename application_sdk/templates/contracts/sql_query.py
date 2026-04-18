"""Typed contracts for SQL query extraction.

These replace the ``Dict[str, Any]`` interfaces used by
``SQLQueryExtractionWorkflow`` and ``SQLQueryExtractionActivities``.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import ConnectionRef, MaxItems
from application_sdk.credentials.ref import CredentialRef


class QueryExtractionInput(Input, allow_unbounded_fields=True):
    """Top-level input for a SQL query extraction run."""

    workflow_id: str = ""
    """Temporal workflow ID for this run."""

    connection: ConnectionRef = Field(default_factory=ConnectionRef)
    """Typed connection reference (qualified name, name, admin users, etc.)."""

    credential_guid: str = ""
    """GUID of credentials stored in the secret store."""

    credential_ref: CredentialRef | None = None
    """Typed credential reference — preferred over credential_guid for new apps."""

    output_prefix: str = ""
    """Object store prefix for output artifacts."""

    output_path: str = ""
    """Local or object store path for output files."""

    lookback_days: int = 30
    """Number of days of query history to extract."""

    batch_size: int = 100000
    """Number of queries per batch."""

    exclude_users: Annotated[list[str], MaxItems(1000)] = Field(default_factory=list)
    """Users whose queries should be excluded."""


class QueryExtractionOutput(Output):
    """Top-level output from a SQL query extraction run."""

    workflow_id: str = ""
    success: bool = False
    total_batches: int = 0
    total_queries: int = 0
    records_uploaded: int = 0
    error: str = ""


class QueryBatchInput(Input, allow_unbounded_fields=True):
    """Input for the get_query_batches task."""

    workflow_args: dict[str, Any] = Field(default_factory=dict)


class QueryBatchOutput(Output):
    """Output from the get_query_batches task."""

    total_batches: int = 0
    batch_size: int = 0
    total_count: int = 0


class QueryFetchInput(Input, allow_unbounded_fields=True):
    """Input for the fetch_queries task."""

    workflow_args: dict[str, Any] = Field(default_factory=dict)
    batch_number: int = 0
    batch_size: int = 100000


class QueryFetchOutput(Output):
    """Output from the fetch_queries task."""

    batch_number: int = 0
    queries_fetched: int = 0
    chunk_count: int = 0
