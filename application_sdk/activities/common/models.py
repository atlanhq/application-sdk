"""Common models for activity-related data structures.

This module contains Pydantic models used to represent various data structures
needed by activities, such as statistics and configuration.
"""

from typing import Optional

from pydantic import BaseModel


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
    typename: Optional[str] = None
