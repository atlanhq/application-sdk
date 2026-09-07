"""Gate budget resolution and no-verdict classification (CNCT-99).

Separate from ``test_preflight_gate_activity`` (verdict/credential plumbing) —
this module covers the budget the gate enforces and what it does when it cannot
reach a verdict at all.

The load-bearing rule: a failure the *source* caused (probe overran the budget,
handler crashed, credential absent) is ``source_unverifiable`` and is subject to
gate mode; a failure the *gate's own plumbing* caused (secret-store outage, rate
limit, worker gone) is ``gate_broken`` and always fails open, in both modes.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest import mock

import pytest

from application_sdk.credentials.errors import CredentialNotFoundError
from application_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    RateLimitedError,
)
from application_sdk.execution._temporal.preflight_gate import (
    CLASSIFICATION_SOURCE_UNVERIFIABLE,
    FAILURE_AUDIENCE_KEY,
    GATE_ATTEMPTS_DEFAULT,
    GATE_ATTEMPTS_MAX,
    GATE_ATTEMPTS_MIN,
    GATE_RETRY,
    GATE_TIMEOUT_DEFAULT_SECONDS,
    GATE_TIMEOUT_MAX_SECONDS,
    GATE_TIMEOUT_MIN_SECONDS,
    PREFLIGHT_FAILED_ERROR_TYPE,
    PREFLIGHT_NO_VERDICT_ERROR_TYPE,
    PREFLIGHT_POSTURE_EVENT,
    PreflightGateInput,
    build_preflight_gate_activity,
    gate_timeouts,
    log_gate_posture,
    resolve_gate_attempts,
    resolve_gate_budget_seconds,
)
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightInput,
    PreflightOutput,
    PreflightStatus,
)
from application_sdk.observability.logger_adaptor import (
    _KNOWN_EXTRA_KEYS,
    GATE_ATTEMPTS_KEY,
    GATE_CLASSIFICATION_KEY,
    GATE_DURATION_KEY,
    GATE_MODE_KEY,
    GATE_TIMEOUT_KEY,
    PREFLIGHT_SURFACE_KEY,
)
from application_sdk.testing.preflight import outcome_rows, single_outcome

_GATE = "application_sdk.execution._temporal.preflight_gate"


class _SlowHandler(DefaultHandler):
    """Sleeps past the budget, so the gate's own wait_for fires."""

    def __init__(self, sleep_for: float = 5.0) -> None:
        self._sleep_for = sleep_for
        self.completed = False

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        await asyncio.sleep(self._sleep_for)
        self.completed = True
        return PreflightOutput(status=PreflightStatus.READY, checks=[])


