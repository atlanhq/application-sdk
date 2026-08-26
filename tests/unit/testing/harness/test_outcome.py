"""Unit tests for the two adapters on the outcome vocabulary.

``assert_settled`` converts a verdict back to the raise-on-failure style the
connector suites already use; ``grade`` converts a pile of verdicts and findings
forward into the one answer a scenario reports. The vocabulary's own dataclasses
are covered in ``test_scaffold.py`` — what is pinned here is the two mappings,
because both encode a decision that is easy to get subtly wrong: which category
a failing wait belongs to, and which of two bad answers outranks the other.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from application_sdk.errors.categories import FailureCategory
from application_sdk.testing.harness import (
    Expired,
    Indeterminate,
    NeverStarted,
    Settled,
    Stalled,
    Verdict,
    WaitExpiredError,
    WaitIndeterminateError,
    WaitNeverStartedError,
    WaitStalledError,
    assert_settled,
    grade,
)
from application_sdk.testing.harness.expectations import UNREADABLE, Finding

_LABEL = "AE run 8412 native status"
_ATTEMPTS = 7


def _finding(expectation: str) -> Finding:
    return Finding(
        subject="Table", detail="expected >= 1, saw 0", expectation=expectation
    )


# ---------------------------------------------------------------------------
# assert_settled
# ---------------------------------------------------------------------------


def test_a_settled_wait_unwraps_to_its_value() -> None:
    outcome = Settled(
        label=_LABEL,
        attempts=_ATTEMPTS,
        elapsed=timedelta(seconds=12),
        value={"extract": "ok"},
    )
    assert assert_settled(outcome) == {"extract": "ok"}


def test_never_started_raises_a_precondition_not_a_timeout() -> None:
    """The budget did not run out — the state that had to exist first never did,
    which is the category's own litmus test and what both e2e leaves guarding
    this moment already choose."""
    outcome = NeverStarted(
        label=_LABEL,
        attempts=_ATTEMPTS,
        elapsed=timedelta(seconds=180),
        grace=timedelta(seconds=180),
    )
    with pytest.raises(WaitNeverStartedError) as caught:
        assert_settled(outcome)
    error = caught.value
    assert error.category is FailureCategory.PRECONDITION
    assert error.code == "PRECONDITION_WAIT_NEVER_STARTED"
    assert error.grace_seconds == 180.0
    assert error.attempts == _ATTEMPTS
    assert error.label == _LABEL
    assert "AE run 8412 native status" in error.message


def test_stalled_raises_carrying_the_fingerprint_that_froze() -> None:
    """The single most useful field in the report, because it says *what* froze —
    so it is a field, not a substring of a rendered sentence."""
    outcome = Stalled(
        label=_LABEL,
        attempts=_ATTEMPTS,
        elapsed=timedelta(seconds=420),
        stall_window=timedelta(seconds=300),
        fingerprint="✓✓·-",
    )
    with pytest.raises(WaitStalledError) as caught:
        assert_settled(outcome)
    error = caught.value
    assert error.category is FailureCategory.TIMEOUT
    assert error.code == "TIMEOUT_WAIT_STALLED"
    assert error.fingerprint == "✓✓·-"
    assert error.stall_window_seconds == 300.0
    assert error.elapsed_seconds == 420.0


def test_expired_raises_the_plain_timeout_with_both_durations() -> None:
    outcome = Expired(
        label=_LABEL,
        attempts=_ATTEMPTS,
        elapsed=timedelta(seconds=600),
        budget=timedelta(seconds=600),
    )
    with pytest.raises(WaitExpiredError) as caught:
        assert_settled(outcome)
    error = caught.value
    assert error.code == "TIMEOUT_WAIT_EXPIRED"
    assert error.timeout_seconds == 600.0
    assert error.elapsed_seconds == 600.0


def test_indeterminate_raises_a_dependency_failure_chained_from_its_cause() -> None:
    """Load-bearing, not a category of convenience: a caller grading a suite has
    to be able to separate "could not tell" from "told, and it was bad" without
    parsing a message, and an expired vcluster token must not read as a
    regression in the thing under test."""
    cause = ConnectionResetError("tunnel died")
    outcome = Indeterminate(
        label=_LABEL,
        attempts=_ATTEMPTS,
        elapsed=timedelta(seconds=95),
        cause=cause,
        transient_failures=5,
    )
    with pytest.raises(WaitIndeterminateError) as caught:
        assert_settled(outcome)
    error = caught.value
    assert error.category is FailureCategory.DEPENDENCY_UNAVAILABLE
    assert error.code == "DEPENDENCY_UNAVAILABLE_WAIT_INDETERMINATE"
    assert error.transient_failures == 5
    assert error.__cause__ is cause, "the original transport error stays reachable"


def test_every_failing_verdict_maps_to_a_distinct_code() -> None:
    """Four verdicts collapsing to three codes would make one of them
    untriageable from the code column alone."""
    codes = {
        WaitNeverStartedError.code,
        WaitStalledError.code,
        WaitExpiredError.code,
        WaitIndeterminateError.code,
    }
    assert len(codes) == 4


# ---------------------------------------------------------------------------
# grade — the precondition gate
# ---------------------------------------------------------------------------


def _settled() -> Settled[str]:
    return Settled(
        label=_LABEL, attempts=_ATTEMPTS, elapsed=timedelta(seconds=1), value="ok"
    )


def _indeterminate() -> Indeterminate[str]:
    return Indeterminate(
        label=_LABEL,
        attempts=_ATTEMPTS,
        elapsed=timedelta(seconds=1),
        cause=ConnectionResetError("x"),
    )


def _expired() -> Expired[str]:
    return Expired(
        label=_LABEL,
        attempts=_ATTEMPTS,
        elapsed=timedelta(seconds=1),
        budget=timedelta(seconds=1),
    )


def test_nothing_observed_grades_as_passed() -> None:
    """A scenario that declared no expectations and performed no waits has
    nothing to be indeterminate about."""
    assert grade() is Verdict.PASSED


def test_everything_settled_and_no_findings_grades_as_passed() -> None:
    assert grade(outcomes=[_settled(), _settled()]) is Verdict.PASSED


def test_a_finding_from_a_read_that_worked_grades_as_failed() -> None:
    assert grade(findings=[_finding("floor")]) is Verdict.FAILED


def test_a_wait_that_did_not_settle_grades_as_failed() -> None:
    assert grade(outcomes=[_settled(), _expired()]) is Verdict.FAILED


def test_an_unreadable_finding_blocks_a_pass_without_claiming_a_failure() -> None:
    """Silence from a search that errored is not evidence, in either direction —
    the C4 fix's consumer side."""
    assert grade(findings=[_finding(UNREADABLE)]) is Verdict.INDETERMINATE


