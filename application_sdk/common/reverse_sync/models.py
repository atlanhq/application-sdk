"""Shared data contracts for reverse sync across all source apps."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SourceTagInfo:
    """Extracted from mutatedDetails — same format for ALL sources."""

    source_tag_name: str
    source_tag_qualified_name: str
    source_tag_guid: str
    source_tag_connector_name: str
    tag_value: str
    classification_type_name: str = ""
    entity_guid: str = ""


@dataclass
class WriteBackSQL:
    """A single SQL statement to execute against the source."""

    statement: str
    operation: str  # "SET" or "UNSET"
    tag_name: str = ""
    tag_value: str = ""


@dataclass
class ReverseSyncResult:
    """Summary of a batch reverse sync execution."""

    total: int = 0
    passed: int = 0
    synced: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def has_failures(self) -> bool:
        return self.failed > 0

    @property
    def success_rate(self) -> float:
        return self.synced / self.total if self.total > 0 else 0.0