class _RaisingHandler(DefaultHandler):
    """Raises a caller-supplied exception from preflight_check."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise self._exc


class _RecordingHandler(DefaultHandler):
    """Records the budget it was handed."""

    def __init__(self) -> None:
        self.preflight_input: PreflightInput | None = None

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        self.preflight_input = input
        return PreflightOutput(status=PreflightStatus.READY, checks=[])


def _gate(handler, *, enforce: bool, budget: float = 0.3):
    return build_preflight_gate_activity(
        handler, app_name="myapp", enforce=enforce, budget_seconds=budget
    )


# Both scans are shared: application_sdk.testing.preflight owns them, including
# the exactly-one assertion that catches a double emission.
_outcome_rows = outcome_rows
_outcome = single_outcome


def _no_outcome(mock_logger) -> bool:
    return not _outcome_rows(mock_logger)


class TestBudgetResolution:
    """The per-app budget is clamped once, at resolution, never at use."""

    def test_default_when_unset(self) -> None:
        assert resolve_gate_budget_seconds(None) == GATE_TIMEOUT_DEFAULT_SECONDS

    def test_in_range_value_is_honoured(self) -> None:
        assert resolve_gate_budget_seconds(60) == 60

    def test_above_ceiling_is_clamped(self) -> None:
        # A slow source is an app-specific fact, but an unbounded gate budget
        # stalls every run's time-to-first-activity — hence a hard ceiling.
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            assert resolve_gate_budget_seconds(500) == GATE_TIMEOUT_MAX_SECONDS
        mock_logger.warning.assert_called_once()

    def test_below_floor_is_clamped(self) -> None:
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            assert resolve_gate_budget_seconds(2) == GATE_TIMEOUT_MIN_SECONDS
        mock_logger.warning.assert_called_once()

    @pytest.mark.parametrize("raw", ["abc", "", [], {}, object()])
    def test_garbage_falls_back_to_default(self, raw) -> None:
        # Never raise — a malformed ClassVar must not stop the worker booting.
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            assert resolve_gate_budget_seconds(raw) == GATE_TIMEOUT_DEFAULT_SECONDS
        mock_logger.warning.assert_called_once()

    def test_numeric_string_is_accepted(self) -> None:
        assert resolve_gate_budget_seconds("60") == 60

    def test_bool_is_not_a_budget(self) -> None:
        # bool is an int subclass; True would silently clamp to the floor.
        with mock.patch(f"{_GATE}.logger"):
            assert resolve_gate_budget_seconds(True) == GATE_TIMEOUT_DEFAULT_SECONDS


class TestTimeoutDerivation:
    """start_to_close and schedule_to_close both derive from the one budget."""

    def test_start_to_close_adds_headroom_for_classification(self) -> None:
        # The activity's own wait_for must fire *before* Temporal's timeout,
        # otherwise the activity never runs its except and the classification
        # is lost — which is the whole CNCT-99 defect.
        start_to_close, _ = gate_timeouts(25)
        assert start_to_close.total_seconds() > 25

    def test_schedule_to_close_fits_two_attempts(self) -> None:
        # Otherwise GATE_RETRY is cosmetic: the second attempt cannot start
        # before the schedule cap fires.
        for budget in (GATE_TIMEOUT_MIN_SECONDS, 25, GATE_TIMEOUT_MAX_SECONDS):
            start_to_close, schedule_to_close = gate_timeouts(budget)
            assert schedule_to_close.total_seconds() >= (
                GATE_RETRY.maximum_attempts * start_to_close.total_seconds()
            )

    def test_scales_with_budget(self) -> None:
        small, _ = gate_timeouts(GATE_TIMEOUT_MIN_SECONDS)
        large, _ = gate_timeouts(GATE_TIMEOUT_MAX_SECONDS)
        assert large > small

    @pytest.mark.parametrize("raw", [None, "abc", 10_000, -5, True])
    def test_clamps_its_own_input_silently(self, raw) -> None:
        # The workflow sizes activity timeouts from this on every run, so it must
        # land on the same number the activity was built with — without
        # re-warning per run about a value the worker already complained about
        # once at boot.
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            start_to_close, schedule_to_close = gate_timeouts(raw)
        assert start_to_close.total_seconds() > GATE_TIMEOUT_MIN_SECONDS
        assert schedule_to_close > start_to_close
        mock_logger.warning.assert_not_called()

    def test_agrees_with_the_worker_side_resolution(self) -> None:
        # Two independent readers of the same ClassVar: the workflow (timeouts)
        # and the worker (activity budget). If they diverged, Temporal's timeout
        # could beat the activity's own and the classification would be lost.
        for raw in (None, "abc", 10_000, 60):
            with mock.patch(f"{_GATE}.logger"):
                budget = resolve_gate_budget_seconds(raw)
                from_workflow, _ = gate_timeouts(raw)
            assert from_workflow.total_seconds() > budget


class TestRemainingBudget:
    """The handler is handed what is *left*, not the nominal budget."""

    async def test_handler_receives_budget_minus_resolution(self) -> None:
        handler = _RecordingHandler()
        gate = _gate(handler, enforce=False, budget=30)

        async def _slow_resolve(_input):
            await asyncio.sleep(0.2)
            return [], {}

        with (
            mock.patch(f"{_GATE}._resolve_gate_credentials", _slow_resolve),
            mock.patch(f"{_GATE}.logger"),
        ):
            await gate(PreflightGateInput())

        assert handler.preflight_input is not None
        # Strictly less than the nominal budget: resolution already spent some.
        assert handler.preflight_input.timeout_seconds < 30

    async def test_budget_exhausted_by_resolution_is_gate_broken(self) -> None:
        # Credential resolution is gate plumbing, not the source. If the vault
        # is slow enough to eat the whole budget, that is not evidence about
        # the source — so it fails open even in hard mode, and the handler is
        # never called with a zero budget.
        handler = _RecordingHandler()
        gate = _gate(handler, enforce=True, budget=0.2)

        async def _slow_resolve(_input):
            await asyncio.sleep(0.4)
            return [], {}

        with (
            mock.patch(f"{_GATE}._resolve_gate_credentials", _slow_resolve),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(DependencyUnavailableError):
                await gate(PreflightGateInput())

        assert handler.preflight_input is None
        assert _no_outcome(mock_logger)


class TestSourceUnverifiableAppliesMode:
    """Budget overrun / crash / missing credential — mode decides."""

    async def test_budget_overrun_blocks_in_hard_mode(self) -> None:
        handler = _SlowHandler()
        gate = _gate(handler, enforce=True)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())

        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        event = _outcome(mock_logger)
        assert event["outcome"] == "blocked"
        assert event[GATE_CLASSIFICATION_KEY] == CLASSIFICATION_SOURCE_UNVERIFIABLE
        assert event[GATE_MODE_KEY] == "hard"
        assert handler.completed is False  # actually cancelled, not just timed out
        # The overrun must stay attributed as a timeout. _no_verdict raises in
        # hard mode, so calling it from inside the guarded try re-caught its own
        # raise and re-classified TIMEOUT -> INTERNAL, losing the budget message.
        assert event["reason"] == "TIMEOUT"
        assert "budget" in str(excinfo.value)
        # One record for the whole event (FND-901): the outcome row itself is
        # the ERROR, carrying the diagnostic exception and who must act.
        assert mock_logger.error.call_count == 1
        assert mock_logger.error.call_args.args[0] == "Preflight gate outcome"
        assert event["exc_info"] is not None
        assert event[FAILURE_AUDIENCE_KEY] == "APP_OWNER"

    async def test_budget_overrun_reports_and_proceeds_in_soft_mode(self) -> None:
        handler = _SlowHandler()
        gate = _gate(handler, enforce=False)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            result = await gate(PreflightGateInput())

        assert result.status is PreflightStatus.NOT_READY
        event = _outcome(mock_logger)
        assert event["outcome"] == "would_block"
        assert event[GATE_CLASSIFICATION_KEY] == CLASSIFICATION_SOURCE_UNVERIFIABLE
        assert event[GATE_MODE_KEY] == "soft"
        # Unverifiable is a real failure in both modes — the row stays ERROR
        # even when soft mode proceeds (the run continued unverified).
        assert mock_logger.error.call_count == 1
        assert mock_logger.info.call_count == 0

    async def test_handler_crash_blocks_in_hard_mode(self) -> None:
        gate = _gate(_RaisingHandler(RuntimeError("boom")), enforce=True)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())

        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert (
            _outcome(mock_logger)[GATE_CLASSIFICATION_KEY]
            == CLASSIFICATION_SOURCE_UNVERIFIABLE
        )

    async def test_handler_crash_proceeds_in_soft_mode(self) -> None:
        gate = _gate(_RaisingHandler(RuntimeError("boom")), enforce=False)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        assert _outcome(mock_logger)["outcome"] == "would_block"

    async def test_credential_not_found_blocks_in_hard_mode(self) -> None:
        gate = _gate(_RaisingHandler(CredentialNotFoundError("nope")), enforce=True)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert (
            _outcome(mock_logger)[GATE_CLASSIFICATION_KEY]
            == CLASSIFICATION_SOURCE_UNVERIFIABLE
        )

    async def test_typed_source_error_blocks_in_hard_mode(self) -> None:
        # AUTH is the source's own answer about readiness, not gate plumbing.
        gate = _gate(_RaisingHandler(AuthError(message="bad creds")), enforce=True)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception):
                await gate(PreflightGateInput())
        assert (
            _outcome(mock_logger)[GATE_CLASSIFICATION_KEY]
            == CLASSIFICATION_SOURCE_UNVERIFIABLE
        )


class _CancellationSwallowingHandler(DefaultHandler):
    """Catches the gate's cancellation and keeps going — the defensive-handler
    shape that defeats ``asyncio.wait_for`` entirely."""

    def __init__(self, *, then_return: bool) -> None:
        self._then_return = then_return

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        try:
            await asyncio.sleep(5)
        except BaseException:
            if self._then_return:
                # wait_for would hand this straight back as a real verdict.
                return PreflightOutput(status=PreflightStatus.READY, checks=[])
            await asyncio.sleep(5)  # ignores the cancel and keeps working
        return PreflightOutput(status=PreflightStatus.READY, checks=[])


class TestUncooperativeHandlerCannotDefeatTheBudget:
    """The budget must hold even when the handler does not cooperate.

    ``asyncio.wait_for`` cancels the handler and then *awaits* it, so a handler
    that swallows CancelledError either returns a value (enforcement silently
    skipped) or runs past start_to_close (Temporal kills the activity and the
    classification is lost — the original CNCT-99 defect through another door).
    """

    async def test_swallow_and_return_does_not_become_a_verdict(self) -> None:
        gate = _gate(
            _CancellationSwallowingHandler(then_return=True), enforce=True, budget=0.3
        )
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        event = _outcome(mock_logger)
        assert event["outcome"] == "blocked"
        assert event[GATE_CLASSIFICATION_KEY] == CLASSIFICATION_SOURCE_UNVERIFIABLE

    async def test_ignoring_the_cancel_does_not_hold_the_activity_open(self) -> None:
        # The gate must classify at the deadline, not wait for the handler to
        # unwind — otherwise it blows start_to_close and loses the verdict.
        gate = _gate(
            _CancellationSwallowingHandler(then_return=False), enforce=True, budget=0.3
        )
        loop = asyncio.get_running_loop()
        started = loop.time()
        with mock.patch(f"{_GATE}.logger"):
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        elapsed = loop.time() - started
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert elapsed < 2.0, f"gate waited {elapsed:.1f}s for an uncooperative handler"


class TestCollapsedPlumbingIsNotACredentialProblem:
    """The resolver collapses any unexpected vault error into
    ``CredentialNotFoundError``, so not-found alone cannot be trusted as a
    config fact — otherwise a transport blip hard-blocks a healthy run."""

    @pytest.mark.parametrize("enforce", [True, False])
    async def test_not_found_wrapping_a_transport_error_fails_open(
        self, enforce: bool
    ) -> None:
        collapsed = CredentialNotFoundError("guid-1")
        collapsed.__cause__ = ConnectionResetError("dapr socket closed")

        async def _raise(_input):
            raise collapsed

        gate = _gate(_RecordingHandler(), enforce=enforce, budget=5)
        with (
            mock.patch(f"{_GATE}._resolve_gate_credentials", _raise),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(CredentialNotFoundError):
                await gate(PreflightGateInput())
        assert _no_outcome(mock_logger)

    async def test_definitive_absence_still_applies_mode(self) -> None:
        # A genuinely missing credential (no cause) remains a config fact the
        # run can be blamed for — that behaviour must survive the fix above.
        async def _raise(_input):
            raise CredentialNotFoundError("guid-1")

        gate = _gate(_RecordingHandler(), enforce=True, budget=5)
        with (
            mock.patch(f"{_GATE}._resolve_gate_credentials", _raise),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert (
            _outcome(mock_logger)[GATE_CLASSIFICATION_KEY]
            == CLASSIFICATION_SOURCE_UNVERIFIABLE
        )


class TestHandlerRaisedBlockPassesThrough:
    async def test_deliberate_block_is_not_rewrapped(self) -> None:
        # A handler that raises the block itself already carries a verdict; the
        # gate must pass it through rather than re-wrap it as an unverifiable
        # source, which would emit a second row and relabel the failure.
        from application_sdk.execution.errors import ApplicationError

        block = ApplicationError(
            "Preflight failed: bad creds",
            type=PREFLIGHT_FAILED_ERROR_TYPE,
            non_retryable=True,
        )
        gate = _gate(_RaisingHandler(block), enforce=True, budget=5)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value is block
        assert _outcome_rows(mock_logger) == []


class TestGateBrokenAlwaysFailsOpen:
    """Plumbing failures propagate to the workflow's fail-open, in both modes."""

    @pytest.mark.parametrize("enforce", [True, False])
    async def test_dependency_outage_propagates(self, enforce: bool) -> None:
        exc = DependencyUnavailableError(message="dapr down", service="secret_store")
        gate = _gate(_RaisingHandler(exc), enforce=enforce)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(DependencyUnavailableError):
                await gate(PreflightGateInput())
        # The workflow owns the no_verdict row for this path, not the activity.
        assert _no_outcome(mock_logger)

    @pytest.mark.parametrize("enforce", [True, False])
    async def test_rate_limit_is_not_a_verdict(self, enforce: bool) -> None:
        # A 429 says "ask me later", not "the source is not ready". Collapsing
        # it into NOT_READY makes hard mode fail *closed* on a transient.
        gate = _gate(_RaisingHandler(RateLimitedError(message="429")), enforce=enforce)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(RateLimitedError):
                await gate(PreflightGateInput())
        assert _no_outcome(mock_logger)


