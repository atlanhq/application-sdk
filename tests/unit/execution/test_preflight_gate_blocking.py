"""Hard mode blocks on the failure's category, not on the posture alone (FND-3040).

A hard gate blocks only on failures the customer can act on: deterministic ones,
and an unreachable source that is still unreachable on the gate's final attempt
(FND-3774). Everything else — transient, Atlan-side, or an app fault — is
reported as ``would_block`` and the run proceeds, exactly as in soft mode.
"""

from __future__ import annotations

from datetime import timedelta
from unittest import mock

import pytest

from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import (
    AppTimeoutError,
    AuthError,
    SourceUnavailableError,
)
from application_sdk.errors.wire import FailureDetails
from application_sdk.execution._temporal.preflight_gate import (
    DEPRECATED_FAIL_OPEN_CATEGORIES,
    GATE_BLOCKING_CATEGORIES,
    GATE_NEVER_BLOCKING_CATEGORIES,
    GATE_RETRIED_CATEGORIES,
    PREFLIGHT_FAILED_ERROR_TYPE,
    PREFLIGHT_NO_VERDICT_ERROR_TYPE,
    PreflightGateInput,
    build_preflight_gate_activity,
    gate_blocks,
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

_ALL_CATEGORIES = list(FailureCategory)


def _category_id(category: FailureCategory) -> str:
    return category.value


def _details(category: FailureCategory, code: str = "SOURCE_CHECK") -> FailureDetails:
    return FailureDetails(
        category=category, code=code, retryable=False, message=f"{code} failed"
    )


class _ReturningHandler(DefaultHandler):
    """Returns a caller-supplied verdict."""

    def __init__(self, output: PreflightOutput) -> None:
        self._output = output

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        return self._output


class _RaisingHandler(DefaultHandler):
    """Raises a caller-supplied error."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
        raise self._error


def _hard_gate(output: PreflightOutput):
    return build_preflight_gate_activity(
        _ReturningHandler(output),
        app_name="myapp",
        mode=PreflightGateMode.HARD,
        budget_seconds=5,
    )


_GATE = "application_sdk.execution._temporal.preflight_gate"


def _on_attempt(attempt: int):
    """Run inside a gate activity attempt numbered ``attempt``."""
    info = mock.MagicMock()
    info.attempt = attempt
    info.start_to_close_timeout = timedelta(seconds=10)
    return mock.patch(f"{_GATE}.activity.info", return_value=info)


class TestThePolicy:
    def test_the_blocking_set_is_exactly_the_customer_actionable_categories(
        self,
    ) -> None:
        assert GATE_BLOCKING_CATEGORIES == {
            FailureCategory.AUTH,
            FailureCategory.PERMISSION,
            FailureCategory.INVALID_INPUT,
            FailureCategory.PRECONDITION,
            FailureCategory.NOT_FOUND,
            FailureCategory.SOURCE_UNAVAILABLE,
        }

    def test_the_never_blocking_set_is_the_fail_open_train_plus_timeout(
        self,
    ) -> None:
        assert GATE_NEVER_BLOCKING_CATEGORIES == DEPRECATED_FAIL_OPEN_CATEGORIES | {
            FailureCategory.TIMEOUT,
        }

    def test_only_an_unreachable_source_is_retried_before_it_blocks(self) -> None:
        assert GATE_RETRIED_CATEGORIES == {FailureCategory.SOURCE_UNAVAILABLE}
        assert GATE_RETRIED_CATEGORIES <= GATE_BLOCKING_CATEGORIES

    def test_the_two_sets_never_overlap(self) -> None:
        assert not GATE_BLOCKING_CATEGORIES & GATE_NEVER_BLOCKING_CATEGORIES

    @pytest.mark.parametrize("category", _ALL_CATEGORIES, ids=_category_id)
    def test_soft_never_blocks(self, category: FailureCategory) -> None:
        assert not gate_blocks(PreflightGateMode.SOFT, category)

    @pytest.mark.parametrize("category", _ALL_CATEGORIES, ids=_category_id)
    def test_hard_blocks_only_on_the_blocking_set(
        self, category: FailureCategory
    ) -> None:
        assert gate_blocks(PreflightGateMode.HARD, category) is (
            category in GATE_BLOCKING_CATEGORIES
        )

    @pytest.mark.parametrize(
        "category", sorted(GATE_NEVER_BLOCKING_CATEGORIES, key=_category_id)
    )
    def test_hard_never_blocks_on_the_never_blocking_set(
        self, category: FailureCategory
    ) -> None:
        assert not gate_blocks(PreflightGateMode.HARD, category)


class TestAHardVerdictBlocksOnItsCategory:
    @pytest.mark.parametrize("category", _ALL_CATEGORIES, ids=_category_id)
    async def test_a_not_ready_verdict_blocks_only_on_a_blocking_category(
        self, category: FailureCategory, capture_preflight_outcomes
    ) -> None:
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="source", passed=False, error=_details(category))
            ],
        )
        gate = _hard_gate(verdict)
        if category in GATE_BLOCKING_CATEGORIES:
            with pytest.raises(ApplicationError) as excinfo:
                await gate(PreflightGateInput())
            assert excinfo.value.type == PREFLIGHT_FAILED_ERROR_TYPE
            expected = "blocked"
        else:
            result = await gate(PreflightGateInput())
            assert result.status is PreflightStatus.NOT_READY
            expected = "would_block"
        row = capture_preflight_outcomes.one
        assert row["outcome"] == expected
        assert row["reason"] == "SOURCE_CHECK"

    async def test_an_untyped_not_ready_verdict_still_blocks(
        self, capture_preflight_outcomes
    ) -> None:
        """No typed error falls back to PRECONDITION, so an un-migrated handler's
        NOT_READY keeps blocking a hard gate."""
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[PreflightCheck(name="source", passed=False, message="no grant")],
        )
        with pytest.raises(ApplicationError):
            await _hard_gate(verdict)(PreflightGateInput())
        assert capture_preflight_outcomes.one["outcome"] == "blocked"

    # The decision follows details[0], the failure the row's reason names. A
    # handler-pinned aggregate is that failure whatever the rows say; with no
    # aggregate the most decisive row is (FND-3774).
    _UNREACHABLE = SourceUnavailableError(message="warehouse paused")
    _BAD_PASSWORD = AuthError(message="bad password")
    _OVERRUN = AppTimeoutError(message="probe overran")
    _CHECKS = [
        PreflightCheck(name="reach", passed=False, error=_UNREACHABLE),
        PreflightCheck(name="login", passed=False, error=_BAD_PASSWORD),
    ]

    async def test_an_unreachable_aggregate_blocks_on_the_final_attempt(
        self, capture_preflight_outcomes
    ) -> None:
        blocks = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            error=self._UNREACHABLE,
            checks=self._CHECKS,
        )
        with pytest.raises(ApplicationError) as excinfo:
            await _hard_gate(blocks)(PreflightGateInput())
        assert excinfo.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        row = capture_preflight_outcomes.one
        assert row["outcome"] == "blocked"
        assert row["reason"] == self._UNREACHABLE.code

    async def test_an_auth_aggregate_blocks_despite_an_earlier_unreachable_row(
        self, capture_preflight_outcomes
    ) -> None:
        blocks = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            error=self._BAD_PASSWORD,
            checks=self._CHECKS,
        )
        with pytest.raises(ApplicationError):
            await _hard_gate(blocks)(PreflightGateInput())
        assert capture_preflight_outcomes.one["outcome"] == "blocked"

    @pytest.mark.parametrize(
        "ahead", [_UNREACHABLE, _OVERRUN], ids=["unreachable", "timeout"]
    )
    async def test_with_no_aggregate_an_auth_row_outranks_an_earlier_row(
        self, ahead, capture_preflight_outcomes
    ) -> None:
        """An AUTH failure blocks on the first attempt, and names AUTH, even when
        an unreachable or timed-out row is listed ahead of it."""
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="reach", passed=False, error=ahead),
                PreflightCheck(name="login", passed=False, error=self._BAD_PASSWORD),
            ],
        )
        with _on_attempt(1), pytest.raises(ApplicationError) as excinfo:
            await _hard_gate(verdict)(PreflightGateInput())
        assert excinfo.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        row = capture_preflight_outcomes.one
        assert row["outcome"] == "blocked"
        assert row["reason"] == self._BAD_PASSWORD.code

    async def test_with_no_aggregate_an_unreachable_row_outranks_a_timeout(
        self, capture_preflight_outcomes
    ) -> None:
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(name="slow", passed=False, error=self._OVERRUN),
                PreflightCheck(name="reach", passed=False, error=self._UNREACHABLE),
            ],
        )
        with pytest.raises(ApplicationError):
            await _hard_gate(verdict)(PreflightGateInput())
        assert capture_preflight_outcomes.one["reason"] == self._UNREACHABLE.code


_UNREACHABLE_VERDICT = PreflightOutput(
    status=PreflightStatus.NOT_READY,
    checks=[
        PreflightCheck(
            name="reach",
            passed=False,
            error=SourceUnavailableError(message="private link down"),
        )
    ],
)


def _gate_for(handler: DefaultHandler, mode: PreflightGateMode):
    return build_preflight_gate_activity(
        handler, app_name="myapp", mode=mode, budget_seconds=5, attempts=2
    )


class TestAnUnreachableSourceBlocksOnlyOnTheFinalAttempt:
    """SOURCE_UNAVAILABLE gets the gate's retry before it blocks (FND-3774).

    A non-final attempt raises the retryable ``PreflightNoVerdict`` marker with
    the full payload and emits no row; the retried attempt's verdict is the one
    that counts. Covers a returned verdict and a raised error alike.
    """

    _HANDLERS = {
        "returned": lambda: _ReturningHandler(_UNREACHABLE_VERDICT),
        "raised": lambda: _RaisingHandler(
            SourceUnavailableError(message="private link down")
        ),
    }

    @pytest.mark.parametrize("shape", sorted(_HANDLERS))
    async def test_hard_defers_a_non_final_attempt_to_the_retry(
        self, shape: str, capture_preflight_outcomes
    ) -> None:
        gate = _gate_for(self._HANDLERS[shape](), PreflightGateMode.HARD)
        with _on_attempt(1), pytest.raises(ApplicationError) as excinfo:
            await gate(PreflightGateInput())
        assert excinfo.value.type == PREFLIGHT_NO_VERDICT_ERROR_TYPE
        assert excinfo.value.non_retryable is False
        primary = excinfo.value.details[0]
        assert primary.category is FailureCategory.SOURCE_UNAVAILABLE
        assert excinfo.value.details[1]["attempt"] == 1
        assert capture_preflight_outcomes.rows == []

    @pytest.mark.parametrize("shape", sorted(_HANDLERS))
    async def test_hard_blocks_on_the_final_attempt(
        self, shape: str, capture_preflight_outcomes
    ) -> None:
        gate = _gate_for(self._HANDLERS[shape](), PreflightGateMode.HARD)
        with _on_attempt(2), pytest.raises(ApplicationError) as excinfo:
            await gate(PreflightGateInput())
        assert excinfo.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        row = capture_preflight_outcomes.one
        assert row["outcome"] == "blocked"
        assert row["gate_attempt"] == 2

    async def test_a_blip_that_clears_on_the_retry_proceeds(
        self, capture_preflight_outcomes
    ) -> None:
        verdicts = iter(
            [_UNREACHABLE_VERDICT, PreflightOutput(status=PreflightStatus.READY)]
        )

        class _Flapping(DefaultHandler):
            async def preflight_check(self, input: PreflightInput) -> PreflightOutput:
                return next(verdicts)

        gate = _gate_for(_Flapping(), PreflightGateMode.HARD)
        with _on_attempt(1), pytest.raises(ApplicationError) as excinfo:
            await gate(PreflightGateInput())
        assert excinfo.value.type == PREFLIGHT_NO_VERDICT_ERROR_TYPE
        with _on_attempt(2):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.READY
        row = capture_preflight_outcomes.one
        assert row["outcome"] == "proceeded"
        assert row["gate_attempt"] == 2

    @pytest.mark.parametrize("shape", sorted(_HANDLERS))
    async def test_soft_reports_on_the_first_attempt_without_a_retry(
        self, shape: str, capture_preflight_outcomes
    ) -> None:
        gate = _gate_for(self._HANDLERS[shape](), PreflightGateMode.SOFT)
        with _on_attempt(1):
            result = await gate(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        row = capture_preflight_outcomes.one
        assert row["outcome"] == "would_block"
        assert row["gate_attempt"] == 1

    async def test_a_deterministic_block_is_not_deferred(
        self, capture_preflight_outcomes
    ) -> None:
        verdict = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            checks=[
                PreflightCheck(
                    name="login", passed=False, error=AuthError(message="bad password")
                )
            ],
        )
        gate = _gate_for(_ReturningHandler(verdict), PreflightGateMode.HARD)
        with _on_attempt(1), pytest.raises(ApplicationError) as excinfo:
            await gate(PreflightGateInput())
        assert excinfo.value.type == PREFLIGHT_FAILED_ERROR_TYPE
        assert capture_preflight_outcomes.one["outcome"] == "blocked"
