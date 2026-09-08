"""Gate budget resolution and no-verdict classification (CNCT-99).

Separate from ``test_preflight_gate_activity`` (verdict/credential plumbing) —
this module covers the budget the gate enforces and what it does when it cannot
reach a verdict at all.

The load-bearing rule: a failure the *source* or the handler caused (probe overran
the budget, handler raised, credential absent) is ``source_unverifiable`` and is
subject to gate mode; a failure the *gate's own plumbing* caused (secret-store
outage, store probe, no worker) is ``gate_broken`` and always fails open, in both
modes. The line is drawn by who raised, never by the error's category.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest import mock

import pytest

from application_sdk.common.env_warnings import _REMOVED_ENV_VARS
from application_sdk.constants import PREFLIGHT_GATE_MODE_ENV
from application_sdk.credentials.errors import CredentialNotFoundError
from application_sdk.errors.categories import Audience, FailureCategory
from application_sdk.errors.leaves import (
    AuthError,
    DependencyUnavailableError,
    RateLimitedError,
    SourceUnavailableError,
)
from application_sdk.execution._temporal.preflight_gate import (
    FAILURE_AUDIENCE_KEY,
    GATE_ATTEMPTS_DEFAULT,
    GATE_ATTEMPTS_MAX,
    GATE_ATTEMPTS_MIN,
    GATE_OUTCOME_ROW_KEYS,
    GATE_TIMEOUT_DEFAULT_SECONDS,
    GATE_TIMEOUT_MAX_SECONDS,
    GATE_TIMEOUT_MIN_SECONDS,
    PREFLIGHT_FAILED_ERROR_TYPE,
    PREFLIGHT_FALLBACK_CODE,
    PREFLIGHT_NO_VERDICT_ERROR_TYPE,
    PREFLIGHT_POSTURE_EVENT,
    PreflightClassification,
    PreflightGateInput,
    PreflightRowOutcome,
    build_preflight_gate_activity,
    coerce_gate_mode,
    gate_attempts,
    gate_budget_seconds,
    gate_outcome_level,
    gate_timeouts,
    log_gate_posture,
    resolve_gate_mode,
)
from application_sdk.execution.errors import ApplicationError
from application_sdk.handler.base import DefaultHandler
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightGateMode,
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


def _gate(handler, *, mode: PreflightGateMode, budget: float = 0.3):
    return build_preflight_gate_activity(
        handler, app_name="myapp", mode=mode, budget_seconds=budget
    )


# Both scans are shared: application_sdk.testing.preflight owns them, including
# the exactly-one assertion that catches a double emission.
_outcome_rows = outcome_rows
_outcome = single_outcome


def _no_outcome(mock_logger) -> bool:
    return not _outcome_rows(mock_logger)


class _ReturningHandler(DefaultHandler):
    """Returns a caller-supplied verdict."""

    def __init__(self, output: PreflightOutput) -> None:
        self._output = output

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return self._output


def _non_final_attempt():
    info = mock.MagicMock()
    info.attempt = 1
    info.start_to_close_timeout = timedelta(seconds=30)
    return mock.patch(f"{_GATE}.activity.info", return_value=info)


def _primary_details(err: BaseException):
    details = getattr(err, "details", ())
    assert details, f"{type(err).__name__} left the gate without FailureDetails"
    return details[0]


class _Unserialisable(DependencyUnavailableError):
    """A typed plumbing fault whose FailureDetails cannot be built."""

    def to_failure_details(self):
        raise ValueError("evidence keys may not use secret-named fields")


def _patched_resolution(*, raises: BaseException | None = None, sleep: float = 0.0):
    async def _resolve(_input):
        if sleep:
            await asyncio.sleep(sleep)
        if raises is not None:
            raise raises
        return [], {}

    return mock.patch(f"{_GATE}._resolve_gate_credentials", _resolve)


class TestBudgetResolution:
    """The per-app budget is clamped once and the complaint travels with it.

    Pure and silent: the worker logs the complaint once at boot, the workflow
    discards it, and both land on the same number.
    """

    def test_default_when_unset(self) -> None:
        assert gate_budget_seconds(None) == (GATE_TIMEOUT_DEFAULT_SECONDS, "")

    def test_in_range_value_is_honoured(self) -> None:
        assert gate_budget_seconds(60) == (60, "")

    def test_numeric_string_is_accepted(self) -> None:
        assert gate_budget_seconds("60") == (60, "")

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(500, GATE_TIMEOUT_MAX_SECONDS), (2, GATE_TIMEOUT_MIN_SECONDS)],
    )
    def test_out_of_range_is_clamped_with_a_complaint(self, raw, expected) -> None:
        budget, complaint = gate_budget_seconds(raw)
        assert budget == expected
        assert "outside the supported" in complaint

    @pytest.mark.parametrize("raw", ["abc", "", [], {}, object(), True])
    def test_garbage_falls_back_to_default_with_a_complaint(self, raw) -> None:
        budget, complaint = gate_budget_seconds(raw)
        assert budget == GATE_TIMEOUT_DEFAULT_SECONDS
        assert complaint


class TestTimeoutDerivation:
    """start_to_close and schedule_to_close both derive from the one budget."""

    def test_start_to_close_adds_headroom_for_classification(self) -> None:
        # The activity's own wait_for must fire *before* Temporal's timeout,
        # otherwise the activity never runs its except and the classification
        # is lost — which is the whole CNCT-99 defect.
        start_to_close, _ = gate_timeouts(25)
        assert start_to_close.total_seconds() > 25

    def test_schedule_to_close_fits_two_attempts(self) -> None:
        # Otherwise the retry policy is cosmetic: the second attempt cannot start
        # before the schedule cap fires.
        for budget in (GATE_TIMEOUT_MIN_SECONDS, 25, GATE_TIMEOUT_MAX_SECONDS):
            start_to_close, schedule_to_close = gate_timeouts(budget)
            assert schedule_to_close.total_seconds() >= (
                GATE_ATTEMPTS_DEFAULT * start_to_close.total_seconds()
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
                budget, _ = gate_budget_seconds(raw)
                from_workflow, _ = gate_timeouts(raw)
            assert from_workflow.total_seconds() > budget


class TestRemainingBudget:
    """The handler is handed what is *left*, not the nominal budget."""

    async def test_handler_receives_budget_minus_resolution(self) -> None:
        handler = _RecordingHandler()
        gate = _gate(handler, mode=PreflightGateMode.SOFT, budget=30)

        with _patched_resolution(sleep=0.2), mock.patch(f"{_GATE}.logger"):
            await gate(PreflightGateInput())

        assert handler.preflight_input is not None
        # Strictly less than the nominal budget: resolution already spent some.
        assert handler.preflight_input.timeout_seconds < 30


class TestSourceUnverifiableAppliesMode:
    """Budget overrun / crash / missing credential — mode decides."""

    async def test_budget_overrun_blocks_in_hard_mode(self) -> None:
        handler = _SlowHandler()
        gate = _gate(handler, mode=PreflightGateMode.HARD)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())

        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        event = _outcome(mock_logger)
        assert event["outcome"] == "blocked"
        assert (
            event[GATE_CLASSIFICATION_KEY]
            == PreflightClassification.SOURCE_UNVERIFIABLE
        )
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
        gate = _gate(handler, mode=PreflightGateMode.SOFT)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            result = await gate(PreflightGateInput())

        assert result.status is PreflightStatus.NOT_READY
        event = _outcome(mock_logger)
        assert event["outcome"] == "would_block"
        assert (
            event[GATE_CLASSIFICATION_KEY]
            == PreflightClassification.SOURCE_UNVERIFIABLE
        )
        assert event[GATE_MODE_KEY] == "soft"
        # Unverifiable is a real failure in both modes — the row stays ERROR
        # even when soft mode proceeds (the run continued unverified).
        assert mock_logger.error.call_count == 1
        assert mock_logger.info.call_count == 0

    async def test_handler_crash_blocks_in_hard_mode(self) -> None:
        gate = _gate(_RaisingHandler(RuntimeError("boom")), mode=PreflightGateMode.HARD)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())

        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert (
            _outcome(mock_logger)[GATE_CLASSIFICATION_KEY]
            == PreflightClassification.SOURCE_UNVERIFIABLE
        )

    async def test_handler_crash_proceeds_in_soft_mode(self) -> None:
        gate = _gate(_RaisingHandler(RuntimeError("boom")), mode=PreflightGateMode.SOFT)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        assert _outcome(mock_logger)["outcome"] == "would_block"

    async def test_credential_not_found_blocks_in_hard_mode(self) -> None:
        gate = _gate(
            _RaisingHandler(CredentialNotFoundError("nope")),
            mode=PreflightGateMode.HARD,
        )
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert (
            _outcome(mock_logger)[GATE_CLASSIFICATION_KEY]
            == PreflightClassification.SOURCE_UNVERIFIABLE
        )

    async def test_typed_source_error_blocks_in_hard_mode(self) -> None:
        # AUTH is the source's own answer about readiness, not gate plumbing.
        gate = _gate(
            _RaisingHandler(AuthError(message="bad creds")), mode=PreflightGateMode.HARD
        )
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        err = excinfo.value
        assert err.type == PREFLIGHT_FAILED_ERROR_TYPE
        assert err.non_retryable is True
        assert _primary_details(err).category is FailureCategory.AUTH
        assert err.details[1]["checks"][0]["passed"] is False
        assert (
            _outcome(mock_logger)[GATE_CLASSIFICATION_KEY]
            == PreflightClassification.SOURCE_UNVERIFIABLE
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
            _CancellationSwallowingHandler(then_return=True),
            mode=PreflightGateMode.HARD,
            budget=0.3,
        )
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        event = _outcome(mock_logger)
        assert event["outcome"] == "blocked"
        assert (
            event[GATE_CLASSIFICATION_KEY]
            == PreflightClassification.SOURCE_UNVERIFIABLE
        )

    async def test_ignoring_the_cancel_does_not_hold_the_activity_open(self) -> None:
        # The gate must classify at the deadline, not wait for the handler to
        # unwind — otherwise it blows start_to_close and loses the verdict.
        gate = _gate(
            _CancellationSwallowingHandler(then_return=False),
            mode=PreflightGateMode.HARD,
            budget=0.3,
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

    @pytest.mark.parametrize("mode", [PreflightGateMode.HARD, PreflightGateMode.SOFT])
    async def test_not_found_wrapping_a_transport_error_fails_open(
        self, mode: PreflightGateMode
    ) -> None:
        collapsed = CredentialNotFoundError("guid-1")
        collapsed.__cause__ = ConnectionResetError("dapr socket closed")

        gate = _gate(_RecordingHandler(), mode=mode, budget=5)
        with (
            _patched_resolution(raises=collapsed),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value.type == "CredentialNotFoundError"
        assert excinfo.value.__cause__ is collapsed
        assert _no_outcome(mock_logger)

    async def test_definitive_absence_still_applies_mode(self) -> None:
        # A genuinely missing credential (no cause) remains a config fact the
        # run can be blamed for — that behaviour must survive the fix above.
        gate = _gate(_RecordingHandler(), mode=PreflightGateMode.HARD, budget=5)
        with (
            _patched_resolution(raises=CredentialNotFoundError("guid-1")),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert getattr(excinfo.value, "type", None) == PREFLIGHT_FAILED_ERROR_TYPE
        assert (
            _outcome(mock_logger)[GATE_CLASSIFICATION_KEY]
            == PreflightClassification.SOURCE_UNVERIFIABLE
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
        gate = _gate(_RaisingHandler(block), mode=PreflightGateMode.HARD, budget=5)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(Exception) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value is block
        assert _outcome_rows(mock_logger) == []


class TestHandlerRaisedPlumbingIsSourceSide:
    """Plumbing is decided by who raised it, not by the error's category.

    A handler cannot declare its source to be gate plumbing. Whatever escapes
    ``preflight_check`` is the handler's statement about the source if typed, or
    an app fault if not, and the mode applies to both. The only fail-open left
    is the gate's own frames: credential resolution and the store probes.
    """

    @pytest.mark.parametrize(
        ("exc", "category", "audience"),
        [
            (
                DependencyUnavailableError(message="db paused", service="source"),
                FailureCategory.DEPENDENCY_UNAVAILABLE,
                Audience.PLATFORM,
            ),
            (
                RateLimitedError(message="429"),
                FailureCategory.RATE_LIMITED,
                Audience.USER,
            ),
        ],
    )
    async def test_hard_mode_blocks_with_the_handlers_own_details(
        self, exc: Exception, category: FailureCategory, audience: Audience
    ) -> None:
        gate = _gate(_RaisingHandler(exc), mode=PreflightGateMode.HARD)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        err = excinfo.value
        assert err.type == PREFLIGHT_FAILED_ERROR_TYPE
        assert _primary_details(err).category is category
        assert _primary_details(err).audience is audience
        row = _outcome(mock_logger)
        assert row["outcome"] == "blocked"
        assert (
            row[GATE_CLASSIFICATION_KEY] == PreflightClassification.SOURCE_UNVERIFIABLE
        )
        assert row[FAILURE_AUDIENCE_KEY] == audience.value

    @pytest.mark.parametrize(
        "exc",
        [
            DependencyUnavailableError(message="db paused", service="source"),
            RateLimitedError(message="429"),
        ],
    )
    async def test_soft_mode_reports_and_proceeds(self, exc: Exception) -> None:
        gate = _gate(_RaisingHandler(exc), mode=PreflightGateMode.SOFT)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        row = _outcome(mock_logger)
        assert row["outcome"] == "would_block"
        assert (
            row[GATE_CLASSIFICATION_KEY] == PreflightClassification.SOURCE_UNVERIFIABLE
        )

    @pytest.mark.parametrize("mode", [PreflightGateMode.HARD, PreflightGateMode.SOFT])
    async def test_resolution_frame_plumbing_still_fails_open(
        self, mode: PreflightGateMode
    ) -> None:
        exc = DependencyUnavailableError(message="vault down", service="secret_store")
        gate = _gate(_RecordingHandler(), mode=mode)
        with (
            _patched_resolution(raises=exc),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        err = excinfo.value
        assert err.type == "DependencyUnavailableError"
        assert err.__cause__ is exc
        assert _primary_details(err).category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert err.details[1] == {"status": None, "checks": [], "attempt": 1}
        assert _no_outcome(mock_logger)


class TestVerdictOnAnyAttempt:
    """A source fault is a verdict on the attempt it happens; nothing waits for a retry."""

    async def test_first_attempt_overrun_blocks_in_hard_mode(self) -> None:
        gate = _gate(_SlowHandler(), mode=PreflightGateMode.HARD)
        with _non_final_attempt(), mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        assert excinfo.value.non_retryable is True
        row = _outcome(mock_logger)
        assert row["outcome"] == "blocked"
        assert row[GATE_ATTEMPTS_KEY] == 1

    async def test_first_attempt_typed_source_fault_blocks_in_hard_mode(self) -> None:
        exc = SourceUnavailableError(message="The source did not answer")
        gate = _gate(_RaisingHandler(exc), mode=PreflightGateMode.HARD)
        with _non_final_attempt(), mock.patch(f"{_GATE}.logger") as mock_logger:
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        err = excinfo.value
        assert err.type == PREFLIGHT_FAILED_ERROR_TYPE
        details = _primary_details(err)
        assert details.category is FailureCategory.SOURCE_UNAVAILABLE
        assert details.audience is Audience.USER
        assert details.app_name == "myapp"
        assert err.details[1]["status"] == PreflightStatus.NOT_READY.value
        (check,) = err.details[1]["checks"]
        assert check["passed"] is False
        assert _outcome(mock_logger)["outcome"] == "blocked"

    async def test_first_attempt_source_fault_reports_in_soft_mode(self) -> None:
        exc = SourceUnavailableError(message="The source did not answer")
        gate = _gate(_RaisingHandler(exc), mode=PreflightGateMode.SOFT)
        with _non_final_attempt(), mock.patch(f"{_GATE}.logger") as mock_logger:
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        assert _outcome(mock_logger)["outcome"] == "would_block"

    async def test_store_probe_failure_on_a_non_final_attempt_still_retries(
        self,
    ) -> None:
        async def _fail_store(result: PreflightOutput, budget, started) -> bool:
            result.checks.append(
                PreflightCheck(
                    name="objectStoreAccess:deployment",
                    passed=False,
                    error=DependencyUnavailableError(
                        message="store relocated", service="objectstore"
                    ).to_failure_details(),
                )
            )
            return True

        gate = build_preflight_gate_activity(
            _ReturningHandler(PreflightOutput(status=PreflightStatus.READY, checks=[])),
            app_name="myapp",
            mode=PreflightGateMode.HARD,
            budget_seconds=5,
            verify_storage=True,
        )
        with (
            _non_final_attempt(),
            mock.patch(f"{_GATE}._append_storage_checks", _fail_store),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        err = excinfo.value
        assert err.type == PREFLIGHT_NO_VERDICT_ERROR_TYPE
        assert err.non_retryable is False
        assert _primary_details(err).category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert _no_outcome(mock_logger)

    async def test_final_attempt_applies_mode(self) -> None:
        gate = _gate(_SlowHandler(), mode=PreflightGateMode.HARD)
        info = mock.MagicMock()
        info.attempt = GATE_ATTEMPTS_DEFAULT
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
        gate = _gate(_SlowHandler(), mode=PreflightGateMode.HARD)
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

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [(PreflightGateMode.HARD, "hard"), (PreflightGateMode.SOFT, "soft")],
    )
    def test_emits_mode_and_budget(
        self, mode: PreflightGateMode, expected: str
    ) -> None:
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            log_gate_posture("myapp", mode=mode, budget_seconds=60)
        call = mock_logger.info.call_args
        assert call.args[0] == PREFLIGHT_POSTURE_EVENT
        assert call.kwargs["app_name"] == "myapp"
        assert call.kwargs[GATE_MODE_KEY] == expected
        assert call.kwargs[GATE_TIMEOUT_KEY] == 60

    def test_emitted_for_soft_apps_too(self) -> None:
        # A hard-only row gives no denominator: adoption and posture drift are
        # only measurable if soft apps appear as well.
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            log_gate_posture("softapp", mode=PreflightGateMode.SOFT, budget_seconds=25)
        mock_logger.info.assert_called_once()


class TestAttemptResolution:
    """Attempts are per-app, clamped, and never raise at boot."""

    def test_default_when_unset(self) -> None:
        assert gate_attempts(None) == (GATE_ATTEMPTS_DEFAULT, "")

    def test_in_range_value_is_honoured(self) -> None:
        assert gate_attempts(1) == (1, "")

    @pytest.mark.parametrize(
        ("raw", "expected"), [(0, GATE_ATTEMPTS_MIN), (9, GATE_ATTEMPTS_MAX)]
    )
    def test_out_of_range_is_clamped_with_a_complaint(self, raw, expected) -> None:
        attempts, complaint = gate_attempts(raw)
        assert attempts == expected
        assert "outside the supported" in complaint

    @pytest.mark.parametrize("raw", ["abc", [], object(), True])
    def test_garbage_falls_back_to_default_with_a_complaint(self, raw) -> None:
        attempts, complaint = gate_attempts(raw)
        assert attempts == GATE_ATTEMPTS_DEFAULT
        assert complaint


class TestCeilingRaisedToThreeHundred:
    """A source that genuinely needs two minutes must be declarable.

    Measured p95 for one federated source sits near 124s, so the previous 120s
    ceiling could not express a budget that source could actually meet.
    """

    def test_ceiling_is_three_hundred(self) -> None:
        assert GATE_TIMEOUT_MAX_SECONDS == 300

    def test_a_slow_source_budget_survives_resolution(self) -> None:
        assert gate_budget_seconds(180)[0] == 180

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
            mode=PreflightGateMode.SOFT,
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
        gate = _gate(_RecordingHandler(), mode=PreflightGateMode.SOFT, budget=30)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        row = _outcome(mock_logger)
        assert row[GATE_DURATION_KEY] >= 0
        assert row[GATE_DURATION_KEY] < 30_000

    async def test_outcome_row_carries_the_budget_in_force(self) -> None:
        # Headroom is duration / budget, so the denominator must be on the row —
        # otherwise every consumer has to join against the posture event.
        gate = _gate(_RecordingHandler(), mode=PreflightGateMode.SOFT, budget=45)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        assert _outcome(mock_logger)[GATE_TIMEOUT_KEY] == 45

    async def test_outcome_row_carries_the_attempt(self) -> None:
        # Distinguishes a first-try success from a retry rescue; without it a
        # flaky-but-passing app is indistinguishable from a healthy one.
        gate = _gate(_RecordingHandler(), mode=PreflightGateMode.SOFT, budget=30)
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

        gate = _gate(_Liar(), mode=PreflightGateMode.SOFT, budget=30)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        assert _outcome(mock_logger)[GATE_DURATION_KEY] < 292_800.0

    async def test_timeout_row_also_carries_the_duration(self) -> None:
        # The row that matters most for sizing: it must not be the one that
        # omits the number.
        gate = _gate(_SlowHandler(5.0), mode=PreflightGateMode.SOFT, budget=0.3)
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


class TestEveryExitCarriesFailureDetails:
    """Every error that leaves the gate carries one FailureDetails.

    The automation engine and the dashboards read ``details[0]`` off the Temporal
    failure. A retry marker or a plumbing re-raise that carries only a class name
    and a message leaves them with nothing to attribute, on exactly the runs
    where attribution matters most.
    """

    async def test_budget_overrun_carries_a_timeout_failure(self) -> None:
        gate = _gate(_SlowHandler(), mode=PreflightGateMode.HARD, budget=0.3)
        with mock.patch(f"{_GATE}.logger"):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        details = _primary_details(excinfo.value)
        assert details.category is FailureCategory.TIMEOUT
        assert details.app_name == "myapp"

    async def test_untyped_crash_carries_an_internal_failure(self) -> None:
        gate = _gate(_RaisingHandler(RuntimeError("boom")), mode=PreflightGateMode.HARD)
        with mock.patch(f"{_GATE}.logger"):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        details = _primary_details(excinfo.value)
        assert details.category is FailureCategory.INTERNAL
        assert details.audience is Audience.APP_OWNER


class TestResolutionRunsUnderTheBudget:
    """Credential resolution shares the gate's deadline instead of having none."""

    async def test_hung_resolution_ends_at_the_budget_as_plumbing(self) -> None:
        handler = _RecordingHandler()
        gate = _gate(handler, mode=PreflightGateMode.HARD, budget=0.4)
        with (
            _patched_resolution(sleep=60),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            started = asyncio.get_running_loop().time()
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
            elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 2.0, "the gate waited on resolution past its own budget"
        assert excinfo.value.type == "DependencyUnavailableError"
        details = _primary_details(excinfo.value)
        assert details.category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert "consumed the entire preflight budget" in details.message
        assert handler.preflight_input is None
        assert _no_outcome(mock_logger)

    async def test_resolution_inside_the_budget_still_reaches_the_handler(self) -> None:
        handler = _RecordingHandler()
        gate = _gate(handler, mode=PreflightGateMode.HARD, budget=5)
        with _patched_resolution(sleep=0.05), mock.patch(f"{_GATE}.logger"):
            await gate(PreflightGateInput())
        assert handler.preflight_input is not None


class TestBlockIsNotLostToPersistence:
    """A results-store failure can never swallow or delay the verdict."""

    async def test_persist_raising_does_not_swallow_the_block(self) -> None:
        gate = _gate(
            _RaisingHandler(AuthError(message="bad creds")), mode=PreflightGateMode.HARD
        )
        with (
            mock.patch(
                f"{_GATE}.persist_check_result", side_effect=RuntimeError("store down")
            ),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        assert _outcome(mock_logger)["outcome"] == "blocked"


class TestProceededRowNamesTheFailedCheck:
    """A proceeded row with a failed check carries that check's code as reason.

    A throttled or otherwise unverified preflight that proceeds is invisible if
    the row only says PARTIAL; the code is what lets a dashboard rank it.
    """

    @staticmethod
    def _typed_failure(code_source: Exception) -> PreflightCheck:
        return PreflightCheck(
            name="workspaceAccess",
            passed=False,
            error=code_source.to_failure_details(),
        )

    async def test_partial_with_typed_failure_uses_the_error_code(self) -> None:
        failed = self._typed_failure(RateLimitedError(message="429"))
        output = PreflightOutput(status=PreflightStatus.PARTIAL, checks=[failed])
        gate = _gate(_ReturningHandler(output), mode=PreflightGateMode.HARD)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            result = await gate(PreflightGateInput())
        row = _outcome(mock_logger)
        assert result.status is PreflightStatus.PARTIAL
        assert row["outcome"] == "proceeded"
        assert row["reason"] == failed.error.code

    async def test_ready_with_failed_advisory_uses_the_error_code(self) -> None:
        failed = self._typed_failure(AuthError(message="advisory scope missing"))
        output = PreflightOutput(
            status=PreflightStatus.READY,
            checks=[PreflightCheck(name="auth", passed=True), failed],
        )
        gate = _gate(_ReturningHandler(output), mode=PreflightGateMode.SOFT)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        assert _outcome(mock_logger)["reason"] == failed.error.code

    async def test_partial_with_untyped_failure_uses_the_fallback_code(self) -> None:
        failed = PreflightCheck(name="scanner", passed=False, message="no scanner")
        output = PreflightOutput(status=PreflightStatus.PARTIAL, checks=[failed])
        gate = _gate(_ReturningHandler(output), mode=PreflightGateMode.HARD)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        assert _outcome(mock_logger)["reason"] == PREFLIGHT_FALLBACK_CODE

    async def test_clean_ready_keeps_the_status_as_reason(self) -> None:
        output = PreflightOutput(
            status=PreflightStatus.READY,
            checks=[PreflightCheck(name="auth", passed=True)],
        )
        gate = _gate(_ReturningHandler(output), mode=PreflightGateMode.HARD)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        assert _outcome(mock_logger)["reason"] == PreflightStatus.READY.value


class TestGateModeResolution:
    """Gate posture comes from ``App.preflight_gate_mode`` alone, as one enum.

    Only the enum member or the literal "hard" enforces; anything unknown falls
    back to soft so a run is never blocked by a typo. There is no deploy-time
    override: the worker and the workflow both coerce the same attribute once,
    so the two frames cannot disagree.
    """

    @pytest.mark.parametrize(
        "declared", [PreflightGateMode.HARD, "hard", " Hard ", "HARD"]
    )
    def test_hard_declarations_enforce(self, declared) -> None:
        assert coerce_gate_mode(declared) is PreflightGateMode.HARD

    @pytest.mark.parametrize(
        "declared", [PreflightGateMode.SOFT, "soft", "on", "", None, True, 1]
    )
    def test_everything_else_is_soft(self, declared) -> None:
        assert coerce_gate_mode(declared) is PreflightGateMode.SOFT

    def test_resolves_from_the_app_class(self) -> None:
        hard = type("Hard", (), {"preflight_gate_mode": "hard"})
        soft = type("Soft", (), {"preflight_gate_mode": PreflightGateMode.SOFT})
        undeclared = type("Plain", (), {})
        assert resolve_gate_mode(hard) is PreflightGateMode.HARD
        assert resolve_gate_mode(soft) is PreflightGateMode.SOFT
        assert resolve_gate_mode(undeclared) is PreflightGateMode.SOFT
        assert resolve_gate_mode(None) is PreflightGateMode.SOFT

    @pytest.mark.parametrize("stale_value", ["hard", "soft", "enabled", ""])
    def test_the_removed_env_var_no_longer_changes_the_posture(
        self, monkeypatch, stale_value: str
    ) -> None:
        monkeypatch.setenv(PREFLIGHT_GATE_MODE_ENV, stale_value)
        assert resolve_gate_mode(type("Soft", (), {"preflight_gate_mode": "soft"})) is (
            PreflightGateMode.SOFT
        )
        assert resolve_gate_mode(type("Hard", (), {"preflight_gate_mode": "hard"})) is (
            PreflightGateMode.HARD
        )

    def test_the_removed_env_var_is_registered_and_still_importable(self) -> None:
        assert PREFLIGHT_GATE_MODE_ENV == "ATLAN_PREFLIGHT_GATE_MODE"
        assert PREFLIGHT_GATE_MODE_ENV in _REMOVED_ENV_VARS


class TestPlumbingPayloadNeverLosesItsPrimary:
    """``details[0]`` is a FailureDetails on every plumbing exit, without exception.

    The generic converter drops the details when the leaf's own evidence cannot
    be serialised, and a splat of that empty tuple would put the status
    envelope at index 0, where the Automation Engine reads the attribution.
    """

    async def test_an_unserialisable_leaf_still_leaves_a_failure_details(
        self,
    ) -> None:
        gate = _gate(_RecordingHandler(), mode=PreflightGateMode.HARD)
        with (
            _patched_resolution(raises=_Unserialisable(message="vault down")),
            mock.patch(f"{_GATE}.logger"),
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        err = excinfo.value
        primary = _primary_details(err)
        assert primary.category is FailureCategory.DEPENDENCY_UNAVAILABLE
        assert primary.app_name == "myapp"
        assert err.details[1] == {"status": None, "checks": [], "attempt": 1}


class TestResolutionTimeoutIsOnlyExhaustionWhenTheClockAgrees:
    """``asyncio.TimeoutError`` is the builtin ``TimeoutError``; a socket raises it too."""

    async def test_a_quick_socket_timeout_is_not_reported_as_budget_exhaustion(
        self,
    ) -> None:
        gate = _gate(_RecordingHandler(), mode=PreflightGateMode.HARD, budget=30)
        with (
            _patched_resolution(raises=TimeoutError("socket read timed out")),
            mock.patch(f"{_GATE}.logger"),
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        message = _primary_details(excinfo.value).message
        assert "consumed the entire preflight budget" not in message
        assert "socket read timed out" in message


class TestHandlerIsCancelledAtTheAdvertisedBudget:
    """The deadline the gate enforces is the number it hands the handler.

    With storage verification opted in, a reserve is carved out of the
    advertised ``timeout_seconds``. A handler that sizes its probes to that
    field must be cancelled at that field, or the reserve is advisory and the
    timeout message blames the handler for a deadline it was never given.
    """

    async def test_storage_reserve_is_enforced_not_just_advertised(self) -> None:
        handler = _SlowHandler(3.0)
        gate = build_preflight_gate_activity(
            handler,
            app_name="myapp",
            mode=PreflightGateMode.SOFT,
            budget_seconds=5,
            verify_storage=True,
        )
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            started = asyncio.get_running_loop().time()
            result = await gate(PreflightGateInput())
            elapsed = asyncio.get_running_loop().time() - started
        assert result.status is PreflightStatus.NOT_READY
        assert handler.completed is False
        assert elapsed < 2.9
        row = _outcome(mock_logger)
        assert row["outcome"] == "would_block"
        assert "2s budget" in result.checks[0].resolved_message


class TestADeadAttemptNeverBlocks:
    """An attempt Temporal has abandoned emits no row and raises no block.

    Its result is inert either way, and a block with no row behind it is a red
    run with no record of why.
    """

    async def test_not_ready_verdict_on_a_dead_attempt_returns_quietly(self) -> None:
        output = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="auth", passed=False, message="bad creds")],
        )
        gate = _gate(_ReturningHandler(output), mode=PreflightGateMode.HARD)
        with (
            mock.patch(f"{_GATE}._attempt_is_live", return_value=False),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        assert _no_outcome(mock_logger)

    async def test_handler_raise_on_a_dead_attempt_returns_quietly(self) -> None:
        gate = _gate(
            _RaisingHandler(AuthError(message="bad")), mode=PreflightGateMode.HARD
        )
        with (
            mock.patch(f"{_GATE}._attempt_is_live", return_value=False),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        assert _no_outcome(mock_logger)


class TestAnAttemptThatStoppedBeatingIsDead:
    """An attempt silent for longer than its heartbeat timeout has been timed out.

    Reproduced live: a worker frozen mid-attempt and resumed saw its probe fail
    in the first second back, before the heartbeat loop could learn from the
    server that the attempt was gone, and emitted a second ``blocked`` row next
    to the workflow's ``frame_lost`` row. The last send time is a local
    monotonic reading, so no clocks have to agree.
    """

    @staticmethod
    def _info(heartbeat_timeout: float | None) -> mock.MagicMock:
        info = mock.MagicMock()
        info.attempt = 1
        info.started_time = None
        info.start_to_close_timeout = timedelta(seconds=30)
        info.heartbeat_timeout = (
            timedelta(seconds=heartbeat_timeout) if heartbeat_timeout else None
        )
        return info

    def test_overdue_heartbeat_means_dead(self) -> None:
        from application_sdk.execution._temporal.preflight_gate import (
            _attempt_is_live,
            _Beats,
        )

        beats = _Beats()
        beats.last_sent -= 5.0
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=self._info(1.0)),
            mock.patch(f"{_GATE}.activity.is_cancelled", return_value=False),
        ):
            assert _attempt_is_live(beats) is False

    def test_recent_heartbeat_means_live(self) -> None:
        from application_sdk.execution._temporal.preflight_gate import (
            _attempt_is_live,
            _Beats,
        )

        with (
            mock.patch(f"{_GATE}.activity.info", return_value=self._info(60.0)),
            mock.patch(f"{_GATE}.activity.is_cancelled", return_value=False),
        ):
            assert _attempt_is_live(_Beats()) is True

    def test_no_heartbeat_timeout_declared_stays_live(self) -> None:
        from application_sdk.execution._temporal.preflight_gate import (
            _attempt_is_live,
            _Beats,
        )

        beats = _Beats()
        beats.last_sent -= 500.0
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=self._info(None)),
            mock.patch(f"{_GATE}.activity.is_cancelled", return_value=False),
        ):
            assert _attempt_is_live(beats) is True

    async def test_a_silent_attempt_emits_no_row_and_raises_no_block(self) -> None:
        output = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="auth", passed=False, message="bad creds")],
        )
        handler = _ReturningHandler(output)

        async def _slow_return(input):
            await asyncio.sleep(0.3)
            return output

        handler.preflight_check = _slow_return
        gate = _gate(handler, mode=PreflightGateMode.HARD, budget=5)
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=self._info(0.1)),
            mock.patch(f"{_GATE}.activity.is_cancelled", return_value=False),
            mock.patch(f"{_GATE}.gate_heartbeat_timings", return_value=(60.0, 600.0)),
            mock.patch(f"{_GATE}.logger") as mock_logger,
        ):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        assert _no_outcome(mock_logger)


