import pytest
from conformance.preflight_testing import (
    assert_extraction_scheduled,
    assert_preflight_result,
    assert_probe_lifetime,
)

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


def test_unrelated_workflow_failure_rejected():
    from temporalio.api.common.v1 import ActivityType
    from temporalio.api.failure.v1 import ApplicationFailureInfo, Failure
    from temporalio.api.history.v1 import (
        ActivityTaskScheduledEventAttributes,
        HistoryEvent,
        WorkflowExecutionFailedEventAttributes,
    )
    from temporalio.client import WorkflowHistory

    history = WorkflowHistory(
        "synthetic-workflow",
        [
            HistoryEvent(
                activity_task_scheduled_event_attributes=ActivityTaskScheduledEventAttributes(
                    activity_type=ActivityType(name="example:preflight")
                )
            ),
            HistoryEvent(
                workflow_execution_failed_event_attributes=WorkflowExecutionFailedEventAttributes(
                    failure=Failure(
                        application_failure_info=ApplicationFailureInfo(
                            type="UnrelatedError"
                        )
                    )
                )
            ),
        ],
    )
    with pytest.raises(AssertionError, match="unrelated reason"):
        assert_extraction_scheduled(
            history,
            "extract",
            expected=0,
            gate_activity_name="example:preflight",
            expected_terminal="failed",
            expected_failure_type="PreflightFailedError",
        )
