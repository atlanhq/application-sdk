"""Unit tests for the preflight gate's warmup phase (FND-3039).

The end-to-end behaviour — every terminal state and the ceiling, on a real
server with real durable timers — is pinned by
``tests/integration/test_preflight_warmup.py``. What lives here is what that
suite cannot reach cheaply: the replay guard, the fail-open paths for the
gate's own plumbing, the warmup activities' reading of a hook's raise, the
tier filter and row key on the check activity, the declared-value clamps, and
the worker's registration and collision guard.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest import mock

import pytest

from application_sdk.app.base import App, _run_preflight_gate
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.errors.base import AppError
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    NotFoundError,
    SourceUnavailableError,
)
from application_sdk.execution._temporal import preflight_gate
from application_sdk.execution._temporal._activity_errors import (
    WorkerActivityNameCollisionError,
)
from application_sdk.execution._temporal.preflight_gate import (
    PREFLIGHT_FAILED_ERROR_TYPE,
    WARMUP_CEILING_DEFAULT_SECONDS,
    WARMUP_CEILING_MIN_SECONDS,
    WARMUP_POLL_DEFAULT_SECONDS,
    WARMUP_SUGGESTED_ACTIONS,
    WARMUP_TRANSITIONS_MAX,
    PreflightGateInput,
    WarmupObservation,
    WarmupOutcome,
    WarmupTransition,
    build_preflight_gate_activity,
    build_preflight_warmup_activities,
    filter_checks_to_tier,
    gate_outcome_row,
    gate_warmup_ceiling_seconds,
    gate_warmup_poll_seconds,
    human_duration,
    preflight_warmup_start_activity_name,
    preflight_warmup_state_activity_name,
    warmup_unavailable_details,
    warmup_waiting_details,
)
from application_sdk.execution._temporal.worker import create_worker
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    CheckTier,
    PreflightCheck,
    PreflightGateMode,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
    WarmupState,
    WarmupStatus,
)
from application_sdk.observability.logger_adaptor import (
    _KNOWN_EXTRA_KEYS,
    CHECK_MATRIX_KEY,
    GATE_CLASSIFICATION_KEY,
    GATE_TIER_KEY,
    WARMUP_DURATION_KEY,
    WARMUP_OUTCOME_KEY,
    WARMUP_TRANSITIONS_KEY,
)

# ---------------------------------------------------------------------------
# Workflow frame
# ---------------------------------------------------------------------------


class _ResolvableInput:
    """Minimal object satisfying the CredentialResolvable protocol."""

    extraction_method = ""
    credential_guid = ""
    agent_json = None


class _Handle:
    """Stand-in for the ``ActivityHandle`` ``workflow.start_activity`` returns."""

    def __init__(self, result: Any = None, exc: BaseException | None = None) -> None:
        self._result = result
        self._exc = exc
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True

    def __await__(self):
        async def _resolve() -> Any:
            if self._exc is not None:
                raise self._exc
            return self._result

        return _resolve().__await__()


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


def _rows(safe_log) -> list[dict]:
    return [c.kwargs for c in safe_log.call_args_list if "outcome" in c.kwargs]


@pytest.fixture
def clock():
    c = _Clock()
    with (
        mock.patch("application_sdk.app.base.workflow.now", side_effect=c.read),
        mock.patch("application_sdk.app.base.workflow.sleep", side_effect=c.sleep),
    ):
        yield c


@pytest.fixture
def safe_log():
    with mock.patch("application_sdk.app.base._safe_log") as m:
        yield m


def _patched(*ids_true: str):
    return mock.patch(
        "application_sdk.app.base.workflow.patched",
        side_effect=lambda patch_id: patch_id in ids_true,
    )


def _gate(
    *,
    execute: Any = None,
    start: _Handle | None = None,
    patched: tuple[str, ...] = ("preflight-gate", "preflight-gate-warmup"),
):
    execute_mock = mock.AsyncMock(side_effect=execute)
    start_mock = mock.Mock(return_value=start or _Handle(WarmupState()))
    patches = (
        _patched(*patched),
        mock.patch("application_sdk.app.base.workflow.execute_activity", execute_mock),
        mock.patch("application_sdk.app.base.workflow.start_activity", start_mock),
    )
    return execute_mock, start_mock, patches


def _tiers(execute_mock: mock.AsyncMock) -> list[CheckTier | None]:
    return [
        call.args[1].tier
        for call in execute_mock.call_args_list
        if call.args[0] == "myapp:preflight"
    ]


class TestTheWorkflowGuards:
    async def test_no_ceiling_means_no_warmup_and_no_patch_marker(
        self, clock, safe_log
    ) -> None:
        execute_mock, start_mock, patches = _gate()
        with patches[0] as patched, patches[1], patches[2]:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        start_mock.assert_not_called()
        assert _tiers(execute_mock) == [None]
        assert [c.args[0] for c in patched.call_args_list] == ["preflight-gate"]
        assert clock.slept == []

    async def test_a_run_from_before_the_phase_replays_without_it(
        self, clock, safe_log
    ) -> None:
        execute_mock, start_mock, patches = _gate(patched=("preflight-gate",))
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", warmup_ceiling_seconds=60
            )
        start_mock.assert_not_called()
        assert _tiers(execute_mock) == [None]
        assert clock.slept == []

    async def test_warmup_start_fires_with_the_fast_dispatch(
        self, clock, safe_log
    ) -> None:
        execute_mock, start_mock, patches = _gate(
            start=_Handle(WarmupState(status=WarmupStatus.READY))
        )
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", warmup_ceiling_seconds=60
            )
        assert start_mock.call_args.args[0] == "myapp:preflight_warmup_start"
        assert _tiers(execute_mock) == [CheckTier.FAST, CheckTier.WARMUP]


class TestTheGatesOwnBreakageFailsOpen:
    async def test_a_failed_warmup_start_activity_fails_open(
        self, clock, safe_log
    ) -> None:
        execute_mock, _, patches = _gate(
            start=_Handle(exc=RuntimeError("no worker polled the queue"))
        )
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(),
                "myapp",
                "crawl",
                gate_mode=PreflightGateMode.HARD,
                warmup_ceiling_seconds=60,
            )
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[GATE_CLASSIFICATION_KEY] == "gate_broken"
        assert row[GATE_TIER_KEY] == "warmup"
        assert _tiers(execute_mock) == [CheckTier.FAST]

    async def test_a_failed_warmup_state_activity_fails_open(
        self, clock, safe_log
    ) -> None:
        def _execute(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "myapp:preflight_warmup_state":
                raise RuntimeError("activity not registered")
            return None

        execute_mock, _, patches = _gate(
            execute=_execute, start=_Handle(WarmupState(status=WarmupStatus.RUNNING))
        )
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(),
                "myapp",
                "crawl",
                gate_mode=PreflightGateMode.HARD,
                warmup_ceiling_seconds=60,
                warmup_poll_seconds=5,
            )
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[GATE_CLASSIFICATION_KEY] == "gate_broken"
        assert clock.slept == [5.0]
        assert _tiers(execute_mock) == [CheckTier.FAST]

    async def test_a_fast_block_cancels_the_pending_warmup(
        self, clock, safe_log
    ) -> None:
        block = ApplicationError(
            "Preflight failed: bad creds",
            type=PREFLIGHT_FAILED_ERROR_TYPE,
            non_retryable=True,
        )
        handle = _Handle(WarmupState(status=WarmupStatus.RUNNING))
        _, _, patches = _gate(execute=block, start=handle)
        with patches[0], patches[1], patches[2], pytest.raises(ApplicationError):
            await _run_preflight_gate(
                _ResolvableInput(),
                "myapp",
                "crawl",
                gate_mode=PreflightGateMode.HARD,
                warmup_ceiling_seconds=60,
            )
        assert handle.cancelled is True


class TestTheCeilingRow:
    @pytest.mark.parametrize(
        ("mode", "outcome"),
        [(PreflightGateMode.HARD, "blocked"), (PreflightGateMode.SOFT, "would_block")],
    )
    async def test_the_ceiling_is_a_source_attributed_row(
        self, clock, safe_log, mode, outcome
    ) -> None:
        execute_mock, _, patches = _gate(
            execute=lambda name, *a, **k: (
                WarmupState(status=WarmupStatus.RUNNING)
                if name.endswith("warmup_state")
                else None
            ),
            start=_Handle(WarmupState(status=WarmupStatus.RUNNING)),
        )
        with patches[0], patches[1], patches[2]:
            if mode.enforces:
                with pytest.raises(ApplicationError) as caught:
                    await _run_preflight_gate(
                        _ResolvableInput(),
                        "myapp",
                        "crawl",
                        gate_mode=mode,
                        warmup_ceiling_seconds=30,
                        warmup_poll_seconds=10,
                    )
                assert caught.value.type == PREFLIGHT_FAILED_ERROR_TYPE
            else:
                await _run_preflight_gate(
                    _ResolvableInput(),
                    "myapp",
                    "crawl",
                    gate_mode=mode,
                    warmup_ceiling_seconds=30,
                    warmup_poll_seconds=10,
                )
        (row,) = _rows(safe_log)
        assert row["outcome"] == outcome
        assert row["reason"] == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert row[GATE_CLASSIFICATION_KEY] == "source_unverifiable"
        assert row[GATE_TIER_KEY] == "warmup"
        assert row["failure.audience"] == "USER"
        assert row[WARMUP_OUTCOME_KEY] == "exhausted"
        assert row[WARMUP_DURATION_KEY] == 30_000.0
        # Three polls on the timer, the last one landing on the ceiling.
        assert clock.slept == [10.0, 10.0, 10.0]
        assert _tiers(execute_mock) == [CheckTier.FAST]

    @pytest.mark.parametrize(("poll_takes", "tiers"), [(2, 2), (10, 1)])
    async def test_a_ready_returned_past_the_ceiling_is_exhausted(
        self, clock, safe_log, poll_takes, tiers
    ) -> None:
        """The deadline is checked when the last poll returns, not only before it.

        The poll starts at 25s against a 30s ceiling. Returning READY at 27s
        runs the WARMUP checks; returning it at 35s is a ceiling breach.
        """

        def _execute(name: str, *args: Any, **kwargs: Any) -> Any:
            if name.endswith("warmup_state"):
                clock.now += timedelta(seconds=poll_takes)
                return WarmupState(status=WarmupStatus.READY)
            return None

        execute_mock, _, patches = _gate(
            execute=_execute, start=_Handle(WarmupState(status=WarmupStatus.RUNNING))
        )
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(),
                "myapp",
                "crawl",
                gate_mode=PreflightGateMode.SOFT,
                warmup_ceiling_seconds=30,
                warmup_poll_seconds=25,
            )
        assert len(_tiers(execute_mock)) == tiers
        if tiers == 1:
            (row,) = _rows(safe_log)
            assert row["outcome"] == "would_block"
            assert row[WARMUP_OUTCOME_KEY] == "exhausted"


# ---------------------------------------------------------------------------
# Warmup activities
# ---------------------------------------------------------------------------


class _Hooks(DefaultHandler):
    def __init__(self, start: Any = None, state: Any = None) -> None:
        self._start = start
        self._state = state
        self.inputs: list[PreflightInput] = []

    async def warmup_start(self, input: PreflightInput) -> WarmupState:
        self.inputs.append(input)
        return await self._play(self._start)

    async def warmup_state(self, input: PreflightInput) -> WarmupState:
        self.inputs.append(input)
        return await self._play(self._state)

    @staticmethod
    async def _play(step: Any) -> WarmupState:
        if isinstance(step, BaseException):
            raise step
        if step == "hang":
            await asyncio.sleep(30)
        return step


def _activities(handler: DefaultHandler):
    start, state = build_preflight_warmup_activities(handler, "myapp")
    return start, state


class TestWarmupActivities:
    def test_names_are_reserved_under_the_app(self) -> None:
        start, state = _activities(DefaultHandler())
        assert (
            getattr(start, "__temporal_activity_definition").name
            == preflight_warmup_start_activity_name("myapp")
            == "myapp:preflight_warmup_start"
        )
        assert (
            getattr(state, "__temporal_activity_definition").name
            == preflight_warmup_state_activity_name("myapp")
            == "myapp:preflight_warmup_state"
        )

    async def test_hooks_get_the_checks_form_config(self) -> None:
        hooks = _Hooks(start=WarmupState(status=WarmupStatus.RUNNING))
        start, _ = _activities(hooks)
        state = await start(
            PreflightGateInput(
                entrypoint="crawl", extraction_snapshot={"warehouse_name": "wh1"}
            )
        )
        assert state.status is WarmupStatus.RUNNING
        (seen,) = hooks.inputs
        assert seen.entrypoint == "crawl"
        assert seen.connection_config.model_dump()["warehouse_name"] == "wh1"

    @pytest.mark.parametrize(
        "raised",
        [AuthError(message="expired"), NotFoundError(message="no such warehouse")],
    )
    async def test_a_terminal_raise_is_failed_with_the_leaf(self, raised) -> None:
        _, state_activity = _activities(_Hooks(state=raised))
        state = await state_activity(PreflightGateInput())
        assert state.status is WarmupStatus.FAILED
        assert state.error is not None
        assert state.error.code == raised.code

    @pytest.mark.parametrize(
        "raised",
        [DependencyUnavailableError(message="resume API 503"), RuntimeError("reset")],
    )
    async def test_any_other_raise_is_polled_again(self, raised) -> None:
        _, state_activity = _activities(_Hooks(state=raised))
        state = await state_activity(PreflightGateInput())
        assert state.status is WarmupStatus.RUNNING
        assert state.error is not None

    async def test_a_hook_that_overruns_is_polled_again(self) -> None:
        _, state_activity = _activities(_Hooks(state="hang"))
        with mock.patch.object(preflight_gate, "WARMUP_CALL_BUDGET_SECONDS", 1):
            state = await state_activity(PreflightGateInput())
        assert state.status is WarmupStatus.RUNNING
        assert state.error is not None
        assert state.error.code == "TIMEOUT"

    async def test_resolution_and_the_hook_share_one_budget(self) -> None:
        """Slow resolution leaves the hook only what remains of the budget.

        Two separate budgets could add up past the activity's start_to_close
        (budget + headroom), and Temporal would kill it before it reported.
        """

        async def _slow_resolution(
            _input: object,
        ) -> tuple[list[object], dict[str, list[object]]]:
            await asyncio.sleep(1.5)
            return [], {}

        hooks = _Hooks(state="hang")
        _, state_activity = _activities(hooks)
        loop = asyncio.get_running_loop()
        started = loop.time()
        with (
            mock.patch.object(preflight_gate, "WARMUP_CALL_BUDGET_SECONDS", 2),
            mock.patch.object(
                preflight_gate, "_resolve_gate_credentials", _slow_resolution
            ),
        ):
            state = await state_activity(PreflightGateInput())
        assert loop.time() - started < 2.5
        assert state.status is WarmupStatus.RUNNING
        assert state.error is not None
        assert state.error.code == "TIMEOUT"
        (seen,) = hooks.inputs
        assert seen.timeout_seconds == 0

    async def test_a_credential_failure_leaves_as_plumbing(self) -> None:
        start, _ = _activities(_Hooks(start=WarmupState()))
        with mock.patch.object(
            preflight_gate,
            "_resolve_gate_credentials",
            side_effect=DependencyUnavailableError(message="vault down"),
        ):
            with pytest.raises(ApplicationError) as caught:
                await start(PreflightGateInput())
        assert caught.value.type == "DependencyUnavailableError"


class TestWarmupUnavailableDetails:
    def test_a_failed_state_keeps_its_own_error(self) -> None:
        state = WarmupState(
            status=WarmupStatus.FAILED, error=AuthError(message="expired")
        )
        details = warmup_unavailable_details(state, "myapp")
        assert details.code == "AUTH"
        assert details.app_name == "myapp"

    def test_an_untyped_failed_state_is_source_unavailable(self) -> None:
        state = WarmupState(status=WarmupStatus.FAILED, message="decommissioned")
        details = warmup_unavailable_details(state, "myapp")
        assert details.code == "SOURCE_UNAVAILABLE"
        assert details.message == "decommissioned"

    def test_the_ceiling_is_warmup_exhausted_naming_it(self) -> None:
        state = WarmupState(status=WarmupStatus.RUNNING, message="RESUMING")
        details = warmup_unavailable_details(state, "myapp", ceiling_seconds=1800)
        assert details.code == "SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"
        assert details.category is FailureCategory.SOURCE_UNAVAILABLE
        assert details.audience is Audience.USER
        assert details.message == (
            "Source wasn't ready within 30 min; last reported: RESUMING"
        )
        assert details.retryable is False

    def test_unreachable_and_exhausted_get_different_remedies(self) -> None:
        """Keyed on code: network for one, sizing and queueing for the other."""
        unreachable = warmup_unavailable_details(
            WarmupState(status=WarmupStatus.FAILED, message="no route"), "myapp"
        )
        exhausted = warmup_unavailable_details(
            WarmupState(status=WarmupStatus.RUNNING), "myapp", ceiling_seconds=60
        )
        assert (
            unreachable.suggested_action
            == WARMUP_SUGGESTED_ACTIONS["SOURCE_UNAVAILABLE"]
        )
        assert "private link" in (unreachable.suggested_action or "")
        assert (
            exhausted.suggested_action
            == WARMUP_SUGGESTED_ACTIONS["SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED"]
        )
        assert "queue" in (exhausted.suggested_action or "")

    def test_a_typed_source_unavailable_without_a_remedy_gets_the_table_one(
        self,
    ) -> None:
        state = WarmupState(
            status=WarmupStatus.FAILED,
            error=SourceUnavailableError(message="refused"),
        )
        details = warmup_unavailable_details(state, "myapp")
        assert (
            details.suggested_action == WARMUP_SUGGESTED_ACTIONS["SOURCE_UNAVAILABLE"]
        )

    def test_a_hooks_own_remedy_wins(self) -> None:
        state = WarmupState(
            status=WarmupStatus.FAILED,
            error=SourceUnavailableError(
                message="refused", suggested_action="Open port 443."
            ),
        )
        details = warmup_unavailable_details(state, "myapp")
        assert details.suggested_action == "Open port 443."

    def test_a_code_outside_the_table_is_left_alone(self) -> None:
        state = WarmupState(
            status=WarmupStatus.FAILED, error=AuthError(message="expired")
        )
        assert warmup_unavailable_details(state, "myapp").suggested_action is None


class TestTheHealthLine:
    @pytest.mark.parametrize(
        ("seconds", "text"),
        [(0, "0s"), (40, "40s"), (60, "1 min"), (125, "2 min 5s"), (1800, "30 min")],
    )
    def test_durations_read_like_a_status_line(self, seconds, text) -> None:
        assert human_duration(seconds) == text

    def test_waiting_names_the_hooks_progress_and_the_wait(self) -> None:
        state = WarmupState(status=WarmupStatus.RUNNING, message="RESUMING")
        assert warmup_waiting_details(state, 40.4) == (
            "waiting for source warmup: RESUMING, 40s"
        )

    def test_waiting_falls_back_to_the_status(self) -> None:
        state = WarmupState(status=WarmupStatus.NOT_STARTED)
        assert warmup_waiting_details(state, 5) == (
            "waiting for source warmup: not_started, 5s"
        )

    async def test_the_workflow_sets_it_while_waiting_and_clears_it(
        self, clock, safe_log
    ) -> None:
        polls = iter(
            [
                WarmupState(status=WarmupStatus.RUNNING, message="RESUMING"),
                WarmupState(status=WarmupStatus.READY),
            ]
        )
        _, _, patches = _gate(
            execute=lambda name, *a, **k: (
                next(polls) if name.endswith("warmup_state") else None
            ),
            start=_Handle(WarmupState(status=WarmupStatus.NOT_STARTED)),
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch(
                "application_sdk.app.base.workflow.set_current_details"
            ) as details,
        ):
            await _run_preflight_gate(
                _ResolvableInput(),
                "myapp",
                "crawl",
                warmup_ceiling_seconds=60,
                warmup_poll_seconds=20,
            )
        assert [c.args[0] for c in details.call_args_list] == [
            "waiting for source warmup: not_started, 0s",
            "waiting for source warmup: RESUMING, 20s",
            "",
        ]

    async def test_a_health_line_that_cannot_be_set_never_fails_the_gate(
        self, clock, safe_log
    ) -> None:
        execute_mock, _, patches = _gate(
            start=_Handle(WarmupState(status=WarmupStatus.READY))
        )
        with (
            patches[0],
            patches[1],
            patches[2],
            mock.patch(
                "application_sdk.app.base.workflow.set_current_details",
                side_effect=RuntimeError("not in workflow"),
            ),
        ):
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", warmup_ceiling_seconds=60
            )
        assert _tiers(execute_mock) == [CheckTier.FAST, CheckTier.WARMUP]


# ---------------------------------------------------------------------------
# Warmup fields on the gate rows (FND-3041)
# ---------------------------------------------------------------------------


def _warmups(execute_mock: mock.AsyncMock) -> list[WarmupObservation | None]:
    return [
        call.args[1].warmup
        for call in execute_mock.call_args_list
        if call.args[0] == "myapp:preflight"
    ]


class TestTheWarmupRowFields:
    async def test_each_dispatch_carries_what_the_workflow_saw(
        self, clock, safe_log
    ) -> None:
        polls = iter(
            [
                WarmupState(status=WarmupStatus.RUNNING),
                WarmupState(status=WarmupStatus.RUNNING),
                WarmupState(status=WarmupStatus.READY),
            ]
        )
        execute_mock, _, patches = _gate(
            execute=lambda name, *a, **k: (
                next(polls) if name.endswith("warmup_state") else None
            ),
            start=_Handle(WarmupState(status=WarmupStatus.NOT_STARTED)),
        )
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(),
                "myapp",
                "crawl",
                warmup_ceiling_seconds=60,
                warmup_poll_seconds=10,
            )
        fast, ready = _warmups(execute_mock)
        assert fast == WarmupObservation(outcome=WarmupOutcome.WARMING)
        assert ready is not None
        assert ready.outcome is WarmupOutcome.READY
        assert ready.duration_ms == 30_000.0
        # Status changes only: the repeated RUNNING poll adds nothing.
        assert [(t.status, t.at_ms) for t in ready.transitions] == [
            (WarmupStatus.NOT_STARTED, 0.0),
            (WarmupStatus.RUNNING, 10_000.0),
            (WarmupStatus.READY, 30_000.0),
        ]

    async def test_an_app_without_a_warmup_dispatches_no_observation(
        self, clock, safe_log
    ) -> None:
        execute_mock, _, patches = _gate()
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(_ResolvableInput(), "myapp", "crawl")
        assert _warmups(execute_mock) == [None]

    async def test_a_failed_warmup_row_says_failed(self, clock, safe_log) -> None:
        _, _, patches = _gate(
            start=_Handle(WarmupState(status=WarmupStatus.FAILED, message="gone"))
        )
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", warmup_ceiling_seconds=60
            )
        (row,) = _rows(safe_log)
        assert row[WARMUP_OUTCOME_KEY] == "failed"
        assert row["reason"] == "SOURCE_UNAVAILABLE"
        assert json.loads(row[WARMUP_TRANSITIONS_KEY]) == [
            {"status": "failed", "at_ms": 0.0}
        ]

    async def test_a_broken_warmup_row_says_broken(self, clock, safe_log) -> None:
        _, _, patches = _gate(start=_Handle(exc=RuntimeError("no worker")))
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", warmup_ceiling_seconds=60
            )
        (row,) = _rows(safe_log)
        assert row["outcome"] == "no_verdict"
        assert row[WARMUP_OUTCOME_KEY] == "broken"
        assert json.loads(row[WARMUP_TRANSITIONS_KEY]) == []

    async def test_a_fast_dispatch_that_dies_reports_warming(
        self, clock, safe_log
    ) -> None:
        _, _, patches = _gate(
            execute=RuntimeError("worker lost"),
            start=_Handle(WarmupState(status=WarmupStatus.RUNNING)),
        )
        with patches[0], patches[1], patches[2]:
            await _run_preflight_gate(
                _ResolvableInput(), "myapp", "crawl", warmup_ceiling_seconds=60
            )
        (row,) = _rows(safe_log)
        assert row[GATE_TIER_KEY] == "fast"
        assert row[WARMUP_OUTCOME_KEY] == "warming"
        assert WARMUP_DURATION_KEY not in row
        assert WARMUP_TRANSITIONS_KEY not in row

    async def test_the_activity_row_carries_the_dispatched_observation(
        self, capture_preflight_outcomes
    ) -> None:
        seen = WarmupObservation(
            outcome=WarmupOutcome.READY,
            duration_ms=12_000.0,
            transitions=[
                WarmupTransition(status=WarmupStatus.RUNNING, at_ms=0.0),
                WarmupTransition(status=WarmupStatus.READY, at_ms=12_000.0),
            ],
        )
        gate = build_preflight_gate_activity(_TwoTierHandler(), "myapp")
        await gate(PreflightGateInput(tier=CheckTier.WARMUP, warmup=seen))
        row = capture_preflight_outcomes.one
        assert row[WARMUP_OUTCOME_KEY] == "ready"
        assert row[WARMUP_DURATION_KEY] == 12_000.0
        assert json.loads(row[WARMUP_TRANSITIONS_KEY]) == [
            {"status": "running", "at_ms": 0.0},
            {"status": "ready", "at_ms": 12_000.0},
        ]
        # Per-check tier rides in the matrix for a check that set one.
        (matrix_row,) = json.loads(row[CHECK_MATRIX_KEY])
        assert matrix_row["tier"] == "warmup"

    def test_transitions_are_capped(self) -> None:
        seen = WarmupObservation(
            outcome=WarmupOutcome.EXHAUSTED,
            duration_ms=1.0,
            transitions=[
                WarmupTransition(status=WarmupStatus.RUNNING, at_ms=float(i))
                for i in range(WARMUP_TRANSITIONS_MAX + 10)
            ],
        )
        row = gate_outcome_row(
            app_name="myapp",
            entrypoint="crawl",
            outcome=preflight_gate.PreflightRowOutcome.WOULD_BLOCK,
            reason="SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED",
            checks=[],
            mode=PreflightGateMode.SOFT,
            classification=preflight_gate.PreflightClassification.SOURCE_UNVERIFIABLE,
            duration_ms=1.0,
            budget_seconds=150,
            attempt=0,
            warmup=seen,
        )
        assert len(json.loads(row[WARMUP_TRANSITIONS_KEY])) == WARMUP_TRANSITIONS_MAX

    def test_every_warmup_key_reaches_otlp(self) -> None:
        """Not allowlisted means dropped before export, so not queryable at all."""
        assert {
            WARMUP_OUTCOME_KEY,
            WARMUP_DURATION_KEY,
            WARMUP_TRANSITIONS_KEY,
        } <= _KNOWN_EXTRA_KEYS


# ---------------------------------------------------------------------------
# The check activity's tier
# ---------------------------------------------------------------------------


class _TwoTierHandler(DefaultHandler):
    def __init__(self) -> None:
        self.tiers: list[CheckTier | None] = []

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.tiers.append(input.tier)
        return PreflightOutput(
            status=PreflightStatus.READY,
            checks=[
                PreflightCheck(name="reachable", passed=True),
                PreflightCheck(name="catalogScan", passed=True, tier=CheckTier.WARMUP),
            ],
        )


class _ColdCatalogHandler(DefaultHandler):
    """Ignores ``input.tier``: runs both tiers and fails on the WARMUP one."""

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        cold = NotFoundError(message="catalog not indexed yet")
        return PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="reachable", passed=True),
                PreflightCheck(
                    name="catalogScan", passed=False, tier=CheckTier.WARMUP, error=cold
                ),
            ],
            error=cold,
        )


def _two_tier_result(
    *, fast_passed: bool, aggregate: AppError | None, message: str = ""
) -> PreflightOutput:
    fast_error = None if fast_passed else AuthError(message="token expired")
    return PreflightOutput(
        status=PreflightStatus.NOT_READY,
        checks=[
            PreflightCheck(name="reachable", passed=fast_passed, error=fast_error),
            PreflightCheck(
                name="catalogScan",
                passed=False,
                tier=CheckTier.WARMUP,
                error=NotFoundError(message="catalog not indexed yet"),
            ),
        ],
        error=aggregate,
        message=message,
    )


class TestTheCheckActivityTier:
    async def test_a_tiered_dispatch_keeps_only_its_tier(
        self, capture_preflight_outcomes
    ) -> None:
        handler = _TwoTierHandler()
        gate = build_preflight_gate_activity(handler, "myapp")
        result = await gate(PreflightGateInput(tier=CheckTier.WARMUP))
        assert handler.tiers == [CheckTier.WARMUP]
        assert [c.name for c in result.checks] == ["catalogScan"]
        assert capture_preflight_outcomes.one[GATE_TIER_KEY] == "warmup"

    async def test_an_untiered_dispatch_is_unchanged(
        self, capture_preflight_outcomes
    ) -> None:
        handler = _TwoTierHandler()
        gate = build_preflight_gate_activity(handler, "myapp")
        result = await gate(PreflightGateInput())
        assert handler.tiers == [None]
        assert [c.name for c in result.checks] == ["reachable", "catalogScan"]
        assert GATE_TIER_KEY not in capture_preflight_outcomes.one

    async def test_a_dropped_warmup_failure_does_not_block_the_fast_dispatch(
        self, capture_preflight_outcomes
    ) -> None:
        gate = build_preflight_gate_activity(
            _ColdCatalogHandler(), "myapp", mode=PreflightGateMode.HARD
        )
        result = await gate(PreflightGateInput(tier=CheckTier.FAST))
        assert result.status is PreflightStatus.READY
        assert [c.name for c in result.checks] == ["reachable"]
        assert capture_preflight_outcomes.one["outcome"] == "proceeded"

    def test_filter_returns_the_same_object_when_nothing_is_dropped(self) -> None:
        result = PreflightOutput(
            status=PreflightStatus.READY,
            checks=[PreflightCheck(name="reachable", passed=True)],
        )
        assert filter_checks_to_tier(result, CheckTier.FAST) is result

    def test_a_verdict_caused_only_by_a_dropped_check_becomes_ready(self) -> None:
        result = _two_tier_result(
            fast_passed=True, aggregate=NotFoundError(message="catalog not indexed yet")
        )
        fast = filter_checks_to_tier(result, CheckTier.FAST)
        assert fast.status is PreflightStatus.READY
        assert fast.error is None
        assert fast.message == ""

    def test_an_untyped_verdict_naming_a_dropped_check_becomes_ready(self) -> None:
        result = _two_tier_result(
            fast_passed=True, aggregate=None, message="catalog not indexed yet"
        )
        assert (
            filter_checks_to_tier(result, CheckTier.FAST).status
            is PreflightStatus.READY
        )

    def test_a_kept_failure_keeps_the_verdict_and_takes_the_attribution(
        self,
    ) -> None:
        result = _two_tier_result(
            fast_passed=False,
            aggregate=NotFoundError(message="catalog not indexed yet"),
        )
        fast = filter_checks_to_tier(result, CheckTier.FAST)
        assert fast.status is PreflightStatus.NOT_READY
        assert fast.error is None
        assert [c.name for c in fast.checks if not c.passed] == ["reachable"]

    def test_the_handlers_own_reason_stands(self) -> None:
        """An aggregate reason that is no check's failure is about the source."""
        result = _two_tier_result(
            fast_passed=True, aggregate=AuthError(message="account locked")
        )
        fast = filter_checks_to_tier(result, CheckTier.FAST)
        assert fast.status is PreflightStatus.NOT_READY
        assert fast.error is not None
        assert fast.error.message == "account locked"

    def test_the_row_carries_no_tier_key_unless_given_one(self) -> None:
        row = gate_outcome_row(
            app_name="myapp",
            entrypoint="crawl",
            outcome=preflight_gate.PreflightRowOutcome.PROCEEDED,
            reason="ready",
            checks=[],
            mode=PreflightGateMode.SOFT,
            classification=preflight_gate.PreflightClassification.VERDICT,
            duration_ms=1.0,
            budget_seconds=150,
            attempt=1,
        )
        assert GATE_TIER_KEY not in row
        assert WARMUP_OUTCOME_KEY not in row

    def test_an_untiered_check_has_no_tier_in_the_matrix(self) -> None:
        row = gate_outcome_row(
            app_name="myapp",
            entrypoint="crawl",
            outcome=preflight_gate.PreflightRowOutcome.PROCEEDED,
            reason="ready",
            checks=[PreflightCheck(name="reachable", passed=True)],
            mode=PreflightGateMode.SOFT,
            classification=preflight_gate.PreflightClassification.VERDICT,
            duration_ms=1.0,
            budget_seconds=150,
            attempt=1,
        )
        (matrix_row,) = json.loads(row[CHECK_MATRIX_KEY])
        assert "tier" not in matrix_row


