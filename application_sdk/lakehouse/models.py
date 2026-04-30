"""Data models for the lakehouse package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class ProcessingResult:
    """Result of processing a single event."""

    status: Literal["SUCCESS", "RETRY", "FAILED"]
    error_message: str | None = None