class TestEveryGateErrorCarriesTheAttempt:
    """``details[1].attempt`` on the block and the marker names the attempt that ran."""

    async def test_block_carries_the_current_attempt(self) -> None:
        gate = _gate(
            _RaisingHandler(AuthError(message="bad")), mode=PreflightGateMode.HARD
        )
        info = mock.MagicMock()
        info.attempt = 2
        info.start_to_close_timeout = timedelta(seconds=30)
        with (
            mock.patch(f"{_GATE}.activity.info", return_value=info),
            mock.patch(f"{_GATE}.logger"),
        ):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value.details[1]["attempt"] == 2

    async def test_block_outside_an_activity_reports_attempt_one(self) -> None:
        gate = _gate(
            _RaisingHandler(AuthError(message="bad")), mode=PreflightGateMode.HARD
        )
        with mock.patch(f"{_GATE}.logger"):
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
        assert excinfo.value.details[1]["attempt"] == 1


class TestOutcomeLevel:
    """One level policy for both frames (FND-901)."""

    _failed = [PreflightCheck(name="x", passed=False, message="no")]
    _passed = [PreflightCheck(name="x", passed=True)]

    @pytest.mark.parametrize(
        ("outcome", "classification", "checks", "expected"),
        [
            (PreflightRowOutcome.BLOCKED, PreflightClassification.VERDICT, [], "error"),
            (
                PreflightRowOutcome.NO_VERDICT,
                PreflightClassification.GATE_BROKEN,
                [],
                "error",
            ),
            (
                PreflightRowOutcome.WOULD_BLOCK,
                PreflightClassification.SOURCE_UNVERIFIABLE,
                [],
                "error",
            ),
            (
                PreflightRowOutcome.WOULD_BLOCK,
                PreflightClassification.FRAME_LOST,
                [],
                "error",
            ),
            (
                PreflightRowOutcome.WOULD_BLOCK,
                PreflightClassification.VERDICT,
                [],
                "info",
            ),
            (
                PreflightRowOutcome.PROCEEDED,
                PreflightClassification.VERDICT,
                _failed,
                "warning",
            ),
            (
                PreflightRowOutcome.PROCEEDED,
                PreflightClassification.VERDICT,
                _passed,
                "info",
            ),
            (PreflightRowOutcome.SKIPPED, PreflightClassification.NOT_RUN, [], "info"),
        ],
    )
    def test_level(self, outcome, classification, checks, expected) -> None:
        assert gate_outcome_level(outcome, classification, checks) == expected


class TestTheRowShapeIsOne:
    """The activity's row carries exactly the keys the shared builder declares."""

    async def test_activity_row_has_every_declared_key(self) -> None:
        output = PreflightOutput(status=PreflightStatus.READY, checks=[])
        gate = _gate(_ReturningHandler(output), mode=PreflightGateMode.SOFT)
        with mock.patch(f"{_GATE}.logger") as mock_logger:
            await gate(PreflightGateInput())
        row = _outcome(mock_logger)
        assert set(GATE_OUTCOME_ROW_KEYS) <= row.keys()