# ---------------------------------------------------------------------------
# Declared-value clamps
# ---------------------------------------------------------------------------


class TestWarmupClamps:
    def test_an_undeclared_ceiling_is_no_warmup(self) -> None:
        assert gate_warmup_ceiling_seconds(None) == (None, "")

    def test_a_ceiling_below_the_floor_is_clamped(self) -> None:
        value, complaint = gate_warmup_ceiling_seconds(1)
        assert value == WARMUP_CEILING_MIN_SECONDS
        assert complaint

    def test_an_unusable_ceiling_still_declares_a_warmup(self) -> None:
        value, complaint = gate_warmup_ceiling_seconds("soon")
        assert value == WARMUP_CEILING_DEFAULT_SECONDS
        assert complaint

    def test_the_poll_defaults_and_clamps(self) -> None:
        assert gate_warmup_poll_seconds(None) == (WARMUP_POLL_DEFAULT_SECONDS, "")
        assert gate_warmup_poll_seconds(0)[0] == 1


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
    captured: dict[str, list] = {}

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


class TestWorkerRegistration:
    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_an_app_with_a_ceiling_registers_both_warmup_activities(self) -> None:
        class _WarmApp(App):
            preflight_warmup_ceiling_seconds = 120

            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        name = AppRegistry.get_instance().list_all()[0].name
        names = _registered_names()
        assert f"{name}:preflight_warmup_start" in names
        assert f"{name}:preflight_warmup_state" in names

    def test_an_app_without_a_ceiling_registers_none(self) -> None:
        class _ColdApp(App):
            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        names = _registered_names()
        assert not [n for n in names if "warmup" in n]
        assert any(n.endswith(":preflight") for n in names)

    def test_a_task_named_like_a_warmup_activity_is_rejected(self) -> None:
        class _CollidingWarmApp(App):
            preflight_warmup_ceiling_seconds = 120

            @task(timeout_seconds=60)
            async def preflight_warmup_state(
                self, input: _WarmupWorkerInput
            ) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        with pytest.raises(WorkerActivityNameCollisionError) as caught:
            create_worker(_mock_client(), enable_sdr=False)
        assert "preflight_warmup_state" in str(caught.value)

    def test_the_same_task_name_is_free_for_an_app_without_a_warmup(self) -> None:
        class _ColdAppWithTheName(App):
            @task(timeout_seconds=60)
            async def preflight_warmup_state(
                self, input: _WarmupWorkerInput
            ) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

            async def run(self, input: _WarmupWorkerInput) -> _WarmupWorkerOutput:
                return _WarmupWorkerOutput()

        names = _registered_names()
        assert (
            names.count(
                f"{AppRegistry.get_instance().list_all()[0].name}:preflight_warmup_state"
            )
            == 1
        )
