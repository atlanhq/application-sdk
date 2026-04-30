"""Common models for task execution data structures."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict


@dataclass
class TaskStatistics:
    """Statistics produced by a completed task.

    Attributes:
        total_record_count: Total number of records processed. Defaults to 0.
        chunk_count: Number of chunks or batches processed. Defaults to 0.
        typename: Optional type identifier for the data being processed.
        partitions: Optional list of partition identifiers written.
    """

    total_record_count: int = 0
    chunk_count: int = 0
    partitions: Optional[List[int]] = None
    typename: Optional[str] = None

    def __post_init__(self) -> None:
        if self.partitions is None:
            self.partitions = []


class TaskResult(TypedDict):
    status: str
    message: str
    metadata: Dict[str, Any]
