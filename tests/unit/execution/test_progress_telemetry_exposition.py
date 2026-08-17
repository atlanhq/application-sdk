"""The exported Prometheus contract for the ADR-0018 stall signals (FND-293).

``test_progress_telemetry.py`` covers *what the module records*. This file covers
*what leaves the process*, because that is what the alert and the dashboard are
written against — and neither of them is in this repository:

- the alert rule ``AtlanAppTaskStalled`` in ``atlanhq/atlan-alerts``
  (``alerting/rules/App-Platform/atlan-apps-task-stall-alerts.yaml``)
- the panels in ``docs/static/observability/task-stall-dashboard.json``
- the runbook in ``docs/runbooks/stalled-task.md``

The alert stands in for the kill while the fleet is in warn mode (ADR-0018 →
*Migration*), so a silent rename on this side is not a cosmetic break: it
disables the only containment a wedged activity has. Renaming an instrument or an
attribute here is therefore allowed, but it must fail this test first and be
carried through to the rule, the panels and the runbook.

Everything is exercised through the **real** ``EnrichedPrometheusMetricReader``
rather than by asserting on the attribute dicts, because the interesting
translation happens in the exporter: dots become underscores, ``unit="s"``
appends ``_seconds``, and ``app.name`` is inlined from the OTel Resource. None of
that is visible from the recording side.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.resources import Resource
from prometheus_client.core import REGISTRY

from application_sdk.execution import progress_telemetry
from application_sdk.execution.progress import ClosedHold, ProgressWatchdogMode

#: The series the alert rule and the dashboard read, and the exact label keys
#: they group and filter by. Kept as data so a failure names the consumer.
GAP_SERIES = "task_no_progress_gap_seconds"
GAP_LABELS = {"app_name", "task_name", "progress_last_label", "watchdog_mode"}
HOLD_SERIES = "task_hold_duration_seconds"
HOLD_LABELS = {"app_name", "task_name", "hold_label", "hold_bounded", "hold_lapsed"}


@pytest.fixture()
def exposition(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Collect the real exposition for whatever the test records.

    The global ``MeterProvider`` is deliberately left alone —
    ``set_meter_provider`` is once-per-process and would make this test order
    dependent — so ``get_meter`` is redirected at the one seam
    ``progress_telemetry`` uses to reach it.
    """
    from application_sdk.observability._prometheus_enrichment import (
        EnrichedPrometheusMetricReader,
    )

    resource = Resource.create({"app.name": "stall-contract", "service.name": "test"})
    reader = EnrichedPrometheusMetricReader(resource=resource)
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    monkeypatch.setattr(otel_metrics, "get_meter", provider.get_meter)
    # The instruments are process-wide singletons built on first use; a previous
    # test may already have bound them to another provider.
    monkeypatch.setattr(progress_telemetry, "_INSTRUMENTS", {})

    def collect() -> dict[str, dict[str, list[Any]]]:
        """Map ``family name -> sample name -> samples``."""
        out: dict[str, dict[str, list[Any]]] = {}
        for family in reader._collector.collect():
            for sample in family.samples:
                out.setdefault(family.name, {}).setdefault(sample.name, []).append(
                    sample
                )
        return out

    try:
        yield collect
    finally:
        try:
            provider.shutdown()
        except Exception:  # noqa: S110 — benign if the collector is already gone
            pass
        try:
            REGISTRY.unregister(reader._collector)
        except Exception:  # noqa: S110 — shutdown() already unregistered it
            pass


