"""Common models for task execution data structures."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TypedDict

from typing_extensions import deprecated


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


@deprecated(
    "application_sdk.common.models.TaskResult is deprecated; use "
    "application_sdk.common.models.TaskStatistics for a task's output, or a "
    "TypedDict of your own — will be removed in v4.0."
)
class TaskResult(TypedDict):
    """A ``{status, message, metadata}`` dict.

    .. deprecated:: 3.58.0
        Nothing in the SDK produces or reads it. Use :class:`TaskStatistics`
        or define your own ``TypedDict``. Will be removed in v4.0. Calling a
        ``TypedDict`` builds a plain dict, so this warns only in type checkers
        and conformance (B001), not at runtime.
    """

    status: str
    message: str
    metadata: Dict[str, Any]
