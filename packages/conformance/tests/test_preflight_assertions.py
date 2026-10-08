import pytest
from conformance.preflight_testing import assert_preflight_result, assert_probe_lifetime

from application_sdk.errors import AuthError
from application_sdk.handler.contracts import (
    PreflightCheck,
    PreflightOutput,
    PreflightStatus,
)


def result(action="Check the configured credentials."):
    error = AuthError(
        message="Authentication failed", suggested_action=action
    ).to_failure_details()
    return PreflightOutput(
        status=PreflightStatus.NOT_READY,
        checks=[PreflightCheck(name="connection", passed=False, error=error)],
    )


def test_typed_mandatory_failure():
    assert_preflight_result(
        result(),
        required_checks={"connection"},
        observed_checks={"connection"},
        expected_status="not_ready",
    )


def test_missing_action_rejected():
    with pytest.raises(AssertionError, match="next action"):
        assert_preflight_result(
            result(None),
            required_checks={"connection"},
            observed_checks={"connection"},
            expected_status="not_ready",
        )


def test_unobserved_probe_rejected():
    with pytest.raises(AssertionError, match="unobserved"):
        assert_preflight_result(
            result(),
            required_checks={"connection"},
            observed_checks=set(),
            expected_status="not_ready",
        )


def test_secret_in_log_rejected():
    with pytest.raises(AssertionError, match="Synthetic secret"):
        assert_preflight_result(
            result(),
            required_checks={"connection"},
            observed_checks={"connection"},
            expected_status="not_ready",
            synthetic_secrets=("synthetic-secret",),
            captured_logs="synthetic-secret",
        )


@pytest.mark.parametrize("elapsed, stopped", [(2, True), (0.5, False)])
def test_lifetime_violation(elapsed, stopped):
    with pytest.raises(AssertionError):
        assert_probe_lifetime(elapsed=elapsed, budget=1, background_stopped=stopped)


def test_partial_result_with_only_advisory_failures_is_accepted():
    output = result()
    output.status = PreflightStatus.PARTIAL
    assert_preflight_result(
        output,
        required_checks=set(),
        observed_checks={"connection"},
        expected_status="partial",
    )


def test_partial_result_hiding_a_mandatory_failure_is_rejected():
    output = result()
    output.status = PreflightStatus.PARTIAL
    with pytest.raises(AssertionError, match="contradicts scenario roles"):
        assert_preflight_result(
            output,
            required_checks={"connection"},
            observed_checks={"connection"},
            expected_status="partial",
        )


def test_partial_result_without_a_failed_check_is_rejected():
    output = PreflightOutput(
        status=PreflightStatus.PARTIAL,
        checks=[PreflightCheck(name="connection", passed=True)],
    )
    with pytest.raises(AssertionError, match="no failed check"):
        assert_preflight_result(
            output,
            required_checks=set(),
            observed_checks={"connection"},
            expected_status="partial",
        )
