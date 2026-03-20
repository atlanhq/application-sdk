"""Common models for activity-related data structures.

This module contains Pydantic models used to represent various data structures
needed by activities, such as statistics and configuration.
"""

from enum import Enum
from typing import Any, Dict, List, Optional, TypedDict

from pydantic import BaseModel, Field, model_validator


class ActivityStatistics(BaseModel):
    """Model for storing activity execution statistics.

    This model tracks various metrics about an activity's execution, such as
    the number of records processed and the number of chunks processed.

    Attributes:
        total_record_count: Total number of records processed by the activity.
            Defaults to 0.
        chunk_count: Number of chunks or batches processed by the activity.
            Defaults to 0.
        typename: Optional type identifier for the activity or the data being
            processed. Defaults to None.

    Example:
        >>> stats = ActivityStatistics(
        ...     total_record_count=1000,
        ...     chunk_count=10,
        ...     typename="user_data"
        ... )
        >>> print(f"Processed {stats.total_record_count} records")
    """

    total_record_count: int = 0
    chunk_count: int = 0
    partitions: Optional[List[int]] = []
    typename: Optional[str] = None


class ActivityResult(TypedDict):
    status: str
    message: str
    metadata: Dict[str, Any]


class LhTableWriteMode(str, Enum):
    """Write mode for lakehouse table load."""

    APPEND = "APPEND"
    UPSERT = "UPSERT"


class LhLoadRequest(BaseModel):
    """Request model for MDLH POST /load API."""

    file_keys: Optional[List[str]] = Field(
        default=None, serialization_alias="fileKeys", max_length=100
    )
    patterns: Optional[List[str]] = Field(default=None, max_length=10)
    namespace: str
    table_name: str = Field(serialization_alias="tableName")
    mode: LhTableWriteMode = LhTableWriteMode.APPEND
    catalog_name: Optional[str] = Field(default=None, serialization_alias="catalogName")
    job_id: Optional[str] = Field(default=None, serialization_alias="jobId")

    @model_validator(mode="after")
    def _require_file_keys_or_patterns(self) -> "LhLoadRequest":
        if not self.file_keys and not self.patterns:
            raise ValueError("At least one of file_keys or patterns must be provided")
        return self


class LhLoadResponse(BaseModel):
    """Response from MDLH load submit (HTTP 202)."""

    job_id: str = Field(..., validation_alias="jobId")
    workflow_id: str = Field(..., validation_alias="workflowId")
    status: str


class LhLoadStatusResponse(BaseModel):
    """Response from MDLH load status poll."""

    job_id: str = Field(..., validation_alias="jobId")
    status: str
