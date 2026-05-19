"""Marker-timestamp validation across incremental SQL contracts (BLDX-518).

``marker_timestamp`` and ``existing_marker`` end up in incremental SQL
templates via ``str.replace``. A workflow input can override the value
(``FetchIncrementalMarkerInput.existing_marker``), so the contract must
constrain it to ISO-8601 / empty — anything else gets rejected before
it reaches a SQL template.

Covers all four contracts that carry a marker field:
``IncrementalTaskInput``, ``FetchIncrementalMarkerInput``,
``FetchIncrementalMarkerOutput``, ``UpdateMarkerInput`` /
``UpdateMarkerOutput``.
"""

from __future__ import annotations

import pytest

from application_sdk.templates.contracts.incremental_sql import (
    FetchColumnsIncrementalInput,
    FetchIncrementalMarkerInput,
    FetchIncrementalMarkerOutput,
    FetchTablesIncrementalInput,
    UpdateMarkerInput,
    UpdateMarkerOutput,
)

# Canonical valid markers covering every shape the contract permits.
_VALID_MARKERS = [
    "",  # empty = full-extraction signal
    "2026-05-18T12:34:56",  # plain ISO, no fractional / tz
    "2026-05-18T12:34:56Z",  # Zulu
    "2026-05-18T12:34:56.123456Z",  # microsecond precision
    "2026-05-18T12:34:56+05:30",  # offset with colon
    "2026-05-18T12:34:56-0700",  # offset without colon
    "2026-05-18 12:34:56",  # space separator (PG/MySQL default)
]

# Malicious / malformed values that must be rejected on every contract.
_INVALID_MARKERS = [
    "2026-05-18T12:34:56'; DROP TABLE x",  # SQL injection via marker
    "not-a-date",  # malformed
    "2026/05/18 12:34:56",  # wrong separator
    "12:34:56",  # time only
    "2026-05-18",  # date only
    "2026-05-18T12:34:56' OR '1'='1",  # classic-OR injection
]


# ---------------------------------------------------------------------------
# IncrementalTaskInput (and subclasses) — marker_timestamp
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cls", [FetchTablesIncrementalInput, FetchColumnsIncrementalInput]
)
class TestIncrementalTaskInputMarker:
    @pytest.mark.parametrize("marker", _VALID_MARKERS)
    def test_valid_marker_accepted(self, cls, marker: str) -> None:
        result = cls.model_validate({"marker_timestamp": marker})
        assert result.marker_timestamp == marker

    @pytest.mark.parametrize("marker", _INVALID_MARKERS)
    def test_invalid_marker_rejected(self, cls, marker: str) -> None:
        with pytest.raises(ValueError, match=r"marker_timestamp must be"):
            cls.model_validate({"marker_timestamp": marker})


# ---------------------------------------------------------------------------
# FetchIncrementalMarkerInput — existing_marker (Optional[str])
# ---------------------------------------------------------------------------


class TestFetchIncrementalMarkerInputMarker:
    def test_none_marker_accepted(self) -> None:
        result = FetchIncrementalMarkerInput.model_validate(
            {"connection_qualified_name": "default/mysql/1", "existing_marker": None}
        )
        assert result.existing_marker is None

    @pytest.mark.parametrize("marker", _VALID_MARKERS)
    def test_valid_marker_accepted(self, marker: str) -> None:
        result = FetchIncrementalMarkerInput.model_validate(
            {
                "connection_qualified_name": "default/mysql/1",
                "existing_marker": marker,
            }
        )
        assert result.existing_marker == marker

    @pytest.mark.parametrize("marker", _INVALID_MARKERS)
    def test_invalid_marker_rejected(self, marker: str) -> None:
        with pytest.raises(ValueError, match=r"marker_timestamp must be"):
            FetchIncrementalMarkerInput.model_validate(
                {
                    "connection_qualified_name": "default/mysql/1",
                    "existing_marker": marker,
                }
            )


# ---------------------------------------------------------------------------
# FetchIncrementalMarkerOutput — both marker fields
# ---------------------------------------------------------------------------


class TestFetchIncrementalMarkerOutputMarker:
    def test_clean_marker_pair_accepted(self) -> None:
        result = FetchIncrementalMarkerOutput.model_validate(
            {
                "marker_timestamp": "2026-05-18T00:00:00Z",
                "next_marker_timestamp": "2026-05-19T00:00:00Z",
            }
        )
        assert result.marker_timestamp == "2026-05-18T00:00:00Z"
        assert result.next_marker_timestamp == "2026-05-19T00:00:00Z"

    def test_invalid_marker_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"marker_timestamp must be"):
            FetchIncrementalMarkerOutput.model_validate(
                {"marker_timestamp": "not-a-date"}
            )

    def test_invalid_next_marker_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"marker_timestamp must be"):
            FetchIncrementalMarkerOutput.model_validate(
                {"next_marker_timestamp": "2026-05-19'; DROP--"}
            )


# ---------------------------------------------------------------------------
# UpdateMarkerInput / UpdateMarkerOutput
# ---------------------------------------------------------------------------


class TestUpdateMarkerContracts:
    def test_input_clean_marker_accepted(self) -> None:
        result = UpdateMarkerInput.model_validate(
            {"next_marker_timestamp": "2026-05-18T00:00:00Z"}
        )
        assert result.next_marker_timestamp == "2026-05-18T00:00:00Z"

    def test_input_invalid_marker_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"marker_timestamp must be"):
            UpdateMarkerInput.model_validate({"next_marker_timestamp": "not-a-date"})

    def test_output_invalid_marker_rejected(self) -> None:
        with pytest.raises(ValueError, match=r"marker_timestamp must be"):
            UpdateMarkerOutput.model_validate(
                {"marker_timestamp": "2026-05-18'; DROP--"}
            )
