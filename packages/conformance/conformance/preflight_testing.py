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

import math
from typing import Any

from conformance.preflight_scenarios import SCENARIOS

__all__ = [
    "SCENARIOS",
    "assert_preflight_result",
    "assert_probe_lifetime",
]


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


def pytest_configure(config: Any) -> None:
    config.addinivalue_line(
        "markers",
        "preflight_conformance(rule, scenario, entrypoint): registered F016 preflight scenario",
    )
