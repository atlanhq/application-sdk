"""Data models for the lakehouse package."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class EventResult:
    """Per-event verdict returned by an events handler.

    The handler returns one ``EventResult`` per input event; ``events_ack``
    serialises these into the Parquet ack file the automation engine reads
    to mark events as processed / retried / failed.
    """

    status: Literal["SUCCESS", "RETRY", "FAILED"]
    error_message: str | None = None
