"""Data models for the lakehouse consumer framework."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal


@dataclass(frozen=True)
class ProcessingResult:
    """Result of processing a single event."""

    status: Literal["SUCCESS", "RETRY", "FAILED"]
    error_message: str | None = None


@dataclass(frozen=True)
class OutcomeRow:
    """An OUTCOME row to append to the Iceberg table."""

    event_id: str
    asset_id: str
    ingested_at: datetime
    payload: None = field(default=None, init=False)
    status: str = ""
    kind: str = field(default="OUTCOME", init=False)
    retry_count: int = 0
    kafka_partition: None = field(default=None, init=False)
    kafka_offset: None = field(default=None, init=False)
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "asset_id": self.asset_id,
            "ingested_at": self.ingested_at,
            "payload": self.payload,
            "status": self.status,
            "kind": self.kind,
            "retry_count": self.retry_count,
            "kafka_partition": self.kafka_partition,
            "kafka_offset": self.kafka_offset,
            "error_message": self.error_message,
        }

    @classmethod
    def from_event(
        cls,
        event: dict[str, Any],
        result: ProcessingResult,
        retry_count: int,
    ) -> OutcomeRow:
        return cls(
            event_id=event["event_id"],
            asset_id=event["asset_id"],
            ingested_at=datetime.now(timezone.utc),
            status=result.status,
            retry_count=retry_count,
            error_message=result.error_message,
        )
