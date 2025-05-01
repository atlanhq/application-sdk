from typing import Optional

from pydantic import BaseModel


class ActivityStatistics(BaseModel):
    """Activity Statistics model to store the statistics returned by the activity execution."""

    total_record_count: int = 0
    chunk_count: int = 0
    typename: Optional[str] = None
