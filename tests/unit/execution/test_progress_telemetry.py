"""Unit tests for application_sdk.execution.progress_telemetry.

The second warn-mode shape: what a released hold reports, and which of those
reports is worth a log line. The first shape (no-progress gaps) is exercised
through the watchdog loop in ``test_heartbeat.py``, which is where it is
produced.

Assertions run against the observer as the tracker actually calls it — through
``exit_hold`` — rather than against the record function alone, so the wiring is
covered too.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.execution import progress_telemetry as pt_mod
from application_sdk.execution.progress import (
    DEFAULT_MAX_NO_PROGRESS_SECONDS,
    ClosedHold,
    ProgressTracker,
)
from application_sdk.execution.progress_telemetry import (
    closed_hold_observer,
    record_closed_hold,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class FakeClock:
    """A monotonic clock the test advances explicitly.

    Injected rather than patched: an asyncio loop shares ``time.monotonic``, so
    patching it globally makes the loop itself misbehave.
    """

    now: float = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclass
class Observed:
    """What one hold's release reported."""

    recorded: list[Any] = field(default_factory=list)
    infos: list[Any] = field(default_factory=list)
    warnings: list[Any] = field(default_factory=list)

    @property
    def attributes(self) -> dict[str, str]:
        """Attributes of the single recorded observation."""
        assert len(self.recorded) == 1
        return self.recorded[0].args[1]

    @property
    def duration(self) -> float:
        assert len(self.recorded) == 1
        return self.recorded[0].args[0]


def _release(
    *,
    took: float,
    allowance: float | None,
    label: str = "full table scan",
    task_name: str = "extract",
    budget: float = DEFAULT_MAX_NO_PROGRESS_SECONDS,
    record_raises: bool = False,
) -> Observed:
    """Open a hold on a real tracker, let ``took`` seconds pass, release it."""
    clock = FakeClock()
    histogram = MagicMock()
    if record_raises:
        histogram.record.side_effect = RuntimeError("metric backend down")

    tracker = ProgressTracker(
        clock=clock,
        on_hold_closed=closed_hold_observer(task_name, budget_seconds=budget),
    )
    token = tracker.enter_hold(label, allowance)
    clock.advance(took)

    with (
        patch.object(pt_mod, "logger") as mock_logger,
        patch.object(pt_mod, "_hold_duration_histogram", return_value=histogram),
    ):
        tracker.exit_hold(token)

    return Observed(
        recorded=list(histogram.record.call_args_list),
        infos=list(mock_logger.info.call_args_list),
        warnings=list(mock_logger.warning.call_args_list),
    )


# ---------------------------------------------------------------------------
# The metric: every hold, so each site has a distribution to size from
# ---------------------------------------------------------------------------


class TestHoldMetric:
    def test_an_unbounded_hold_is_recorded_as_unbounded(self) -> None:
        observed = _release(took=42.0, allowance=None)

        assert observed.duration == 42.0
        assert observed.attributes == {
            "task.name": "extract",
            "hold.label": "full table scan",
            "hold.bounded": "false",
            "hold.lapsed": "false",
        }

    def test_a_hold_inside_its_allowance_is_bounded_and_not_lapsed(self) -> None:
        observed = _release(took=30.0, allowance=60.0)

        assert observed.attributes["hold.bounded"] == "true"
        assert observed.attributes["hold.lapsed"] == "false"

    def test_a_hold_past_its_allowance_is_lapsed(self) -> None:
        observed = _release(took=90.0, allowance=60.0)

        assert observed.attributes["hold.bounded"] == "true"
        assert observed.attributes["hold.lapsed"] == "true"

    def test_short_holds_are_recorded_too(self) -> None:
        """The metric is not thresholded: choosing an allowance for a site means
        reading that site's whole distribution, not only its tail."""
        observed = _release(took=0.25, allowance=None)

        assert observed.duration == 0.25
        assert not observed.infos

    def test_the_task_name_comes_from_the_observer(self) -> None:
        observed = _release(took=1.0, allowance=None, task_name="fetch_databases")

        assert observed.attributes["task.name"] == "fetch_databases"


# ---------------------------------------------------------------------------
# The log: only the findings that are on the work-list
# ---------------------------------------------------------------------------