class TestNoProgressGapExposition:
    """The series ``AtlanAppTaskStalled`` alerts on."""

    def test_series_name_and_label_keys(self, exposition) -> None:
        progress_telemetry.record_no_progress_gap(
            "fetch_tables", 947.0, "fetch_tables.page", ProgressWatchdogMode.WARN
        )
        families = exposition()

        assert GAP_SERIES in families, (
            f"The alert reads {GAP_SERIES}_sum; the exporter emitted "
            f"{sorted(families)} instead. If the instrument or its unit was "
            "renamed, update the atlan-alerts rule and the dashboard too."
        )
        counts = families[GAP_SERIES][f"{GAP_SERIES}_count"]
        assert set(counts[0].labels) == GAP_LABELS, (
            "The alert groups by these labels exactly; a change here changes what "
            f"the page says. Expected {sorted(GAP_LABELS)}, got "
            f"{sorted(counts[0].labels)}."
        )

    def test_sum_is_gap_seconds(self, exposition) -> None:
        """The threshold is ``increase(..._sum[1h]) >= 3600`` — so seconds."""
        progress_telemetry.record_no_progress_gap(
            "fetch_tables", 947.0, "fetch_tables.page", ProgressWatchdogMode.WARN
        )
        sums = exposition()[GAP_SERIES][f"{GAP_SERIES}_sum"]
        assert sums[0].value == pytest.approx(947.0)

    def test_task_and_last_label_reach_the_page(self, exposition) -> None:
        """The two things the runbook's step 3 branches on."""
        progress_telemetry.record_no_progress_gap(
            "fetch_tables", 947.0, "fetch_tables.page", ProgressWatchdogMode.WARN
        )
        labels = exposition()[GAP_SERIES][f"{GAP_SERIES}_count"][0].labels
        assert labels["task_name"] == "fetch_tables"
        assert labels["progress_last_label"] == "fetch_tables.page"
        assert labels["app_name"] == "stall-contract", (
            "app.name is inlined from the OTel Resource, not passed by the "
            "recorder; without it the alert cannot name the connector."
        )

    def test_never_signalled_attempt_keeps_an_empty_label(self, exposition) -> None:
        """An attempt that never reported progress must still be alertable.

        The alert filters on ``app_name`` and ``task_name`` but deliberately not
        on ``progress_last_label``: empty means *it went quiet at the top of the
        task*, which is a finding rather than a series to drop.
        """
        progress_telemetry.record_no_progress_gap(
            "fetch_tables", 947.0, "", ProgressWatchdogMode.WARN
        )
        labels = exposition()[GAP_SERIES][f"{GAP_SERIES}_count"][0].labels
        assert labels["progress_last_label"] == ""
        assert labels["task_name"] == "fetch_tables"

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            (ProgressWatchdogMode.WARN, "warn"),
            (ProgressWatchdogMode.ENFORCE, "enforce"),
        ],
    )
    def test_watchdog_mode_values(
        self, exposition, mode: ProgressWatchdogMode, expected: str
    ) -> None:
        """The dashboard splits reported-only from killed on these two values."""
        progress_telemetry.record_no_progress_gap("fetch_tables", 947.0, "page", mode)
        labels = exposition()[GAP_SERIES][f"{GAP_SERIES}_count"][0].labels
        assert labels["watchdog_mode"] == expected


class TestHoldDurationExposition:
    """The series the per-app hold work-list panels read."""

    def test_series_name_and_label_keys(self, exposition) -> None:
        progress_telemetry.record_closed_hold(
            ClosedHold(
                label="run_in_thread.Cursor.execute",
                duration_seconds=1200.0,
                allowance_seconds=None,
            ),
            task_name="query_extraction",
        )
        families = exposition()

        assert (
            HOLD_SERIES in families
        ), f"The work-list panels read {HOLD_SERIES}; got {sorted(families)}."
        counts = families[HOLD_SERIES][f"{HOLD_SERIES}_count"]
        assert set(counts[0].labels) == HOLD_LABELS

    def test_bounded_and_lapsed_are_lowercase_strings(self, exposition) -> None:
        """The panels filter ``hold_bounded="false"`` / ``hold_lapsed="true"``.

        Python's ``str(True)`` is ``"True"``, so this is the one place a plain
        ``str()`` would silently make every panel match nothing.
        """
        progress_telemetry.record_closed_hold(
            ClosedHold(
                label="snapshot metadata query",
                duration_seconds=2000.0,
                allowance_seconds=1800.0,
            ),
            task_name="query_extraction",
        )
        labels = exposition()[HOLD_SERIES][f"{HOLD_SERIES}_count"][0].labels
        assert labels["hold_bounded"] == "true"
        assert labels["hold_lapsed"] == "true"

    def test_unbounded_hold_is_distinguishable(self, exposition) -> None:
        progress_telemetry.record_closed_hold(
            ClosedHold(
                label="run_in_thread.Cursor.execute",
                duration_seconds=1200.0,
                allowance_seconds=None,
            ),
            task_name="query_extraction",
        )
        labels = exposition()[HOLD_SERIES][f"{HOLD_SERIES}_count"][0].labels
        assert labels["hold_bounded"] == "false"
        assert labels["hold_lapsed"] == "false"

    def test_budget_bucket_boundary_exists(self, exposition) -> None:
        """The work-list panel counts holds past the budget as
        ``_count - _bucket{le="1000"}``, so that boundary must exist.

        It is the closest default boundary above the 900s no-progress budget; if
        the exporter's bucket set ever changes, the panel silently counts the
        wrong thing.
        """
        progress_telemetry.record_closed_hold(
            ClosedHold(
                label="run_in_thread.Cursor.execute",
                duration_seconds=1200.0,
                allowance_seconds=None,
            ),
            task_name="query_extraction",
        )
        buckets = exposition()[HOLD_SERIES][f"{HOLD_SERIES}_bucket"]
        boundaries = {s.labels["le"] for s in buckets}
        assert "1000.0" in boundaries, (
            "The le=1000 boundary the hold work-list panel subtracts is gone; "
            f"boundaries are {sorted(boundaries)}."
        )
        over_budget = [
            s for s in buckets if s.labels["le"] == "1000.0" and s.value == 0.0
        ]
        assert over_budget, "A 1200s hold must not be counted under le=1000."
