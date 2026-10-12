"""Hard mode blocks on the failure's category, not on the posture alone (FND-3040).

A hard gate blocks only on failures that are deterministic and the customer can
act on. Everything else — transient, Atlan-side, or an app fault — is reported
as ``would_block`` and the run proceeds, exactly as in soft mode.
"""

from __future__ import annotations

import pytest

from application_sdk.errors.categories import FailureCategory
from application_sdk.errors.leaves import AuthError, SourceUnavailableError
from application_sdk.errors.wire import FailureDetails
from application_sdk.execution._temporal.preflight_gate import (
    DEPRECATED_FAIL_OPEN_CATEGORIES,
    GATE_BLOCKING_CATEGORIES,
    GATE_NEVER_BLOCKING_CATEGORIES,
    PREFLIGHT_FAILED_ERROR_TYPE,
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


def _hard_gate(output: PreflightOutput):
    return build_preflight_gate_activity(
        _ReturningHandler(output),
        app_name="myapp",
        mode=PreflightGateMode.HARD,
        budget_seconds=5,
    )


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
        }

    def test_the_never_blocking_set_is_the_fail_open_train_plus_transients(
        self,
    ) -> None:
        assert GATE_NEVER_BLOCKING_CATEGORIES == DEPRECATED_FAIL_OPEN_CATEGORIES | {
            FailureCategory.TIMEOUT,
            FailureCategory.SOURCE_UNAVAILABLE,
        }

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

    # The decision follows details[0], the failure the row's reason names: an
    # unreachable source attributed by the handler does not block because
    # another failed row happens to be an auth failure, and vice versa.
    _UNREACHABLE = SourceUnavailableError(message="warehouse paused")
    _BAD_PASSWORD = AuthError(message="bad password")
    _CHECKS = [
        PreflightCheck(name="reach", passed=False, error=_UNREACHABLE),
        PreflightCheck(name="login", passed=False, error=_BAD_PASSWORD),
    ]

    async def test_an_unreachable_aggregate_proceeds_despite_a_later_auth_row(
        self, capture_preflight_outcomes
    ) -> None:
        proceeds = PreflightOutput(
            status=PreflightStatus.NOT_READY,
            error=self._UNREACHABLE,
            checks=self._CHECKS,
        )
        result = await _hard_gate(proceeds)(PreflightGateInput())
        assert result.status is PreflightStatus.NOT_READY
        assert capture_preflight_outcomes.one["outcome"] == "would_block"

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
