"""The per-run coverage outcome row and its allowlisted keys."""

from __future__ import annotations

import orjson

from application_sdk.observability.events import (
    LINEAGE_COVERAGE_EVENT,
    OUTCOME_EVENT_NAMES,
)
from application_sdk.observability.lineage import (
    CoverageCounts,
    CustomSqlParserFailure,
    LineageStatus,
    assess,
)
from application_sdk.observability.lineage.outcome import coverage_outcome_fields
from application_sdk.observability.logger_adaptor import _KNOWN_EXTRA_KEYS


def _counts() -> CoverageCounts:
    counts = CoverageCounts()
    counts.extend(
        [
            assess(LineageStatus.HAS_LINEAGE),
            assess(LineageStatus.MISSING_LINEAGE, CustomSqlParserFailure),
            assess(LineageStatus.PENDING),
        ]
    )
    return counts


def test_event_name_is_pinned():
    assert LINEAGE_COVERAGE_EVENT in OUTCOME_EVENT_NAMES


def test_every_field_is_allowlisted():
    fields = coverage_outcome_fields(_counts(), app_name="example-app")
    assert set(fields) <= _KNOWN_EXTRA_KEYS


def test_counts_and_summary():
    fields = coverage_outcome_fields(_counts(), app_name="example-app")
    assert fields["lineage_assets_total"] == 3
    assert fields["lineage_valid"] == 1
    assert fields["lineage_missing"] == 1
    assert fields["lineage_pending"] == 1
    assert fields["lineage_coverage_rate"] == 0.5
    summary = orjson.loads(fields["lineage_coverage_summary"])
    assert summary == {
        "gap": {"PROCESSING": 1},
        "category": {"PARSER_FAILURE": 1},
    }


def test_no_reason_codes_leave_the_run():
    fields = coverage_outcome_fields(_counts(), app_name="example-app")
    assert "CUSTOM_SQL_PARSER_FAILURE" not in orjson.dumps(fields).decode()
