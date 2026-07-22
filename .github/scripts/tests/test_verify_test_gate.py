"""Tests for .github/actions/verify-test-gate/verify_test_gate.py.

Co-located module (checked out with the composite action in consumer repos);
the test lives here with the other action-script tests.

Signature: evaluate/render take (unit, integration, discover_e2e, e2e).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "verify-test-gate")
)

from verify_test_gate import evaluate, main, render  # noqa: E402

# --- passing states --------------------------------------------------------


def test_all_pass() -> None:
    assert evaluate("success", "success", "success", "success") == []


def test_integration_skipped_is_pass() -> None:
    # Integration is skipped on PRs and when a connector has no integration
    # suite — both legitimate. Unit passing + e2e not requested ⇒ gate passes.
    assert evaluate("success", "skipped", "skipped", "skipped") == []


# --- failing states --------------------------------------------------------


def test_unit_fail() -> None:
    errors = evaluate("failure", "success", "skipped", "skipped")
    assert len(errors) == 1
    assert "unit tests" in errors[0]


def test_integration_fail() -> None:
    errors = evaluate("success", "failure", "skipped", "skipped")
    assert len(errors) == 1
    assert "integration tests" in errors[0]


def test_integration_cancelled_is_failure() -> None:
    assert evaluate("success", "cancelled", "skipped", "skipped") != []


def test_discover_fail_requested_but_empty() -> None:
    # discovery failed (e2e requested, zero suites); e2e leg then skipped.
    errors = evaluate("success", "success", "failure", "skipped")
    assert len(errors) == 1
    assert "discovery" in errors[0]


def test_e2e_leg_fail() -> None:
    errors = evaluate("success", "success", "success", "failure")
    assert len(errors) == 1
    assert "e2e suites" in errors[0]


def test_multiple_failures_all_reported() -> None:
    # unit, integration, discovery, e2e all bad → 4 reasons.
    errors = evaluate("failure", "failure", "failure", "failure")
    assert len(errors) == 4


def test_e2e_cancelled_is_failure() -> None:
    assert evaluate("success", "success", "success", "cancelled") != []


def test_e2e_skipped_when_discovery_succeeded_is_failure() -> None:
    # Discovery success ⇒ suites exist ⇒ the matrix must run. A skipped matrix
    # here (e.g. a future caller re-wired the e2e `if`) must not green the gate.
    errors = evaluate("success", "success", "success", "skipped")
    assert len(errors) == 1
    assert "matrix was skipped" in errors[0]
    out = render("success", "success", "success", "skipped")
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ Matrix skipped despite discovered suites"


# --- render() (display strings shared with the summary table) --------------


def test_render_all_pass() -> None:
    out = render("success", "success", "success", "success")
    assert out["passed"] == "true"
    assert out["unit-status"] == "✅ Passed"
    assert out["integration-status"] == "✅ Passed"
    assert out["e2e-status"] == "✅ Passed"
    assert out["overall-status"] == "✅ All passed"


def test_render_integration_skipped() -> None:
    out = render("success", "skipped", "skipped", "skipped")
    assert out["passed"] == "true"
    assert "Skipped" in out["integration-status"]
    assert "add `e2e` label" in out["e2e-status"]
    assert out["overall-status"] == "✅ All passed"


def test_render_discovery_failed_requested_but_empty() -> None:
    out = render("success", "success", "failure", "skipped")
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ No suites discovered (e2e was requested)"
    assert out["overall-status"] == "❌ Some failed"


def test_render_e2e_leg_failed() -> None:
    out = render("success", "success", "success", "failure")
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ Failed"


def test_render_unit_failed() -> None:
    out = render("failure", "skipped", "skipped", "skipped")
    assert out["passed"] == "false"
    assert out["unit-status"] == "❌ Failed"


def test_render_integration_failed() -> None:
    out = render("success", "failure", "skipped", "skipped")
    assert out["passed"] == "false"
    assert out["integration-status"] == "❌ Failed"


# --- CLI wrapper (emits outputs; never exits non-zero — job enforces) -------


def test_main_always_exits_zero_and_emits_passed_true(capsys) -> None:
    rc = main(
        [
            "--unit",
            "success",
            "--integration",
            "skipped",
            "--discover-e2e",
            "skipped",
            "--e2e",
            "skipped",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "passed=true" in out
    assert "unit-status=✅ Passed" in out


def test_main_emits_passed_false_and_annotates_on_fail(capsys) -> None:
    rc = main(
        [
            "--unit",
            "success",
            "--integration",
            "success",
            "--discover-e2e",
            "success",
            "--e2e",
            "failure",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0  # never fails itself; the gate job enforces via `passed`
    assert "passed=false" in captured.out
    assert "::error::" in captured.err
