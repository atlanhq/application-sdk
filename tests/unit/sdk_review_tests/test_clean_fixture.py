"""Clean test fixture — no findings expected.

This trivial commit tests that reset-review-status dismisses the
bot's prior approval and removes the sdk-review-approved label.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FixtureRecord:
    """A minimal record used as test data."""

    id: str
    name: str
    metadata: dict[str, Any]


def normalize_name(name: str) -> str:
    """Return canonical lower-case form of name."""
    return name.strip().lower()


def merge_metadata(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with base merged under override."""
    merged = dict(base)
    merged.update(override)
    return merged


def build_fixture(
    record_id: str,
    name: str,
    *,
    extra: dict[str, Any] | None = None,
) -> FixtureRecord:
    """Construct a FixtureRecord with sensible defaults."""
    metadata: dict[str, Any] = {"source": "test-fixture"}
    if extra is not None:
        metadata = merge_metadata(metadata, extra)
    return FixtureRecord(id=record_id, name=normalize_name(name), metadata=metadata)


def summarise(records: list[FixtureRecord]) -> dict[str, int]:
    """Return basic stats about records."""
    if not records:
        raise ValueError("records must be non-empty")
    summary: dict[str, int] = {}
    for record in records:
        source = record.metadata.get("source", "unknown")
        summary[source] = summary.get(source, 0) + 1
    logger.debug("summarised %d records across %d sources", len(records), len(summary))
    return summary
