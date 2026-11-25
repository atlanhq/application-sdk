"""The per-run coverage row for ClickHouse: :data:`LINEAGE_COVERAGE_EVENT`.

Emit once per run, after every asset is assessed::

    logger.info(LINEAGE_COVERAGE_EVENT, **coverage_outcome_fields(counts, app_name))

Only counts leave the run this way. Per-asset rows carry asset names and stay
in the tenant's own store (the run's artifacts, or the app's lakehouse tables).
"""

from __future__ import annotations

from typing import Any

import orjson

from application_sdk.observability.events import LINEAGE_COVERAGE_EVENT
from application_sdk.observability.lineage.assessment import CoverageCounts
from application_sdk.observability.lineage.taxonomy import Verdict
from application_sdk.observability.logger_adaptor import LINEAGE_COVERAGE_SUMMARY_KEY

__all__ = ["LINEAGE_COVERAGE_EVENT", "coverage_outcome_fields"]


def coverage_outcome_fields(counts: CoverageCounts, app_name: str) -> dict[str, Any]:
    """Keyword arguments for the :data:`LINEAGE_COVERAGE_EVENT` log call."""
    rate = counts.coverage_rate
    summary = {
        "gap": {gap.value: n for gap, n in sorted(counts.gaps.items())},
        "category": dict(sorted(counts.categories.items())),
    }
    return {
        "app_name": app_name,
        "lineage_assets_total": counts.total,
        "lineage_valid": counts.verdicts[Verdict.VALID],
        "lineage_invalid": counts.verdicts[Verdict.INVALID],
        "lineage_missing": counts.verdicts[Verdict.MISSING],
        "lineage_valid_none": counts.verdicts[Verdict.VALID_NONE],
        "lineage_pending": counts.verdicts[Verdict.PENDING],
        "lineage_coverage_rate": None if rate is None else round(rate, 4),
        LINEAGE_COVERAGE_SUMMARY_KEY: orjson.dumps(summary).decode(),
    }