def test_an_indeterminate_wait_blocks_a_pass_the_same_way() -> None:
    assert grade(outcomes=[_settled(), _indeterminate()]) is Verdict.INDETERMINATE


@pytest.mark.parametrize(
    "findings",
    [
        [_finding(UNREADABLE), _finding("floor")],
        [_finding("floor"), _finding(UNREADABLE)],
    ],
    ids=["unreadable-first", "finding-first"],
)
def test_a_confirmed_regression_outranks_an_unrelated_unreadable(
    findings: list[Finding],
) -> None:
    """Order-independently. The gate exists to stop a *failed read* being
    graded, not to discard evidence that was actually gathered — one dropped
    tunnel elsewhere must not bury a real finding."""
    assert grade(findings=findings) is Verdict.FAILED


def test_a_failed_wait_outranks_an_indeterminate_one() -> None:
    assert grade(outcomes=[_indeterminate(), _expired()]) is Verdict.FAILED


def test_a_real_finding_outranks_an_indeterminate_wait() -> None:
    """The two halves are graded against each other, not each in its own silo."""
    assert (
        grade(outcomes=[_indeterminate()], findings=[_finding("exact")])
        is Verdict.FAILED
    )


def test_the_verdict_reads_as_a_plain_string() -> None:
    """A scenario suite writes this into a report file it did not import an enum
    to read back."""
    assert Verdict.INDETERMINATE == "indeterminate"
