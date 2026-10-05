"""Unit tests for the preflight gate's warmup tier (FND-3039).

The end-to-end behaviour — every way the wait can end, on a real server with
real durable timers — is pinned by ``tests/integration/test_preflight_warmup.py``.
What lives here is what that suite cannot reach cheaply: the check activity's
first-dispatch probe and its row, the ``{app}:preflight_warmup`` poll
activity's reading of a probe's raise, the workflow frame's wait loop on a fake
clock (cadence, ceiling, late ``READY``, postures, health line, transitions),
the fail-open paths for the gate's own plumbing, the declared-value clamps, and
the worker's registration and collision guard.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

import pytest

from application_sdk.app.base import App, _run_preflight_gate
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AppPermissionDeniedError,
    AuthError,
    DependencyUnavailableError,
    NotFoundError,
    SourceUnavailableError,
)
from application_sdk.errors.wire import FailureDetails
from application_sdk.execution._temporal import preflight_gate
from application_sdk.execution._temporal._activity_errors import (
    WorkerActivityNameCollisionError,
)
from application_sdk.execution._temporal.preflight_gate import (
    PREFLIGHT_FAILED_ERROR_TYPE,
    WARMUP_SUGGESTED_ACTIONS,
    WARMUP_TRANSITIONS_MAX,
    PreflightGateInput,
    WarmupOutcome,
    WarmupPoll,
    WarmupTransition,
    WarmupWait,
    build_preflight_gate_activity,
    build_preflight_warmup_activity,
    gate_outcome_row,
    gate_timeouts,
    human_duration,
    preflight_warmup_activity_name,
    warmup_activity_timeouts,
    warmup_exhausted_details,
    warmup_retry_policy,
    warmup_unavailable_details,
    warmup_waiting_details,
)
from application_sdk.execution._temporal.worker import create_worker
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler._preflight_outcome import (
    FAILURE_AUDIENCE_KEY,
    FAILURE_MESSAGE_KEY,
)
from application_sdk.handler._warmup import (
    WARMUP_CEILING_DEFAULT_SECONDS,
    WARMUP_CEILING_MIN_SECONDS,
    WARMUP_PROBE_TIMEOUT_DEFAULT_SECONDS,
    warmup_ceiling_seconds,
    warmup_poll_delay,
    warmup_probe_timeout_seconds,
)
from application_sdk.handler.contracts import (
    CheckTier,
    PreflightCheck,
    PreflightGateMode,
    PreflightInput,
    PreflightOutput,
    WarmupInput,
    WarmupObservation,
    WarmupState,
)
from application_sdk.observability.logger_adaptor import (
    _KNOWN_EXTRA_KEYS,
    CHECK_MATRIX_KEY,
    GATE_CLASSIFICATION_KEY,
    GATE_MODE_KEY,
    GATE_TIER_KEY,
    WARMUP_DURATION_KEY,
    WARMUP_OUTCOME_KEY,
    WARMUP_TRANSITIONS_KEY,
)
from application_sdk.testing import WarmingSource, WarmingSourceHandler

# ---------------------------------------------------------------------------
# Workflow frame
# ---------------------------------------------------------------------------


class _ResolvableInput:
    """Minimal object satisfying the CredentialResolvable protocol."""

    extraction_method = ""
    credential_guid = ""
    agent_json = None


class _Clock:
    """``workflow.now`` and ``workflow.sleep`` over one fake clock."""

    def __init__(self) -> None:
        self.now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.slept: list[float] = []

    def read(self) -> datetime:
        return self.now

    async def sleep(self, duration: timedelta) -> None:
        self.slept.append(duration.total_seconds())
        self.now += duration


class _Activities:
    """Stand-in for ``workflow.execute_activity`` over the two gate activities.

    The poll activity answers ``polls`` in order, its last entry repeating;
    ``polls[0]`` is the first probe, dispatched before any check. The check
    activity raises ``first`` / ``second`` when they are exceptions (its result
    is never read). ``first_takes`` advances the fake clock while the first
    check dispatch runs; ``poll_takes`` while every poll after the first runs.
    ``order`` is every dispatched activity name, in order.
    """

    def __init__(
        self,
        clock: _Clock,
        *,
        polls: Sequence[WarmupPoll | BaseException],
        first: BaseException | None = None,
        second: BaseException | None = None,
        first_takes: float = 0.0,
        poll_takes: float = 0.0,
    ) -> None:
        self._clock = clock
        self._polls = list(polls)
        self._first = first
        self._second = second
        self._first_takes = first_takes
        self._poll_takes = poll_takes
        self.order: list[str] = []
        self.checks: list[PreflightGateInput] = []
        self.check_kwargs: list[dict[str, Any]] = []
        self.polls: list[PreflightGateInput] = []
        self.poll_kwargs: list[dict[str, Any]] = []

    async def __call__(self, name: str, arg: PreflightGateInput, **kwargs: Any) -> Any:
        self.order.append(name)
        answer: WarmupPoll | BaseException | None
        if name == "myapp:preflight":
            self.checks.append(arg)
            self.check_kwargs.append(kwargs)
            if len(self.checks) == 1:
                self._clock.now += timedelta(seconds=self._first_takes)
                answer = self._first
            else:
                answer = self._second
        elif name == "myapp:preflight_warmup":
            self.polls.append(arg)
            self.poll_kwargs.append(kwargs)
            if len(self.polls) > 1:
                self._clock.now += timedelta(seconds=self._poll_takes)
            answer = self._polls[min(len(self.polls), len(self._polls)) - 1]
        else:
            raise AssertionError(f"unexpected activity {name}")
        if isinstance(answer, BaseException):
            raise answer
        return answer

    @property
    def tiers(self) -> list[frozenset[CheckTier] | None]:
        return [c.tiers for c in self.checks]


def _poll(
    state: WarmupState,
    *,
    source_state: str = "",
    queued_queries: int | None = None,
    next_poll_seconds: int | None = None,
    error: FailureDetails | None = None,
) -> WarmupPoll:
    return WarmupPoll(
        observation=WarmupObservation(
            state=state,
            source_state=source_state,
            queued_queries=queued_queries,
            next_poll_seconds=next_poll_seconds,
        ),
        error=error,
    )


def _rows(safe_log: mock.Mock) -> list[dict[str, Any]]:
    return [c.kwargs for c in safe_log.call_args_list if "outcome" in c.kwargs]


@pytest.fixture
def clock() -> Iterator[_Clock]:
    c = _Clock()
    with (
        mock.patch("application_sdk.app.base.workflow.now", side_effect=c.read),
        mock.patch("application_sdk.app.base.workflow.sleep", side_effect=c.sleep),
    ):
        yield c


@pytest.fixture
def safe_log() -> Iterator[mock.Mock]:
    with mock.patch("application_sdk.app.base._safe_log") as m:
        yield m


@pytest.fixture
def health() -> Iterator[mock.Mock]:
    with mock.patch("application_sdk.app.base.workflow.set_current_details") as m:
        yield m


async def _run(activities: _Activities, **kwargs: Any) -> None:
    with (
        mock.patch("application_sdk.app.base.workflow.patched", return_value=True),
        mock.patch(
            "application_sdk.app.base.workflow.execute_activity", new=activities
        ),
    ):
        await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl", **kwargs)


_PREFLIGHT_ONLY = frozenset({CheckTier.PREFLIGHT})
_READY_WAIT = frozenset({CheckTier.WARMUP})
_PROBE = "myapp:preflight_warmup"
_CHECK = "myapp:preflight"


class TestTheFirstProbe:
    async def test_it_is_its_own_activity_before_the_checks(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(clock, polls=[_poll(WarmupState.READY)])
        await _run(activities)
        assert activities.order == [_PROBE, _CHECK]

    async def test_it_runs_on_the_probe_timeouts_not_the_gates(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(clock, polls=[_poll(WarmupState.READY)])
        await _run(
            activities,
            budget_seconds=150,
            max_attempts=2,
            warmup_probe_timeout_seconds=7,
        )
        probe_s2c, probe_sc2 = warmup_activity_timeouts(7)
        gate_s2c, gate_sc2 = gate_timeouts(150, 2)
        (probe,) = activities.poll_kwargs
        assert probe["result_type"] is WarmupPoll
        assert probe["start_to_close_timeout"] == probe_s2c == timedelta(seconds=17)
        assert probe["schedule_to_close_timeout"] == probe_sc2
        assert probe["retry_policy"] == warmup_retry_policy()
        # The check dispatch keeps the gate's whole window: the probe ran
        # before it, on its own clock, and spent none of it.
        (check,) = activities.check_kwargs
        assert check["start_to_close_timeout"] == gate_s2c
        assert check["schedule_to_close_timeout"] == gate_sc2
        assert gate_s2c != probe_s2c

    async def test_ready_is_one_dispatch_with_every_tier_and_an_unchanged_row(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(clock, polls=[_poll(WarmupState.READY)])
        await _run(activities, gate_mode=PreflightGateMode.HARD)
        assert activities.tiers == [None]
        assert activities.checks[0].warmup is None
        assert len(activities.polls) == 1
        assert clock.slept == []
        assert _rows(safe_log) == []
        health.assert_not_called()

    async def test_a_broken_probe_runs_every_tier_and_says_so(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(clock, polls=[RuntimeError("no worker polled")])
        await _run(
            activities,
            gate_mode=PreflightGateMode.HARD,
            warmup_mode=PreflightGateMode.HARD,
        )
        assert activities.order == [_PROBE, _CHECK]
        assert activities.tiers == [None]
        assert activities.checks[0].warmup == WarmupWait(outcome=WarmupOutcome.BROKEN)
        assert _rows(safe_log) == []
        warnings = [c for c in safe_log.call_args_list if c.args[0] == "warning"]
        assert [c.args[1] for c in warnings] == [
            "Preflight warmup probe failed; running every check tier"
        ]
        assert clock.slept == []

    @pytest.mark.parametrize(
        ("gate_mode", "outcome"),
        [(PreflightGateMode.HARD, "blocked"), (PreflightGateMode.SOFT, "would_block")],
    )
    async def test_a_terminal_first_probe_is_a_verdict_with_no_check_dispatch(
        self, clock, safe_log, health, gate_mode, outcome
    ) -> None:
        raised = AuthError(message="expired")
        activities = _Activities(
            clock,
            polls=[_poll(WarmupState.WARMING, error=raised.to_failure_details())],
        )
        if outcome == "blocked":
            with pytest.raises(ApplicationError) as caught:
                await _run(
                    activities,
                    gate_mode=gate_mode,
                    warmup_mode=PreflightGateMode.SOFT,
                )
            assert caught.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        else:
            await _run(
                activities, gate_mode=gate_mode, warmup_mode=PreflightGateMode.HARD
            )
        assert activities.order == [_PROBE]
        (row,) = _rows(safe_log)
        assert row["outcome"] == outcome
        assert row["reason"] == "AUTH"
        assert row[GATE_MODE_KEY] == gate_mode.value
        assert row[GATE_TIER_KEY] == "warmup"
        assert row[WARMUP_OUTCOME_KEY] == "failed"
        assert json.loads(row[WARMUP_TRANSITIONS_KEY]) == [
            {"state": "warming", "at_ms": 0.0}
        ]

    async def test_a_terminal_first_probe_leaves_no_waiting_health_line(
        self, clock, safe_log, health
    ) -> None:
        raised = AuthError(message="expired")
        activities = _Activities(
            clock,
            polls=[_poll(WarmupState.WARMING, error=raised.to_failure_details())],
        )
        await _run(activities, gate_mode=PreflightGateMode.SOFT)
        lines = [c.args[0] for c in health.call_args_list]
        assert not lines or lines[-1] == ""

    @pytest.mark.parametrize(
        ("state", "outcome"),
        [
            (WarmupState.COLD, WarmupOutcome.WARMING),
            (WarmupState.WARMING, WarmupOutcome.WARMING),
            (WarmupState.QUEUED, WarmupOutcome.WARMING),
            (WarmupState.UNAVAILABLE, WarmupOutcome.UNAVAILABLE),
        ],
    )
    async def test_not_ready_runs_the_preflight_tier_with_the_wait_so_far(
        self, clock, safe_log, health, state, outcome
    ) -> None:
        activities = _Activities(clock, polls=[_poll(state), _poll(WarmupState.READY)])
        await _run(activities)
        assert activities.order[:2] == [_PROBE, _CHECK]
        assert activities.tiers[0] == _PREFLIGHT_ONLY
        assert activities.checks[0].warmup == WarmupWait(outcome=outcome)

    async def test_a_dead_preflight_dispatch_fails_open_and_stops_the_wait(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(
            clock,
            polls=[_poll(WarmupState.COLD)],
            first=RuntimeError("worker lost"),
        )
        await _run(
            activities,
            gate_mode=PreflightGateMode.HARD,
            warmup_mode=PreflightGateMode.HARD,
        )
        assert len(activities.polls) == 1
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[GATE_CLASSIFICATION_KEY] == "gate_broken"
        assert row[GATE_TIER_KEY] == "preflight"
        assert row[WARMUP_OUTCOME_KEY] == "warming"
        health.assert_called_with("")

    async def test_a_transient_first_raise_is_named_at_the_ceiling(
        self, clock, safe_log, health
    ) -> None:
        raised = DependencyUnavailableError(message="resume API 503")
        activities = _Activities(
            clock,
            polls=[_poll(WarmupState.WARMING, error=raised.to_failure_details())],
            first_takes=40,
        )
        await _run(activities, warmup_ceiling_seconds=30)
        assert len(activities.polls) == 1
        (row,) = _rows(safe_log)
        assert row["outcome"] == "warmup_exhausted"
        assert row[FAILURE_MESSAGE_KEY] == (
            "Source wasn't ready within 30s; last reported: warming (resume API 503)"
        )


class TestTheWaitCadence:
    async def test_unhinted_polls_back_off_to_thirty_and_stop_at_the_ceiling(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(
            clock, polls=[_poll(WarmupState.COLD), _poll(WarmupState.WARMING)]
        )
        await _run(activities, warmup_ceiling_seconds=120)
        # 5, 10, 20, then the 30s cap; the last wait is cut to land on the ceiling.
        assert clock.slept == [5.0, 10.0, 20.0, 30.0, 30.0, 25.0]
        assert len(activities.polls) == 7
        assert activities.tiers == [_PREFLIGHT_ONLY]

    async def test_a_hint_is_honoured_with_a_five_second_floor(
        self, clock, safe_log, health
    ) -> None:
        """A hinted wait does not advance the unhinted backoff."""
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD, next_poll_seconds=2),
                _poll(WarmupState.WARMING, next_poll_seconds=45),
                _poll(WarmupState.WARMING),
                _poll(WarmupState.WARMING),
                _poll(WarmupState.READY),
            ],
        )
        await _run(activities, warmup_ceiling_seconds=120)
        assert clock.slept == [5.0, 45.0, 5.0, 10.0]
        assert activities.tiers == [_PREFLIGHT_ONLY, _READY_WAIT]

    async def test_a_hint_never_waits_past_the_ceiling(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD, next_poll_seconds=100),
                _poll(WarmupState.WARMING),
            ],
        )
        await _run(activities, warmup_ceiling_seconds=30)
        assert clock.slept == [30.0]
        assert len(activities.polls) == 2
        (row,) = _rows(safe_log)
        assert row["outcome"] == "warmup_exhausted"

    async def test_every_poll_runs_on_the_probe_timeouts(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(
            clock, polls=[_poll(WarmupState.COLD), _poll(WarmupState.READY)]
        )
        await _run(activities, warmup_probe_timeout_seconds=7)
        assert [k["start_to_close_timeout"] for k in activities.poll_kwargs] == [
            timedelta(seconds=17),
            timedelta(seconds=17),
        ]
        assert [p.tiers for p in activities.polls] == [None, None]


class TestReadyRunsTheWarmupTier:
    async def test_the_second_dispatch_carries_what_the_wait_saw(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD),
                _poll(WarmupState.WARMING, source_state="RESUMING"),
                _poll(WarmupState.QUEUED, queued_queries=3),
                _poll(WarmupState.QUEUED, queued_queries=3),
                _poll(WarmupState.READY),
            ],
        )
        await _run(activities)
        assert clock.slept == [5.0, 10.0, 20.0, 30.0]
        assert activities.tiers == [_PREFLIGHT_ONLY, _READY_WAIT]
        # State changes only: the repeated QUEUED poll adds nothing.
        assert activities.checks[1].warmup == WarmupWait(
            outcome=WarmupOutcome.READY,
            duration_ms=65_000.0,
            transitions=[
                WarmupTransition(state=WarmupState.COLD, at_ms=0.0),
                WarmupTransition(state=WarmupState.WARMING, at_ms=5_000.0),
                WarmupTransition(state=WarmupState.QUEUED, at_ms=15_000.0),
                WarmupTransition(state=WarmupState.READY, at_ms=65_000.0),
            ],
        )
        # The frame writes no row: the check activities do.
        assert _rows(safe_log) == []

    async def test_the_health_line_is_set_while_pending_and_cleared(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD),
                _poll(WarmupState.WARMING, source_state="RESUMING"),
                _poll(WarmupState.QUEUED, queued_queries=3),
                _poll(WarmupState.READY),
            ],
        )
        await _run(activities)
        assert [c.args[0] for c in health.call_args_list] == [
            "waiting for source warmup: cold, 0s",
            "waiting for source warmup: RESUMING, 5s",
            "waiting for source warmup: queued, 3 queued, 15s",
            "",
        ]

    async def test_a_health_line_that_cannot_be_set_never_fails_the_gate(
        self, clock, safe_log
    ) -> None:
        activities = _Activities(
            clock, polls=[_poll(WarmupState.COLD), _poll(WarmupState.READY)]
        )
        with mock.patch(
            "application_sdk.app.base.workflow.set_current_details",
            side_effect=RuntimeError("not in workflow"),
        ):
            await _run(activities)
        assert activities.tiers == [_PREFLIGHT_ONLY, _READY_WAIT]

    async def test_a_dead_warmup_dispatch_fails_open_with_the_wait(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(
            clock,
            polls=[_poll(WarmupState.COLD), _poll(WarmupState.READY)],
            second=RuntimeError("worker lost"),
        )
        await _run(
            activities,
            gate_mode=PreflightGateMode.HARD,
            warmup_mode=PreflightGateMode.HARD,
        )
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[GATE_CLASSIFICATION_KEY] == "gate_broken"
        assert row[GATE_TIER_KEY] == "warmup"
        assert row[WARMUP_OUTCOME_KEY] == "ready"
        assert row[WARMUP_DURATION_KEY] == 5_000.0

    async def test_a_block_from_the_warmup_dispatch_is_re_raised_unchanged(
        self, clock, safe_log, health
    ) -> None:
        block = ApplicationError(
            "Preflight failed: catalog scan found no schemas",
            type=PREFLIGHT_FAILED_ERROR_TYPE,
            non_retryable=True,
        )
        activities = _Activities(
            clock,
            polls=[_poll(WarmupState.COLD), _poll(WarmupState.READY)],
            second=block,
        )
        with pytest.raises(ApplicationError) as caught:
            await _run(activities, gate_mode=PreflightGateMode.HARD)
        assert caught.value is block
        assert _rows(safe_log) == []


class TestUnavailable:
    @pytest.mark.parametrize(
        ("gate_mode", "warmup_mode", "outcome"),
        [
            (PreflightGateMode.HARD, PreflightGateMode.SOFT, "would_block"),
            (PreflightGateMode.HARD, None, "would_block"),
            (PreflightGateMode.SOFT, PreflightGateMode.HARD, "blocked"),
        ],
    )
    async def test_the_warmup_posture_alone_decides(
        self, clock, safe_log, health, gate_mode, warmup_mode, outcome
    ) -> None:
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD),
                _poll(WarmupState.UNAVAILABLE, source_state="SUSPENDED"),
            ],
        )
        if outcome == "blocked":
            with pytest.raises(ApplicationError) as caught:
                await _run(activities, gate_mode=gate_mode, warmup_mode=warmup_mode)
            assert caught.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        else:
            await _run(activities, gate_mode=gate_mode, warmup_mode=warmup_mode)
        (row,) = _rows(safe_log)
        assert row["outcome"] == outcome
        assert row["reason"] == "SOURCE_UNAVAILABLE"
        assert row[GATE_CLASSIFICATION_KEY] == "source_unverifiable"
        # Stamped with the posture that decided the row, not the gate's.
        assert row[GATE_MODE_KEY] == (warmup_mode or PreflightGateMode.SOFT).value
        assert row[GATE_TIER_KEY] == "warmup"
        assert row[WARMUP_OUTCOME_KEY] == "unavailable"
        assert "SUSPENDED" in row[FAILURE_MESSAGE_KEY]
        assert activities.tiers == [_PREFLIGHT_ONLY]

    async def test_an_unavailable_first_probe_runs_preflight_then_ends(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(clock, polls=[_poll(WarmupState.UNAVAILABLE)])
        await _run(activities, gate_mode=PreflightGateMode.HARD)
        assert activities.order == [_PROBE, _CHECK]
        assert activities.tiers == [_PREFLIGHT_ONLY]
        assert clock.slept == []
        (row,) = _rows(safe_log)
        assert row["outcome"] == "would_block"
        assert row[WARMUP_OUTCOME_KEY] == "unavailable"
        assert json.loads(row[WARMUP_TRANSITIONS_KEY]) == [
            {"state": "unavailable", "at_ms": 0.0}
        ]


class TestTheCeiling:
    @pytest.mark.parametrize(
        ("gate_mode", "warmup_mode", "raises"),
        [
            (PreflightGateMode.HARD, None, False),
            (PreflightGateMode.HARD, PreflightGateMode.SOFT, False),
            (PreflightGateMode.SOFT, PreflightGateMode.HARD, True),
        ],
    )
    async def test_the_ceiling_is_a_warmup_exhausted_row(
        self, clock, safe_log, health, gate_mode, warmup_mode, raises
    ) -> None:
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD),
                _poll(WarmupState.WARMING, source_state="RESUMING"),
            ],
        )
        if raises:
            with pytest.raises(ApplicationError) as caught:
                await _run(
                    activities,
                    gate_mode=gate_mode,
                    warmup_mode=warmup_mode,
                    warmup_ceiling_seconds=30,
                )
            assert caught.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        else:
            await _run(
                activities,
                gate_mode=gate_mode,
                warmup_mode=warmup_mode,
                warmup_ceiling_seconds=30,
            )
        (row,) = _rows(safe_log)
        assert row["outcome"] == "warmup_exhausted"
        assert row["reason"] == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert row[GATE_CLASSIFICATION_KEY] == "source_unverifiable"
        assert row[GATE_MODE_KEY] == (warmup_mode or PreflightGateMode.SOFT).value
        assert row[GATE_TIER_KEY] == "warmup"
        assert row[FAILURE_AUDIENCE_KEY] == "USER"
        assert row[WARMUP_OUTCOME_KEY] == "exhausted"
        assert row[WARMUP_DURATION_KEY] == 30_000.0
        assert row[FAILURE_MESSAGE_KEY] == (
            "Source wasn't ready within 30s; last reported: RESUMING"
        )
        assert clock.slept == [5.0, 10.0, 15.0]
        assert activities.tiers == [_PREFLIGHT_ONLY]
        health.assert_called_with("")

    async def test_the_exhausted_row_names_the_last_transient_raise(
        self, clock, safe_log, health
    ) -> None:
        raised = DependencyUnavailableError(message="resume API 503")
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD),
                _poll(WarmupState.WARMING, error=raised.to_failure_details()),
            ],
        )
        await _run(activities, warmup_ceiling_seconds=30)
        (row,) = _rows(safe_log)
        assert row["outcome"] == "warmup_exhausted"
        assert row[FAILURE_MESSAGE_KEY] == (
            "Source wasn't ready within 30s; last reported: warming (resume API 503)"
        )

    @pytest.mark.parametrize(
        ("poll_takes", "ran_warmup_tier"), [(2, True), (5, False), (10, False)]
    )
    async def test_a_ready_returned_at_or_past_the_ceiling_is_exhausted(
        self, clock, safe_log, health, poll_takes, ran_warmup_tier
    ) -> None:
        """The deadline is checked when the poll returns, not only before it.

        The poll starts at 25s against a 30s ceiling. READY at 27s runs the
        WARMUP checks; READY at 30s or 35s does not buy them extra time.
        """
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD, next_poll_seconds=25),
                _poll(WarmupState.READY),
            ],
            poll_takes=poll_takes,
        )
        await _run(activities, warmup_ceiling_seconds=30)
        if ran_warmup_tier:
            assert activities.tiers == [_PREFLIGHT_ONLY, _READY_WAIT]
            assert _rows(safe_log) == []
        else:
            assert activities.tiers == [_PREFLIGHT_ONLY]
            (row,) = _rows(safe_log)
            assert row["outcome"] == "warmup_exhausted"
            assert row[WARMUP_OUTCOME_KEY] == "exhausted"

    async def test_the_ceiling_runs_from_gate_start(
        self, clock, safe_log, health
    ) -> None:
        """PREFLIGHT checks that outlast the ceiling leave no time to poll."""
        activities = _Activities(clock, polls=[_poll(WarmupState.COLD)], first_takes=40)
        await _run(activities, warmup_ceiling_seconds=30)
        assert len(activities.polls) == 1
        assert clock.slept == []
        (row,) = _rows(safe_log)
        assert row["outcome"] == "warmup_exhausted"
        assert row[WARMUP_DURATION_KEY] == 40_000.0

    async def test_transitions_are_capped(self, clock, safe_log, health) -> None:
        """A source flapping for a whole ceiling does not grow the row per poll."""
        flapping = [
            _poll(
                WarmupState.WARMING if i % 2 == 0 else WarmupState.COLD,
                next_poll_seconds=5,
            )
            for i in range(200)
        ]
        activities = _Activities(
            clock,
            polls=[_poll(WarmupState.COLD, next_poll_seconds=5), *flapping],
        )
        await _run(activities, warmup_ceiling_seconds=600)
        assert len(activities.polls) == 121
        (row,) = _rows(safe_log)
        transitions = json.loads(row[WARMUP_TRANSITIONS_KEY])
        assert len(transitions) == WARMUP_TRANSITIONS_MAX
        # Oldest first: the cap keeps the start of the wait.
        assert transitions[:2] == [
            {"state": "cold", "at_ms": 0.0},
            {"state": "warming", "at_ms": 5_000.0},
        ]


class TestATerminalProbeRaise:
    @pytest.mark.parametrize(
        ("raised", "gate_mode", "warmup_mode", "outcome"),
        [
            (AuthError(message="expired"), PreflightGateMode.HARD, None, "blocked"),
            (
                AppPermissionDeniedError(message="no grant"),
                PreflightGateMode.HARD,
                None,
                "blocked",
            ),
            (
                NotFoundError(message="no such warehouse"),
                PreflightGateMode.HARD,
                None,
                "blocked",
            ),
            (
                AuthError(message="expired"),
                PreflightGateMode.SOFT,
                PreflightGateMode.HARD,
                "would_block",
            ),
        ],
    )
    async def test_category_gating_under_the_gate_mode(
        self, clock, safe_log, health, raised, gate_mode, warmup_mode, outcome
    ) -> None:
        activities = _Activities(
            clock,
            polls=[
                _poll(WarmupState.COLD),
                _poll(WarmupState.WARMING, error=raised.to_failure_details()),
                _poll(WarmupState.READY),
            ],
        )
        if outcome == "blocked":
            with pytest.raises(ApplicationError) as caught:
                await _run(activities, gate_mode=gate_mode, warmup_mode=warmup_mode)
            assert caught.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        else:
            await _run(activities, gate_mode=gate_mode, warmup_mode=warmup_mode)
        # The wait ends on the first terminal poll.
        assert len(activities.polls) == 2
        assert activities.tiers == [_PREFLIGHT_ONLY]
        (row,) = _rows(safe_log)
        assert row["outcome"] == outcome
        assert row["reason"] == raised.code
        assert row[GATE_MODE_KEY] == gate_mode.value
        assert row[GATE_CLASSIFICATION_KEY] == "source_unverifiable"
        assert row[GATE_TIER_KEY] == "warmup"
        assert row[WARMUP_OUTCOME_KEY] == "failed"


class TestThePollsOwnBreakageFailsOpen:
    async def test_a_failed_later_poll_is_gate_broken(
        self, clock, safe_log, health
    ) -> None:
        activities = _Activities(
            clock,
            polls=[_poll(WarmupState.COLD), RuntimeError("activity not registered")],
        )
        await _run(
            activities,
            gate_mode=PreflightGateMode.HARD,
            warmup_mode=PreflightGateMode.HARD,
        )
        assert activities.tiers == [_PREFLIGHT_ONLY]
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row["reason"] == "RuntimeError"
        assert row[GATE_CLASSIFICATION_KEY] == "gate_broken"
        assert row[GATE_TIER_KEY] == "warmup"
        assert row[WARMUP_OUTCOME_KEY] == "broken"
        assert json.loads(row[WARMUP_TRANSITIONS_KEY]) == [
            {"state": "cold", "at_ms": 0.0}
        ]
        health.assert_called_with("")


# ---------------------------------------------------------------------------
# The check activity
# ---------------------------------------------------------------------------


class _RecordingHandler(WarmingSourceHandler):
    """A scripted handler that keeps every input it was called with."""

    def __init__(self, source: WarmingSource, **kwargs: Any) -> None:
        super().__init__(source, **kwargs)
        self.warmup_inputs: list[WarmupInput] = []
        self.check_inputs: list[PreflightInput] = []

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        self.warmup_inputs.append(input)
        return await super().warmup(input)

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.check_inputs.append(input)
        return await super().preflight_check(input)


class _HangingWarmupHandler(_RecordingHandler):
    """A handler whose warmup probe never answers within any test bound."""

    async def warmup(self, input: WarmupInput) -> WarmupObservation:
        self.calls.append("probe")
        await asyncio.sleep(30)
        return WarmupObservation(state=WarmupState.READY)


def _handler(*script: Any, **kwargs: Any) -> WarmingSourceHandler:
    return WarmingSourceHandler(WarmingSource(list(script)), **kwargs)


class TestTheCheckActivity:
    @pytest.mark.parametrize(
        ("tiers", "warmup", "called", "row_tier", "warmup_outcome"),
        [
            (None, None, "check:preflight+warmup", None, None),
            (
                None,
                WarmupWait(outcome=WarmupOutcome.BROKEN),
                "check:preflight+warmup",
                None,
                "broken",
            ),
            (
                _PREFLIGHT_ONLY,
                WarmupWait(outcome=WarmupOutcome.WARMING),
                "check:preflight",
                "preflight",
                "warming",
            ),
            (
                _PREFLIGHT_ONLY,
                WarmupWait(outcome=WarmupOutcome.UNAVAILABLE),
                "check:preflight",
                "preflight",
                "unavailable",
            ),
            (
                _READY_WAIT,
                WarmupWait(outcome=WarmupOutcome.READY, duration_ms=1.0),
                "check:warmup",
                "warmup",
                "ready",
            ),
        ],
        ids=["every-tier", "broken-probe", "warming", "unavailable", "warmup"],
    )
    async def test_it_never_probes_and_its_row_follows_the_dispatch(
        self,
        capture_preflight_outcomes,
        tiers,
        warmup,
        called,
        row_tier,
        warmup_outcome,
    ) -> None:
        handler = _handler(WarmupState.COLD)
        gate = build_preflight_gate_activity(handler, "myapp")
        result = await gate(
            PreflightGateInput(entrypoint="crawl", tiers=tiers, warmup=warmup)
        )
        assert handler.calls == [called]
        assert handler.source.probes == 0
        assert result.warmup is None
        row = capture_preflight_outcomes.one
        assert row["outcome"] == "proceeded"
        assert row.get(GATE_TIER_KEY) == row_tier
        assert row.get(WARMUP_OUTCOME_KEY) == warmup_outcome
        if warmup is None:
            for key in (WARMUP_DURATION_KEY, WARMUP_TRANSITIONS_KEY):
                assert key not in row

    async def test_the_probe_does_not_spend_the_handlers_budget(
        self, capture_preflight_outcomes
    ) -> None:
        """A warmup that would hang is never awaited by the check activity, so
        the handler is told (nearly) the whole budget and the call is quick."""
        handler = _HangingWarmupHandler(WarmingSource([WarmupState.READY]))
        gate = build_preflight_gate_activity(handler, "myapp", budget_seconds=20)
        loop = asyncio.get_running_loop()
        started = loop.time()
        await gate(PreflightGateInput(entrypoint="crawl"))
        assert loop.time() - started < 5
        assert handler.warmup_inputs == []
        assert "probe" not in handler.calls
        (seen,) = handler.check_inputs
        assert seen.timeout_seconds >= 19

    async def test_the_warmup_dispatch_row_carries_the_wait(
        self, capture_preflight_outcomes
    ) -> None:
        handler = _handler(WarmupState.COLD)
        wait = WarmupWait(
            outcome=WarmupOutcome.READY,
            duration_ms=12_000.0,
            transitions=[
                WarmupTransition(state=WarmupState.COLD, at_ms=0.0),
                WarmupTransition(state=WarmupState.READY, at_ms=12_000.0),
            ],
        )
        gate = build_preflight_gate_activity(handler, "myapp")
        await gate(
            PreflightGateInput(entrypoint="crawl", tiers=_READY_WAIT, warmup=wait)
        )
        row = capture_preflight_outcomes.one
        assert row[WARMUP_DURATION_KEY] == 12_000.0
        assert json.loads(row[WARMUP_TRANSITIONS_KEY]) == [
            {"state": "cold", "at_ms": 0.0},
            {"state": "ready", "at_ms": 12_000.0},
        ]
        (matrix_row,) = capture_preflight_outcomes.matrix
        assert matrix_row["tier"] == "warmup"

    async def test_a_failing_warmup_check_is_a_verdict(
        self, capture_preflight_outcomes
    ) -> None:
        handler = _handler(WarmupState.READY, warmup_check_passes=False)
        gate = build_preflight_gate_activity(
            handler, "myapp", mode=PreflightGateMode.HARD
        )
        with pytest.raises(ApplicationError) as caught:
            await gate(
                PreflightGateInput(
                    tiers=_READY_WAIT,
                    warmup=WarmupWait(outcome=WarmupOutcome.READY, duration_ms=1.0),
                )
            )
        assert caught.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        row = capture_preflight_outcomes.one
        assert row["outcome"] == "blocked"
        assert row[GATE_TIER_KEY] == "warmup"


class TestThePostCallTierCheck:
    @pytest.mark.parametrize(
        ("tiers", "outside"),
        [(_PREFLIGHT_ONLY, "catalogScan"), (_READY_WAIT, "reachable")],
        ids=["preflight-dispatch", "warmup-dispatch"],
    )
    async def test_a_row_outside_the_tiers_is_no_verdict(
        self, capture_preflight_outcomes, tiers, outside
    ) -> None:
        handler = _handler(WarmupState.READY, ignores_tiers=True)
        gate = build_preflight_gate_activity(
            handler, "myapp", mode=PreflightGateMode.HARD
        )
        with pytest.raises(ApplicationError) as caught:
            await gate(PreflightGateInput(tiers=tiers))
        assert caught.value.type == "InternalError"
        assert outside in str(caught.value)
        # Never a silent drop, and never a verdict row.
        assert capture_preflight_outcomes.rows == []

    async def test_every_tier_requested_means_nothing_is_outside(
        self, capture_preflight_outcomes
    ) -> None:
        handler = _handler(WarmupState.READY, ignores_tiers=True)
        gate = build_preflight_gate_activity(handler, "myapp")
        result = await gate(PreflightGateInput())
        assert len(result.checks) == 2

    async def test_the_workflow_fails_it_open(
        self, clock, safe_log, health, capture_preflight_outcomes
    ) -> None:
        gate = build_preflight_gate_activity(
            _handler(WarmupState.COLD, ignores_tiers=True), "myapp"
        )
        with pytest.raises(ApplicationError) as caught:
            await gate(PreflightGateInput(tiers=_PREFLIGHT_ONLY))
        activities = _Activities(
            clock, polls=[_poll(WarmupState.COLD)], first=caught.value
        )
        await _run(activities, gate_mode=PreflightGateMode.HARD)
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[GATE_CLASSIFICATION_KEY] == "gate_broken"
        assert len(activities.polls) == 1


class TestStorageVerification:
    @pytest.mark.parametrize(
        ("tiers", "verified"),
        [(None, True), (_PREFLIGHT_ONLY, True), (_READY_WAIT, False)],
        ids=["every-tier", "preflight-dispatch", "warmup-dispatch"],
    )
    async def test_every_dispatch_but_the_warmup_one_verifies_storage(
        self, capture_preflight_outcomes, tiers, verified
    ) -> None:
        storage = mock.AsyncMock(return_value=False)
        gate = build_preflight_gate_activity(
            _handler(WarmupState.READY), "myapp", verify_storage=True
        )
        with mock.patch.object(preflight_gate, "_append_storage_checks", storage):
            await gate(PreflightGateInput(tiers=tiers))
        assert storage.await_count == (1 if verified else 0)


# ---------------------------------------------------------------------------
# The poll activity
# ---------------------------------------------------------------------------


def _poll_activity(handler: WarmingSourceHandler, probe_timeout_seconds: int = 10):
    return build_preflight_warmup_activity(
        handler, "myapp", probe_timeout_seconds=probe_timeout_seconds
    )


class TestThePollActivity:
    def test_its_name_is_reserved_under_the_app(self) -> None:
        poll = _poll_activity(_handler(WarmupState.READY))
        assert (
            getattr(poll, "__temporal_activity_definition").name
            == preflight_warmup_activity_name("myapp")
            == "myapp:preflight_warmup"
        )

    async def test_an_observation_passes_through(self) -> None:
        seen = WarmupObservation(
            state=WarmupState.QUEUED,
            source_state="QUEUED",
            queued_queries=4,
            next_poll_seconds=20,
        )
        result = await _poll_activity(_handler(seen))(PreflightGateInput())
        assert result == WarmupPoll(observation=seen)
        assert result.is_terminal is False

    @pytest.mark.parametrize(
        "raised",
        [
            AuthError(message="expired"),
            AppPermissionDeniedError(message="no grant"),
            NotFoundError(message="no such warehouse"),
        ],
    )
    async def test_a_terminal_raise_is_carried_as_the_error(self, raised) -> None:
        result = await _poll_activity(_handler(raised))(PreflightGateInput())
        assert result.is_terminal is True
        assert result.error is not None
        assert result.error.code == raised.code

    @pytest.mark.parametrize(
        "raised",
        [
            DependencyUnavailableError(message="resume API 503"),
            SourceUnavailableError(message="refused"),
            RuntimeError("connection reset"),
        ],
    )
    async def test_any_other_raise_is_warming_with_the_error(self, raised) -> None:
        result = await _poll_activity(_handler(raised))(PreflightGateInput())
        assert result.observation == WarmupObservation(state=WarmupState.WARMING)
        assert result.error is not None
        assert result.is_terminal is False

    async def test_a_probe_that_overruns_is_warming(self) -> None:
        handler = _HangingWarmupHandler(WarmingSource([WarmupState.READY]))
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await _poll_activity(handler, probe_timeout_seconds=1)(
            PreflightGateInput()
        )
        assert loop.time() - started < 5
        assert result == WarmupPoll(
            observation=WarmupObservation(state=WarmupState.WARMING)
        )

    async def test_the_probe_gets_the_checks_input_and_the_probe_timeout(
        self,
    ) -> None:
        handler = _RecordingHandler(WarmingSource([WarmupState.READY]))
        await _poll_activity(handler, probe_timeout_seconds=7)(
            PreflightGateInput(
                entrypoint="crawl", extraction_snapshot={"warehouse_name": "wh1"}
            )
        )
        (seen,) = handler.warmup_inputs
        assert seen.probe_timeout_seconds == 7
        assert seen.entrypoint == "crawl"
        assert seen.connection_config.model_dump()["warehouse_name"] == "wh1"

    async def test_a_credential_failure_leaves_as_plumbing(self) -> None:
        handler = _handler(WarmupState.READY)
        with mock.patch.object(
            preflight_gate,
            "_resolve_gate_credentials",
            side_effect=DependencyUnavailableError(message="vault down"),
        ):
            with pytest.raises(ApplicationError) as caught:
                await _poll_activity(handler)(PreflightGateInput())
        assert caught.value.type == "DependencyUnavailableError"
        assert handler.probes == 0

    @pytest.mark.parametrize(
        ("error", "terminal"),
        [
            (None, False),
            (AuthError(message="expired"), True),
            (DependencyUnavailableError(message="503"), False),
        ],
    )
    def test_is_terminal_is_keyed_on_the_category(self, error, terminal) -> None:
        poll = WarmupPoll(
            observation=WarmupObservation(state=WarmupState.WARMING),
            error=None if error is None else error.to_failure_details(),
        )
        assert poll.is_terminal is terminal


# ---------------------------------------------------------------------------
# Attribution and the health line
# ---------------------------------------------------------------------------


class TestWarmupDetails:
    def test_unavailable_is_a_source_unavailable_naming_the_source(self) -> None:
        details = warmup_unavailable_details(
            WarmupObservation(state=WarmupState.UNAVAILABLE, source_state="SUSPENDED"),
            "myapp",
        )
        assert details.code == "SOURCE_UNAVAILABLE"
        assert details.category is FailureCategory.SOURCE_UNAVAILABLE
        assert details.app_name == "myapp"
        assert "SUSPENDED" in details.message

    def test_the_ceiling_is_warmup_exhausted_naming_it(self) -> None:
        details = warmup_exhausted_details(
            WarmupObservation(state=WarmupState.WARMING, source_state="RESUMING"),
            "myapp",
            1800,
        )
        assert details.code == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert details.category is FailureCategory.SOURCE_UNAVAILABLE
        assert details.audience is Audience.USER
        assert details.message == (
            "Source wasn't ready within 30 min; last reported: RESUMING"
        )
        assert details.retryable is False
        assert (
            details.suggested_action
            == WARMUP_SUGGESTED_ACTIONS["SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"]
        )

    def test_the_ceiling_names_the_queue_depth(self) -> None:
        details = warmup_exhausted_details(
            WarmupObservation(state=WarmupState.QUEUED, queued_queries=3), "myapp", 60
        )
        assert details.message == (
            "Source wasn't ready within 1 min; last reported: queued, 3 queued"
        )


class TestTheHealthLine:
    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(0, "0s"), (40, "40s"), (60, "1 min"), (125, "2 min 5s"), (1800, "30 min")],
    )
    def test_durations_read_like_a_status_line(self, seconds, text) -> None:
        assert human_duration(seconds) == text

    def test_waiting_names_the_sources_own_label(self) -> None:
        seen = WarmupObservation(state=WarmupState.WARMING, source_state="RESUMING")
        assert warmup_waiting_details(seen, 40.4) == (
            "waiting for source warmup: RESUMING, 40s"
        )

    def test_waiting_falls_back_to_the_state(self) -> None:
        seen = WarmupObservation(state=WarmupState.COLD)
        assert warmup_waiting_details(seen, 5) == "waiting for source warmup: cold, 5s"


# ---------------------------------------------------------------------------
# Warmup fields on the gate rows (FND-3041)
# ---------------------------------------------------------------------------


def _row(**kwargs: Any) -> dict[str, Any]:
    return gate_outcome_row(
        app_name="myapp",
        entrypoint="crawl",
        outcome=preflight_gate.PreflightRowOutcome.PROCEEDED,
        reason="ready",
        checks=kwargs.pop("checks", []),
        mode=PreflightGateMode.SOFT,
        classification=preflight_gate.PreflightClassification.VERDICT,
        duration_ms=1.0,
        budget_seconds=150,
        attempt=1,
        **kwargs,
    )


class TestTheWarmupRowFields:
    def test_the_row_carries_no_tier_or_warmup_key_unless_given_one(self) -> None:
        row = _row()
        assert GATE_TIER_KEY not in row
        assert WARMUP_OUTCOME_KEY not in row

    def test_a_warming_row_has_no_duration_or_transitions(self) -> None:
        row = _row(
            tier=CheckTier.PREFLIGHT,
            warmup=WarmupWait(outcome=WarmupOutcome.WARMING),
        )
        assert row[GATE_TIER_KEY] == "preflight"
        assert row[WARMUP_OUTCOME_KEY] == "warming"
        assert WARMUP_DURATION_KEY not in row
        assert WARMUP_TRANSITIONS_KEY not in row

    def test_transitions_are_capped(self) -> None:
        row = _row(
            warmup=WarmupWait(
                outcome=WarmupOutcome.EXHAUSTED,
                duration_ms=1.0,
                transitions=[
                    WarmupTransition(state=WarmupState.WARMING, at_ms=float(i))
                    for i in range(WARMUP_TRANSITIONS_MAX + 10)
                ],
            )
        )
        assert len(json.loads(row[WARMUP_TRANSITIONS_KEY])) == WARMUP_TRANSITIONS_MAX

    def test_a_preflight_tier_check_has_no_tier_in_the_matrix(self) -> None:
        row = _row(checks=[PreflightCheck(name="reachable", passed=True)])
        (matrix_row,) = json.loads(row[CHECK_MATRIX_KEY])
        assert "tier" not in matrix_row

    def test_every_warmup_key_reaches_otlp(self) -> None:
        """Not allowlisted means dropped before export, so not queryable at all."""
        assert {
            GATE_TIER_KEY,
            WARMUP_OUTCOME_KEY,
            WARMUP_DURATION_KEY,
            WARMUP_TRANSITIONS_KEY,
        } <= _KNOWN_EXTRA_KEYS


# ---------------------------------------------------------------------------
# Declared-value clamps and the poll cadence
# ---------------------------------------------------------------------------


class TestWarmupSettings:
    def test_the_app_declares_the_documented_defaults(self) -> None:
        assert App.preflight_warmup_ceiling_seconds == WARMUP_CEILING_DEFAULT_SECONDS
        assert (
            App.preflight_warmup_probe_timeout_seconds
            == WARMUP_PROBE_TIMEOUT_DEFAULT_SECONDS
        )
        assert App.preflight_warmup_mode is PreflightGateMode.SOFT
        assert not hasattr(App, "preflight_warmup_poll_seconds")

    @pytest.mark.parametrize(
        ("raw", "value", "complains"),
        [
            (None, WARMUP_CEILING_DEFAULT_SECONDS, False),
            (1, WARMUP_CEILING_MIN_SECONDS, True),
            ("soon", WARMUP_CEILING_DEFAULT_SECONDS, True),
            (True, WARMUP_CEILING_DEFAULT_SECONDS, True),
            (86_400, 86_400, False),
        ],
    )
    def test_the_ceiling_is_floored_never_capped(self, raw, value, complains) -> None:
        got, complaint = warmup_ceiling_seconds(raw)
        assert got == value
        assert bool(complaint) is complains

    @pytest.mark.parametrize(
        ("raw", "ceiling", "value", "complains"),
        [
            (None, 600, WARMUP_PROBE_TIMEOUT_DEFAULT_SECONDS, False),
            (0, 600, 1, True),
            (120, 60, 60, True),
            (None, 5, 5, False),
        ],
    )
    def test_the_probe_timeout_is_floored_and_never_past_the_ceiling(
        self, raw, ceiling, value, complains
    ) -> None:
        got, complaint = warmup_probe_timeout_seconds(raw, ceiling)
        assert got == value
        assert bool(complaint) is complains

    @pytest.mark.parametrize(
        ("unhinted", "hint", "remaining", "delay"),
        [
            (0, None, 600, 5.0),
            (1, None, 600, 10.0),
            (2, None, 600, 20.0),
            (3, None, 600, 30.0),
            (50, None, 600, 30.0),
            (0, 0, 600, 5.0),
            (0, 3, 600, 5.0),
            (5, 45, 600, 45.0),
            (0, 100, 30, 30.0),
            (3, None, 12.5, 12.5),
            (0, None, -1, 0.0),
        ],
    )
    def test_the_poll_delay(self, unhinted, hint, remaining, delay) -> None:
        assert warmup_poll_delay(unhinted, hint, remaining) == delay


# ---------------------------------------------------------------------------
# Worker registration
# ---------------------------------------------------------------------------


@dataclass
class _WarmupWorkerInput(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _WarmupWorkerOutput(Output, allow_unbounded_fields=True):
    y: str = ""


def _mock_client() -> mock.MagicMock:
    client = mock.MagicMock()
    client.namespace = "default"
    client.service_client.config.target_host = "localhost:7233"
    return client


def _registered_names() -> list[str]:
    captured: dict[str, list[Any]] = {}

    def _capture(*args: Any, **kwargs: Any) -> mock.MagicMock:
        captured["activities"] = list(kwargs.get("activities", []))
        return mock.MagicMock()

    with mock.patch(
        "application_sdk.execution._temporal.worker.Worker", side_effect=_capture
    ):
        create_worker(_mock_client(), enable_sdr=False)
    return [
        getattr(a, "__temporal_activity_definition").name
        for a in captured["activities"]
        if hasattr(a, "__temporal_activity_definition")
    ]


def _app_name() -> str:
    return AppRegistry.get_instance().list_all()[0].name


class TestWorkerRegistration:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_an_app_that_declares_nothing_registers_the_poll(self) -> None:
        class _PlainApp(App):
            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        names = _registered_names()
        assert names.count(f"{_app_name()}:preflight") == 1
        assert names.count(f"{_app_name()}:preflight_warmup") == 1

    def test_an_app_that_declares_a_ceiling_registers_the_same_poll(self) -> None:
        class _WarmApp(App):
            preflight_warmup_ceiling_seconds = 120

            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        names = _registered_names()
        assert names.count(f"{_app_name()}:preflight_warmup") == 1
        assert not [n for n in names if n.endswith(("_start", "_state"))]

    def test_a_task_named_like_the_poll_is_rejected_for_any_app(self) -> None:
        class _CollidingApp(App):
            @task(timeout_seconds=60)
            async def preflight_warmup(
                self, input: _WarmupWorkerInput
            ) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        with pytest.raises(WorkerActivityNameCollisionError) as caught:
            create_worker(_mock_client(), enable_sdr=False)
        assert "preflight_warmup" in str(caught.value)

    def test_clamped_declarations_are_logged(self) -> None:
        class _MisdeclaredApp(App):
            preflight_warmup_ceiling_seconds = 1
            preflight_warmup_probe_timeout_seconds = 0

            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        with mock.patch("application_sdk.execution._temporal.worker.logger") as log:
            _registered_names()
        complaints = {
            c.args[0]: c.args[2]
            for c in log.warning.call_args_list
            if c.args and str(c.args[0]).startswith("preflight_warmup_")
        }
        assert complaints == {
            "preflight_warmup_ceiling_seconds: %s; using %ds": (
                WARMUP_CEILING_MIN_SECONDS
            ),
            "preflight_warmup_probe_timeout_seconds: %s; using %ds": 1,
        }

    def test_valid_declarations_are_not_logged(self) -> None:
        class _WellDeclaredApp(App):
            preflight_warmup_ceiling_seconds = 900
            preflight_warmup_probe_timeout_seconds = 20

            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        with mock.patch("application_sdk.execution._temporal.worker.logger") as log:
            _registered_names()
        assert not [
            c
            for c in log.warning.call_args_list
            if c.args and str(c.args[0]).startswith("preflight_warmup_")
        ]
