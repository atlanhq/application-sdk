"""Clean, idiomatic test fixtures for the application SDK.

This module is part of a test scenario for the @sdk-review pipeline.
It intentionally contains NO review findings — all code follows
the SDK's conventions: %-style logging, explicit exception types,
type hints on public functions, docstrings on public functions.
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
    """Return a canonical lower-case form of ``name`` with surrounding
    whitespace stripped.

    >>> normalize_name(" Alice ")
    'alice'
    """
    return name.strip().lower()


def merge_metadata(
    base: dict[str, Any], override: dict[str, Any]
) -> dict[str, Any]:
    """Return a new dict with ``base`` merged under ``override``.

    Does not mutate inputs; ``override`` wins on key conflicts.
    """
    merged = dict(base)
    merged.update(override)
    return merged


def build_fixture(
    record_id: str,
    name: str,
    *,
    extra: dict[str, Any] | None = None,
) -> FixtureRecord:
    """Construct a :class:`FixtureRecord` with sensible defaults.

    Args:
        record_id: Stable identifier for the record.
        name: Display name (will be normalised).
        extra: Optional additional metadata merged into the record.

    Returns:
        A new :class:`FixtureRecord` with normalised name and
        optional metadata merged.
    """
    metadata: dict[str, Any] = {"source": "test-fixture"}
    if extra is not None:
        metadata = merge_metadata(metadata, extra)

    return FixtureRecord(
        id=record_id,
        name=normalize_name(name),
        metadata=metadata,
    )


def summarise(records: list[FixtureRecord]) -> dict[str, int]:
    """Return a small summary of ``records``: count by source.

    Raises:
        ValueError: if ``records`` is empty.
    """
    if not records:
        raise ValueError("records must be non-empty")

    summary: dict[str, int] = {}
    for record in records:
        source = record.metadata.get("source", "unknown")
        summary[source] = summary.get(source, 0) + 1

    logger.debug("summarised %d records across %d sources", len(records), len(summary))
    return summary
