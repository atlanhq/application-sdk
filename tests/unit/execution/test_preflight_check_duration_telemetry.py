"""Reproduction: the gate publishes handler-authored check durations unvalidated.

``PreflightCheck.duration_ms`` is written entirely by the app — the SDK sets it
in one place (the ``0.0`` default) and reads it in one place
(``_check_matrix_json``). The gate copies whatever it finds into the outcome
event's ``check_matrix``, which is what connector-pulse aggregates.

That number is bounded in reality: the activity is killed at ``start_to_close``,
so no check inside it can have taken longer than the budget. Production rows
carry per-check durations 5-12x above that ceiling, which is physically
impossible as elapsed time. These tests pin the mechanism that lets such a row
exist, so the fix has something to break.
"""

from __future__ import annotations

import time
from unittest import mock

import orjson
import pytest

from application_sdk.execution._temporal.preflight_gate import (
    GATE_TIMEOUT_DEFAULT_SECONDS,
    PreflightGateInput,
    build_preflight_gate_activity,
)
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.observability.logger_adaptor import CHECK_MATRIX_KEY

_GATE = "application_sdk.execution._temporal.preflight_gate"

# The worst production value observed on a completed ``proceeded`` row: a single
# check claiming 292.8s under an SDK that kills the activity at 25s.
_IMPOSSIBLE_DURATION_MS = 292_800.0


class _MisreportingHandler(DefaultHandler):
    """Returns instantly but claims its check took ``duration_ms``.

    Stands in for a handler whose timing is wrong — a unit conversion, or a
    clock started outside the check. The gate cannot tell this apart from an
    honest report, which is the point.
    """

    def __init__(self, duration_ms: float) -> None:
        self._duration_ms = duration_ms

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(
                    name="tablesCheck", passed=True, duration_ms=self._duration_ms
                )
            ],
        )


def _matrix(mock_logger) -> list[dict]:
    rows = [
        c.kwargs
        for c in mock_logger.info.call_args_list
        if c.args and c.args[0] == "Preflight gate outcome"
    ]
    assert len(rows) == 1, f"expected exactly 1 outcome event, got {len(rows)}"
    return orjson.loads(rows[0][CHECK_MATRIX_KEY])


class TestSelfReportedDurationsAreUnvalidated:
    @pytest.mark.asyncio
    async def test_duration_far_above_the_budget_is_published_verbatim(self):
        """The mechanism: a claim 1000x the budget reaches ClickHouse intact."""
        gate = build_preflight_gate_activity(
            _MisreportingHandler(_IMPOSSIBLE_DURATION_MS),
            app_name="myapp",
            enforce=False,
            budget_seconds=0.3,
        )
        started = time.monotonic()
        with mock.patch(f"{_GATE}.logger") as m:
            await gate(PreflightGateInput())
        elapsed_ms = (time.monotonic() - started) * 1000

        assert _matrix(m)[0]["duration_ms"] == _IMPOSSIBLE_DURATION_MS
        # The row asserts 292.8s of work inside an activity that lived
        # milliseconds. Nothing in the gate objects.
        assert elapsed_ms < _IMPOSSIBLE_DURATION_MS

    @pytest.mark.asyncio
    async def test_no_warning_is_emitted_for_an_impossible_duration(self):
        """Nothing flags it, so the fleet had no signal the data was wrong."""
        gate = build_preflight_gate_activity(
            _MisreportingHandler(_IMPOSSIBLE_DURATION_MS),
            app_name="myapp",
            enforce=False,
            budget_seconds=0.3,
        )
        with mock.patch(f"{_GATE}.logger") as m:
            await gate(PreflightGateInput())

        assert m.warning.call_args_list == []

    @pytest.mark.asyncio
    async def test_a_completed_run_cannot_exceed_the_default_budget(self):
        """Why the production rows are provably wrong, stated as an invariant.

        A completed outcome row means the activity finished, and the activity
        cannot outlive the budget. So every ``duration_ms`` on such a row
        describes work that fit inside it — any value above is not elapsed time.
        """
        gate = build_preflight_gate_activity(
            _MisreportingHandler(_IMPOSSIBLE_DURATION_MS),
            app_name="myapp",
            enforce=False,
            budget_seconds=GATE_TIMEOUT_DEFAULT_SECONDS,
        )
        started = time.monotonic()
        with mock.patch(f"{_GATE}.logger") as m:
            await gate(PreflightGateInput())
        elapsed_seconds = time.monotonic() - started

        assert elapsed_seconds < GATE_TIMEOUT_DEFAULT_SECONDS
        reported_seconds = _matrix(m)[0]["duration_ms"] / 1000
        assert reported_seconds > GATE_TIMEOUT_DEFAULT_SECONDS


class TestUnsetDurationDefault:
    """CONNECT-1170 gap 4: the unset default reads as a plausible instant check.

    ``duration_ms`` is app-authored; a handler that never sets it publishes
    ``0.0``, which is indistinguishable from a genuine sub-millisecond check.
    The agreed shape is a ``-1.0`` sentinel (not ``None`` — the key must
    survive ``exclude_none`` and the type stays non-optional).
    """

    def test_unset_duration_is_distinguishable_from_an_instant_check(self) -> None:
        # Behavioural form: what matters is that an unset duration and a
        # genuinely instant check produce different wire values, and that the
        # unset one can never read as elapsed time.
        unset = PreflightCheck(name="tablesCheck", passed=True).duration_ms
        instant = PreflightCheck(
            name="tablesCheck", passed=True, duration_ms=0.0
        ).duration_ms
        assert unset != instant
        assert unset < 0
