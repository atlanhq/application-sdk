"""The shared preflight-outcome capture reads every level the row can arrive at.

The regression these pin is the one that motivated the module: a reader that
watches ``info`` alone returns an empty list for a blocked run — which the gate
emits at ``error`` — and so passes while asserting nothing. Each test below
therefore drives a row in at a non-``info`` level and requires the capture to
still see it.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from application_sdk.observability.events import PREFLIGHT_OUTCOME_EVENT
from application_sdk.observability.logger_adaptor import CHECK_MATRIX_KEY
from application_sdk.testing.preflight import (
    OUTCOME_LEVELS,
    PreflightOutcomeCapture,
    first_outcome_or_none,
    outcome_level,
    outcome_rows,
    single_outcome,
)


def _mock_logger_emitting(level: str, **kwargs: object) -> MagicMock:
    logger = MagicMock()
    getattr(logger, level)(PREFLIGHT_OUTCOME_EVENT, **kwargs)
    return logger


class TestCaptureObject:
    @pytest.mark.parametrize("level", OUTCOME_LEVELS)
    def test_records_the_row_at_every_level(self, level: str) -> None:
        capture = PreflightOutcomeCapture()
        getattr(capture, level)(PREFLIGHT_OUTCOME_EVENT, outcome="blocked")

        assert capture.rows == [{"outcome": "blocked"}]
        assert capture.level == level

    def test_ignores_other_events(self) -> None:
        capture = PreflightOutcomeCapture()
        capture.info("Preflight gate posture", gate_mode="hard")
        capture.error("something else entirely", detail=1)

        assert capture.rows == []
        assert capture.level is None

    def test_debug_is_a_declared_no_op(self) -> None:
        capture = PreflightOutcomeCapture()
        capture.debug("noise")

        assert capture.rows == []

    def test_a_typo_raises_instead_of_yielding_a_callable(self) -> None:
        """No ``__getattr__`` catch-all: a misspelled assertion must fail loudly."""
        with pytest.raises(AttributeError):
            _ = PreflightOutcomeCapture().rowz

    def test_one_asserts_a_single_row(self) -> None:
        capture = PreflightOutcomeCapture()
        capture.error(PREFLIGHT_OUTCOME_EVENT, outcome="blocked")

        assert capture.one == {"outcome": "blocked"}

    def test_one_rejects_a_double_emission(self) -> None:
        """Returning the first match would hide a re-entrant gate emitting twice."""
        capture = PreflightOutcomeCapture()
        capture.error(PREFLIGHT_OUTCOME_EVENT, outcome="blocked")
        capture.info(PREFLIGHT_OUTCOME_EVENT, outcome="proceeded")

        with pytest.raises(
            AssertionError, match="expected exactly 1 outcome row, got 2"
        ):
            _ = capture.one

    def test_one_rejects_no_emission(self) -> None:
        with pytest.raises(
            AssertionError, match="expected exactly 1 outcome row, got 0"
        ):
            _ = PreflightOutcomeCapture().one

    def test_matrix_decodes_the_single_rows_check_matrix(self) -> None:
        capture = PreflightOutcomeCapture()
        capture.error(
            PREFLIGHT_OUTCOME_EVENT,
            outcome="blocked",
            **{CHECK_MATRIX_KEY: '[{"name": "credentialScopes", "passed": false}]'},
        )

        assert capture.matrix == [{"name": "credentialScopes", "passed": False}]

    def test_levels_align_with_rows(self) -> None:
        capture = PreflightOutcomeCapture()
        capture.info(PREFLIGHT_OUTCOME_EVENT, outcome="proceeded")
        capture.error(PREFLIGHT_OUTCOME_EVENT, outcome="blocked")

        assert capture.rows == [{"outcome": "proceeded"}, {"outcome": "blocked"}]
        assert capture.levels == ["info", "error"]


class TestMockLoggerHelpers:
    @pytest.mark.parametrize("level", OUTCOME_LEVELS)
    def test_outcome_rows_scans_every_level(self, level: str) -> None:
        logger = _mock_logger_emitting(level, outcome="blocked")

        assert outcome_rows(logger) == [{"outcome": "blocked"}]

    @pytest.mark.parametrize("level", OUTCOME_LEVELS)
    def test_outcome_level_reports_the_level_used(self, level: str) -> None:
        assert outcome_level(_mock_logger_emitting(level, outcome="blocked")) == level

    def test_outcome_level_is_none_without_a_row(self) -> None:
        assert outcome_level(MagicMock()) is None

    def test_single_outcome_asserts_exactly_one(self) -> None:
        logger = MagicMock()
        logger.error(PREFLIGHT_OUTCOME_EVENT, outcome="blocked")
        logger.info(PREFLIGHT_OUTCOME_EVENT, outcome="proceeded")

        with pytest.raises(
            AssertionError, match="expected exactly 1 outcome row, got 2"
        ):
            single_outcome(logger)

    def test_first_outcome_or_none_tolerates_absence(self) -> None:
        assert first_outcome_or_none(MagicMock()) is None
        assert first_outcome_or_none(
            _mock_logger_emitting("warning", outcome="proceeded")
        ) == {"outcome": "proceeded"}

    def test_rows_ignore_calls_without_positional_message(self) -> None:
        logger = MagicMock()
        logger.error(outcome="blocked")

        assert outcome_rows(logger) == []

    def test_double_emission_reads_the_same_through_both_entry_points(self) -> None:
        """The two mechanisms claim to be the same scan; feed both an info-then-
        error double emission and they must agree — chronological rows, and the
        latest row's level."""
        logger = MagicMock()
        logger.info(PREFLIGHT_OUTCOME_EVENT, outcome="proceeded")
        logger.error(PREFLIGHT_OUTCOME_EVENT, outcome="blocked")

        capture = PreflightOutcomeCapture()
        capture.info(PREFLIGHT_OUTCOME_EVENT, outcome="proceeded")
        capture.error(PREFLIGHT_OUTCOME_EVENT, outcome="blocked")

        assert outcome_rows(logger) == capture.rows
        assert outcome_rows(logger) == [
            {"outcome": "proceeded"},
            {"outcome": "blocked"},
        ]
        assert outcome_level(logger) == capture.level == "error"


class TestFixture:
    def test_fixture_installs_the_capture_on_the_gate_logger(
        self, capture_preflight_outcomes: PreflightOutcomeCapture
    ) -> None:
        from application_sdk.execution._temporal import preflight_gate as pg

        assert pg.logger is capture_preflight_outcomes

        pg.logger.error(PREFLIGHT_OUTCOME_EVENT, outcome="blocked")
        assert capture_preflight_outcomes.one == {"outcome": "blocked"}
        assert capture_preflight_outcomes.level == "error"
