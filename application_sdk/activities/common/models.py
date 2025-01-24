from typing import Optional

from pydantic import BaseModel


class MetadataModel(BaseModel):
    """Model to store metadata for activities."""

    total_record_count: int = 0
    chunk_count: int = 0
    typename: Optional[str] = None
