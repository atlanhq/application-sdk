"""Preflight scenario registration and assertions for app-owned source adapters.

Load with ``pytest -p conformance.preflight_testing``. Mark real-handler tests
with ``preflight_conformance(rule="P062", scenario="healthy", entrypoint="default")``.
The runner checks execution coverage; these assertions check observed behavior.
Registration alone cannot prove that a test uses the real handler.
"""

from __future__ import annotations

import json
import math
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import pytest

_ACTIVE: ContextVar[dict[str, Any] | None] = ContextVar(
    "preflight_scenario", default=None
)


def _record(kind: str) -> None:
    record = _ACTIVE.get()
    if record is not None:
        record.setdefault("assertions", []).append(kind)


SCENARIOS = {
    "P062": (
        "healthy",
        "mandatory_failure",
        "advisory_failure",
        "recoverable_transient",
        "persistent_failure",
        "mixed_resources",
        "extraction_fallback",
        "credential_entrypoint_shapes",
        "no_probe",
        "hung_probe",
        "cancellation_cleanup",
        "budget_retry",
        "typed_safe_output",
    ),
    "P063": (
        "ready_partial",
        "not_ready",
        "typed_handler_failures",
        "handler_crash",
        "awaitable_overrun",
        "cancellation_resistant_probe",
        "running_attempt_timeout",
        "evidence_serialization",
        "never_started",
        "credential_absence_outage",
        "credential_overrun",
        "storage_exception_verdict",
        "external_cancellation",
        "old_history_replay",
        "mode_attempt_agreement",
    ),
    "P064": (
        "http_success_failure",
        "sdr_dispatch",
        "activity_result_block",
        "retry_marker",
        "plumbing_no_verdict",
        "workflow_activity_death",
        "message_precedence",
        "legacy_compatibility",
        "outcome_schema",
        "advisory_codes_duration",
        "log_handoff_contexts",
        "attempt_selection",
    ),
}


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
    derived = "not_ready" if required_failed else "partial" if failed else "ready"
    assert (
        result.status.value == expected_status == derived
    ), "Verdict contradicts scenario roles"
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
    _record("result")


def assert_probe_lifetime(
    *, elapsed: float, budget: float, background_stopped: bool, tolerance: float = 0.1
) -> None:
    """Assert elapsed time and independent teardown evidence from the source adapter."""
    assert all(
        math.isfinite(value) and value >= 0 for value in (elapsed, budget, tolerance)
    )
    assert elapsed <= budget + tolerance, "Probe exceeded its remaining deadline"
    assert background_stopped, "Cancelled probe left background work running"
    _record("lifetime")


def assert_preflight_exit(
    payload: dict[str, Any],
    *,
    status: str | None,
    synthetic_secrets: tuple[str, ...] = (),
) -> None:
    """Validate a decoded gate exit containing status, checks, and typed error."""
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
    _record("exit")


def assert_extraction_scheduled(
    history: Any,
    activity_name: str,
    *,
    expected: int,
    gate_activity_name: str,
    expected_terminal: str,
    expected_failure_type: str | None = None,
) -> None:
    """Inspect a Temporal WorkflowHistory, not a mocked execute_activity call."""
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
    _record("history")


def pytest_addoption(parser: Any) -> None:
    parser.addoption("--preflight-report")
    parser.addoption("--preflight-rules", default="P062,P063,P064")


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "preflight_conformance(rule, scenario, entrypoint): executable preflight scenario",
    )
    config._preflight_evidence = {"tests": {}, "collection_errors": 0}


def pytest_collection_modifyitems(config: Any, items: list[Any]) -> None:
    if not config.getoption("--preflight-report"):
        return
    selected = set(config.getoption("--preflight-rules").split(","))
    kept, removed = [], []
    for item in items:
        marker = item.get_closest_marker("preflight_conformance")
        if marker is None or marker.kwargs.get("rule") not in selected:
            removed.append(item)
            continue
        kept.append(item)
        data = marker.kwargs
        config._preflight_evidence["tests"][item.nodeid] = {
            "rule": data.get("rule"),
            "scenario": data.get("scenario"),
            "entrypoint": data.get("entrypoint", "default"),
            "unsupported": bool(data.get("unsupported")),
            "reason": bool(str(data.get("reason", "")).strip()),
            "file": item.location[0],
            "line": item.location[1] + 1,
            "phases": {},
        }
    items[:] = kept
    config.hook.pytest_deselected(items=removed)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: Any):
    record = item.config._preflight_evidence["tests"].get(item.nodeid)
    token = _ACTIVE.set(record)
    try:
        return (yield)
    finally:
        _ACTIVE.reset(token)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: Any, call: Any):
    report = yield
    record = item.config._preflight_evidence["tests"].get(item.nodeid)
    if record is not None:
        required = {
            {"P062": "result", "P063": "history", "P064": "exit"}[record["rule"]]
        }
        if record["rule"] == "P062" and record["scenario"] in {
            "hung_probe",
            "cancellation_cleanup",
            "budget_retry",
        }:
            required.add("lifetime")
        if (
            report.when == "call"
            and report.outcome == "passed"
            and not required <= set(record.get("assertions", []))
        ):
            report.outcome = "failed"
            report.longrepr = (
                "Scenario did not execute the required preflight contract assertion"
            )
        record["phases"][report.when] = report.outcome
        record["xfail"] = bool(getattr(report, "wasxfail", False)) or record.get(
            "xfail", False
        )
    return report


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    path = session.config.getoption("--preflight-report")
    if path:
        data = session.config._preflight_evidence
        data["exitstatus"] = int(exitstatus)
        Path(path).write_text(json.dumps(data), encoding="utf-8")