class TestRetryAwareness:
    """A non-final attempt retries; only the last one becomes a verdict."""

    async def test_non_final_attempt_retries_without_emitting(self) -> None:
        gate = _gate(_SlowHandler(), enforce=True)
        info = mock.MagicMock()
        info.attempt = 1
        info.start_to_close_timeout = timedelta(seconds=30)
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=info),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())

        # Pin the positive type: "not the block type" would pass for any error
        # at all. The workflow must see a retryable no-verdict, so that it does
        # not abort on the first slow attempt and the retry actually happens.
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_NO_VERDICT_ERROR_TYPE
        assert excinfo.value.non_retryable is False
        assert _no_outcome(mock_logger)

    async def test_final_attempt_applies_mode(self) -> None:
        gate = _gate(_SlowHandler(), enforce=True)
        info = mock.MagicMock()
        info.attempt = GATE_RETRY.maximum_attempts
        info.start_to_close_timeout = timedelta(seconds=30)
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=info),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())

        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert _outcome(mock_logger)["outcome"] == "blocked"

    async def test_missing_activity_context_treated_as_final(self) -> None:
        # Outside an activity (unit tests, direct calls) enforcement must not be
        # silently skipped — default to producing the verdict.
        gate = _gate(_SlowHandler(), enforce=True)
        with (
            mock.patch(f"{_GATE}.activity.info", side_effect=RuntimeError("no ctx")),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert _outcome(mock_logger)["outcome"] == "blocked"


class TestClassificationIsQueryable:
    def test_classification_key_reaches_otlp(self) -> None:
        # Unregistered kwargs are dropped by _build_extra_dict and never reach
        # ClickHouse — which would make the whole telemetry half a no-op.
        assert GATE_CLASSIFICATION_KEY in _KNOWN_EXTRA_KEYS

    def test_timeout_key_reaches_otlp(self) -> None:
        assert GATE_TIMEOUT_KEY in _KNOWN_EXTRA_KEYS


class TestPostureEvent:
    """The boot-time denominator: which apps believe they are gated."""

    @pytest.mark.parametrize(("enforce", "expected"), [(True, "hard"), (False, "soft")])
    def test_emits_mode_and_budget(self, enforce: bool, expected: str) -> None:
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            log_gate_posture("myapp", enforce=enforce, budget_seconds=60)
        call = mock_logger.info.call_args
        assert call.args[0] == PREFLIGHT_POSTURE_EVENT
        assert call.kwargs["app_name"] == "myapp"
        assert call.kwargs[GATE_MODE_KEY] == expected
        assert call.kwargs[GATE_TIMEOUT_KEY] == 60

    def test_emitted_for_soft_apps_too(self) -> None:
        # A hard-only row gives no denominator: adoption and posture drift are
        # only measurable if soft apps appear as well.
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            log_gate_posture("softapp", enforce=False, budget_seconds=25)
        mock_logger.info.assert_called_once()


class TestAttemptResolution:
    """Attempts are per-app, clamped, and never raise at boot."""

    def test_default_when_unset(self) -> None:
        assert resolve_gate_attempts(None) == GATE_ATTEMPTS_DEFAULT

    def test_in_range_value_is_honoured(self) -> None:
        assert resolve_gate_attempts(1) == 1

    @pytest.mark.parametrize(
        ("raw", "expected"), [(0, GATE_ATTEMPTS_MIN), (9, GATE_ATTEMPTS_MAX)]
    )
    def test_out_of_range_is_clamped(self, raw: int, expected: int) -> None:
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            assert resolve_gate_attempts(raw) == expected
        mock_logger.warning.assert_called_once()

    @pytest.mark.parametrize("raw", ["abc", [], object(), True])
    def test_garbage_falls_back_to_default(self, raw) -> None:
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            assert resolve_gate_attempts(raw) == GATE_ATTEMPTS_DEFAULT
        mock_logger.warning.assert_called_once()


class TestCeilingRaisedToThreeHundred:
    """A source that genuinely needs two minutes must be declarable.

    Measured p95 for one federated source sits near 124s, so the previous 120s
    ceiling could not express a budget that source could actually meet.
    """

    def test_ceiling_is_three_hundred(self) -> None:
        assert GATE_TIMEOUT_MAX_SECONDS == 300

    def test_a_slow_source_budget_survives_resolution(self) -> None:
        assert resolve_gate_budget_seconds(180) == 180

    def test_schedule_to_close_tracks_attempts(self) -> None:
        # One attempt at the ceiling must not reserve the two-attempt window:
        # that would hold a worker slot for twice as long as the owner asked.
        _, one = gate_timeouts(GATE_TIMEOUT_MAX_SECONDS, attempts=1)
        _, two = gate_timeouts(GATE_TIMEOUT_MAX_SECONDS, attempts=2)
        assert one < two

    def test_single_attempt_still_fits_its_own_attempt(self) -> None:
        start_to_close, schedule_to_close = gate_timeouts(
            GATE_TIMEOUT_MAX_SECONDS, attempts=1
        )
        assert schedule_to_close > start_to_close


class TestFinalAttemptFollowsThePerAppPolicy:
    """``_is_final_attempt`` must read the app's attempts, not a module default."""

    async def test_single_attempt_app_reaches_a_verdict_on_attempt_one(self) -> None:
        # With attempts=1 there is no retry to wait for, so attempt 1 is final
        # and the no-verdict must be applied rather than deferred.
        gate = build_preflight_gate_activity(
            _SlowHandler(5.0),
            app_name="myapp",
            enforce=False,
            budget_seconds=0.3,
            attempts=1,
        )
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with mock.patch(f"{_GATE}.activity.info") as info:
                info.return_value = mock.Mock(attempt=1, start_to_close_timeout=None)
                await gate(PreflightGateInput())
        assert _outcome(mock_logger)["outcome"] == "would_block"


class TestMeasuredDurationIsEmitted:
    """The gate reports its own elapsed time, not the handler's self-report.

    Production ``check_matrix`` durations proved untrustworthy: a handler
    abandoned at ``start_to_close`` keeps running and logs a duration far past
    the budget. A gate-measured number is the only one that can size a budget.
    """

    async def test_outcome_row_carries_measured_duration(self) -> None:
        gate = _gate(_RecordingHandler(), enforce=False, budget=30)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        row = _outcome(mock_logger)
        assert row[GATE_DURATION_KEY] >= 0
        assert row[GATE_DURATION_KEY] < 30_000

    async def test_outcome_row_carries_the_budget_in_force(self) -> None:
        # Headroom is duration / budget, so the denominator must be on the row —
        # otherwise every consumer has to join against the posture event.
        gate = _gate(_RecordingHandler(), enforce=False, budget=45)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        assert _outcome(mock_logger)[GATE_TIMEOUT_KEY] == 45

    async def test_outcome_row_carries_the_attempt(self) -> None:
        # Distinguishes a first-try success from a retry rescue; without it a
        # flaky-but-passing app is indistinguishable from a healthy one.
        gate = _gate(_RecordingHandler(), enforce=False, budget=30)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        assert _outcome(mock_logger)[GATE_ATTEMPTS_KEY] >= 1

    async def test_measured_duration_ignores_a_lying_handler(self) -> None:
        class _Liar(DefaultHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                return PreflightOutput(
                    status=PreflightStatus.READY,
                    checks=[
                        PreflightCheck(name="c", passed=True, duration_ms=292_800.0)
                    ],
                )

        gate = _gate(_Liar(), enforce=False, budget=30)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        assert _outcome(mock_logger)[GATE_DURATION_KEY] < 292_800.0

    async def test_timeout_row_also_carries_the_duration(self) -> None:
        # The row that matters most for sizing: it must not be the one that
        # omits the number.
        gate = _gate(_SlowHandler(5.0), enforce=False, budget=0.3)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        row = _outcome(mock_logger)
        assert row["outcome"] == "would_block"
        assert row[GATE_DURATION_KEY] > 0


class TestNewKeysReachTheWire:
    """Unregistered kwargs are dropped before OTLP, so registration is the wire."""

    @pytest.mark.parametrize(
        "key", [GATE_DURATION_KEY, GATE_ATTEMPTS_KEY, PREFLIGHT_SURFACE_KEY]
    )
    def test_key_is_registered(self, key: str) -> None:
        assert key in _KNOWN_EXTRA_KEYS
