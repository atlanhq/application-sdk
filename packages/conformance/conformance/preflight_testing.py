"""Preflight scenario registration and assertions for app-owned source adapters.

Load with ``pytest -p conformance.preflight_testing``, which registers the
``preflight_conformance`` marker. Mark real-handler tests with
``preflight_conformance(rule="F016", scenario="healthy", entrypoint="default")``
and check the observed behaviour with ``assert_preflight_result`` (plus
``assert_probe_lifetime`` for the lifetime scenarios).

Conformance F016 reads those registrations statically: it checks the matrix
is *defined*. Whether the tests *pass* is the test gate's measure; nothing in
the conformance suite executes them.
"""

from __future__ import annotations

import json
import math
import warnings
from typing import Any

from conformance.preflight_scenarios import SCENARIOS

__all__ = [
    "SCENARIOS",
    "assert_extraction_scheduled",
    "assert_preflight_exit",
    "assert_preflight_result",
    "assert_probe_lifetime",
]


def _deprecated(name: str, rule: str) -> None:
    warnings.warn(
        f"{name} served the retired conformance rule {rule} and is removed in "
        "v0.40.0; assert gate behaviour in the SDK's own tests instead.",
        DeprecationWarning,
        stacklevel=3,
    )


def assert_preflight_result(
    result: Any,
    *,
    required_checks: set[str],
    observed_checks: set[str],
    expected_status: str,
    synthetic_secrets: tuple[str, ...] = (),
    captured_logs: str = "",
    mandatory_order: tuple[str, ...] = (),
    expected_errors: dict[str, dict[str, Any]] | None = None,
) -> None:
    """Validate a real handler result against adapter-supplied probe evidence."""
    from application_sdk.errors.wire import FailureDetails
    from application_sdk.handler.contracts import PreflightOutput

    assert isinstance(result, PreflightOutput), "Handler must return PreflightOutput"
    checks = {check.name: check for check in result.checks}
    assert len(checks) == len(result.checks), "Check names must be unique"
    assert set(checks) <= observed_checks, "Result contains an unobserved probe"
    failed = [check for check in result.checks if not check.passed]
    for check in failed:
        assert isinstance(check.error, FailureDetails), "Failed check lacks typed error"
        assert check.error.code.strip(), "Failure code must be nonblank"
        assert check.error.message.strip(), "Failure message must be nonblank"
        assert (
            check.error.suggested_action or ""
        ).strip(), "Failure needs a next action"
    assert all(
        check.error is None for check in result.checks if check.passed
    ), "Passed check carries failure"
    required_failed = [check for check in failed if check.name in required_checks]
    if not required_failed:
        assert required_checks <= set(checks), "Required probe evidence is missing"
    if mandatory_order and required_failed:
        failed_index = mandatory_order.index(required_failed[0].name)
        assert set(mandatory_order[:failed_index]) <= set(
            checks
        ), "Earlier mandatory probes are missing"
        assert not set(mandatory_order[failed_index + 1 :]) & set(
            checks
        ), "Mandatory probes did not short-circuit"
    allowed = {"not_ready"} if required_failed else {"ready", "partial"}
    assert (
        result.status.value == expected_status
    ), "Verdict differs from the scenario's expected status"
    assert expected_status in allowed, "Verdict contradicts scenario roles"
    if result.status.value == "partial":
        assert failed, "PARTIAL verdict with no failed check"
    if result.error is not None:
        assert any(
            check.error == result.error for check in required_failed
        ), "Aggregate error does not match a blocker"
    wire = result.model_dump_json() + captured_logs
    assert all(
        secret not in wire for secret in synthetic_secrets if secret
    ), "Synthetic secret exposed"
    for name, fields in (expected_errors or {}).items():
        assert (
            name in checks and checks[name].error is not None
        ), "Expected typed failure is missing"
        details = checks[name].error
        assert details is not None
        error = details.model_dump(mode="json")
        assert all(
            error.get(key) == value for key, value in fields.items()
        ), "Failure attribution differs from the source scenario"