class TestWorkListLog:
    def test_a_long_unbounded_hold_is_reported(self) -> None:
        observed = _release(took=1200.0, allowance=None, budget=900.0)

        assert len(observed.infos) == 1
        message = str(observed.infos[0])
        assert "holding_progress" in message
        assert "no declared allowance" in message

    def test_an_unbounded_hold_at_the_budget_is_reported(self) -> None:
        """The boundary is inclusive: a hold that reached the budget is exactly
        one that would have tripped the watchdog had it not been vouched for."""
        observed = _release(took=900.0, allowance=None, budget=900.0)

        assert len(observed.infos) == 1

    def test_a_short_unbounded_hold_is_not_reported(self) -> None:
        observed = _release(took=899.0, allowance=None, budget=900.0)

        assert not observed.infos

    def test_a_lapsed_hold_is_reported_however_short(self) -> None:
        """No SDK-invented threshold is involved: the human declared the number
        and the operation outlived it."""
        observed = _release(took=2.0, allowance=1.0, budget=900.0)

        assert len(observed.infos) == 1
        assert "outliving" in str(observed.infos[0])

    def test_a_long_bounded_hold_inside_its_allowance_is_not_reported(self) -> None:
        """Somebody already looked at this site and sized it — it is not work."""
        observed = _release(took=5000.0, allowance=7200.0, budget=900.0)

        assert not observed.infos

    def test_findings_are_never_logged_at_warning(self) -> None:
        """Warn mode is a fleet-wide default, so a finding is an expected
        observation rather than an actionable failure (ADR-0018)."""
        for took, allowance in ((1200.0, None), (2.0, 1.0)):
            observed = _release(took=took, allowance=allowance, budget=900.0)

            assert observed.infos and not observed.warnings

    def test_an_unlabelled_hold_still_reads(self) -> None:
        observed = _release(took=1200.0, allowance=None, budget=900.0, label="")

        assert "<unlabelled>" in str(observed.infos[0])
        assert observed.attributes["hold.label"] == ""


# ---------------------------------------------------------------------------
# Telemetry must never fail the activity it is only observing
# ---------------------------------------------------------------------------


class TestBestEffort:
    def test_a_metric_failure_still_reports_the_finding(self) -> None:
        observed = _release(
            took=1200.0, allowance=None, budget=900.0, record_raises=True
        )

        assert len(observed.infos) == 1
        assert any("hold duration metric" in str(c) for c in observed.warnings)

    def test_a_metric_failure_never_reaches_the_caller(self) -> None:
        """``exit_hold`` runs in the app's own ``finally`` — it must return
        normally whatever the metric backend is doing. ``_release`` calls it
        unguarded, so returning at all is half the assertion."""
        observed = _release(took=1.0, allowance=None, record_raises=True)

        assert len(observed.recorded) == 1  # attempted, not skipped
        assert observed.warnings  # and reported as a gap in the report


# ---------------------------------------------------------------------------
# The observer itself
# ---------------------------------------------------------------------------


class TestObserver:
    def test_the_default_budget_is_the_no_progress_budget(self) -> None:
        """One number with two uses, not two numbers that can drift apart."""
        histogram = MagicMock()
        with (
            patch.object(pt_mod, "logger") as mock_logger,
            patch.object(pt_mod, "_hold_duration_histogram", return_value=histogram),
        ):
            closed_hold_observer("extract")(
                ClosedHold(
                    label="metadata query",
                    duration_seconds=DEFAULT_MAX_NO_PROGRESS_SECONDS,
                    allowance_seconds=None,
                )
            )

        assert len(mock_logger.info.call_args_list) == 1

    @pytest.mark.parametrize("allowance", [None, 60.0])
    def test_an_unreachable_meter_degrades_to_log_only(
        self, allowance: float | None
    ) -> None:
        """No ``MeterProvider`` at all — both shapes of finding must still be
        named in the log rather than disappearing with the metric."""
        with (
            patch.object(pt_mod, "logger") as mock_logger,
            patch.object(
                pt_mod, "_hold_duration_histogram", side_effect=RuntimeError("no meter")
            ),
        ):
            record_closed_hold(
                ClosedHold(
                    label="metadata query",
                    duration_seconds=10_000.0,
                    allowance_seconds=allowance,
                ),
                task_name="extract",
            )

        assert len(mock_logger.info.call_args_list) == 1
        assert mock_logger.warning.call_args_list
