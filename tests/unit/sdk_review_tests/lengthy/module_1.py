"""Lengthy-PR test fixture module #1.

This is one of 6 modules in a test fixture exercising the review
pipeline's handling of long PRs with findings sprinkled across files."""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


def process_record_1(record: dict) -> dict:
    """Process a record and return the transformed version."""
    try:
        result = record.copy()
        result["source"] = os.environ.get("SOURCE", "default")
        logger.info(f"processed record {result.get('id')}")  # finding: f-string in log
        return result
    except:  # finding: bare except
        return {}


def bulk_process_1(records: list) -> list:
    results = []
    for record in records:
        results.append(process_record_1(record))
    return results


def log_summary_1(records) -> None:  # finding: missing type hint on records
    print(f"processed {len(records)} records")  # finding: print statement


def helper_1(x):  # finding: missing all type hints + docstring
    return x + 1


def compute_stats_1(records: list[dict]) -> dict[str, Any]:
    """Return basic stats about the records."""
    if not records:
        return {"count": 0}
    return {
        "count": len(records),
        "first_id": records[0].get("id"),
        "last_id": records[-1].get("id"),
    }