def assert_probe_lifetime(
    *, elapsed: float, budget: float, background_stopped: bool, tolerance: float = 0.1
) -> None:
    """Assert elapsed time and independent teardown evidence from the source adapter."""
    assert all(
        math.isfinite(value) and value >= 0 for value in (elapsed, budget, tolerance)
    )
    assert elapsed <= budget + tolerance, "Probe exceeded its remaining deadline"
    assert background_stopped, "Cancelled probe left background work running"


def assert_preflight_exit(
    payload: dict[str, Any],
    *,
    status: str | None,
    synthetic_secrets: tuple[str, ...] = (),
) -> None:
    """Validate a decoded gate exit containing status, checks, and typed error.

    Deprecated: served the retired F018 rule; removed in v0.40.0.
    """
    _deprecated("assert_preflight_exit", "F018")
    from application_sdk.errors.wire import FailureDetails

    assert payload.get("status", "missing") == status, "Exit lost its verdict state"
    assert isinstance(payload.get("checks"), list), "Exit lost its check list"
    if status in {None, "not_ready"}:
        error = FailureDetails.model_validate(payload.get("error"))
        assert error.code.strip() and error.message.strip()
        assert (
            error.suggested_action or ""
        ).strip(), "Exit needs an audience-appropriate action"
    wire = json.dumps(payload, default=str)
    assert all(
        secret not in wire for secret in synthetic_secrets if secret
    ), "Synthetic secret exposed"


def assert_extraction_scheduled(
    history: Any,
    activity_name: str,
    *,
    expected: int,
    gate_activity_name: str,
    expected_terminal: str,
    expected_failure_type: str | None = None,
) -> None:
    """Inspect a Temporal WorkflowHistory, not a mocked execute_activity call.

    Deprecated: served the retired F017 rule; removed in v0.40.0.
    """
    _deprecated("assert_extraction_scheduled", "F017")
    from temporalio.client import WorkflowHistory

    assert isinstance(
        history, WorkflowHistory
    ), "Provide fetched Temporal WorkflowHistory"
    assert history.events, "Empty history is not workflow execution evidence"
    count = sum(
        event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name
        == activity_name
        for event in history.events
    )
    assert count == expected, "Extraction scheduling count violates gate contract"
    assert any(
        event.HasField("activity_task_scheduled_event_attributes")
        and event.activity_task_scheduled_event_attributes.activity_type.name
        == gate_activity_name
        for event in history.events
    ), "History does not contain the preflight gate"
    assert expected_terminal in {"failed", "completed", "canceled"}
    field = f"workflow_execution_{expected_terminal}_event_attributes"
    terminal = [event for event in history.events if event.HasField(field)]
    assert len(terminal) == 1, "Workflow terminal outcome differs from the scenario"
    if expected_terminal == "failed":
        assert expected_failure_type, "Specify the expected typed workflow failure"
        failure = terminal[0].workflow_execution_failed_event_attributes.failure
        types = set()
        while True:
            if failure.HasField("application_failure_info"):
                types.add(failure.application_failure_info.type)
            if not failure.HasField("cause"):
                break
            failure = failure.cause
        assert expected_failure_type in types, "Workflow failed for an unrelated reason"


def pytest_addoption(parser: Any) -> None:
    # Deprecated no-ops, kept for one release so a test command that still
    # passes them keeps parsing. They produced a report the conformance suite
    # graded; the suite no longer reads test results. Removed in v0.40.0.
    parser.addoption("--preflight-report", help="Deprecated no-op; removed in v0.40.0.")
    parser.addoption("--preflight-rules", help="Deprecated no-op; removed in v0.40.0.")


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "preflight_conformance(rule, scenario, entrypoint): registered F016 preflight scenario",
    )
    for option in ("--preflight-report", "--preflight-rules"):
        if config.getoption(option):
            warnings.warn(
                f"{option} is a deprecated no-op and is removed in v0.40.0: "
                "conformance F016 reads scenario registrations statically.",
                DeprecationWarning,
                stacklevel=1,
            )
