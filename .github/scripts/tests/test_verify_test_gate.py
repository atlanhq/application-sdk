"""Tests for .github/actions/verify-test-gate/verify_test_gate.py.

Co-located module (checked out with the composite action in consumer repos);
the test lives here with the other action-script tests.
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
    assert evaluate("success", "success", "success") == []


def test_all_skipped_e2e_not_requested() -> None:
    # No `e2e` label: discovery + matrix skipped, unit tests pass.
    assert evaluate("success", "skipped", "skipped") == []


# --- failing states --------------------------------------------------------


def test_tests_fail() -> None:
    errors = evaluate("failure", "skipped", "skipped")
    assert len(errors) == 1
    assert "tests job" in errors[0]


def test_discover_fail_requested_but_empty() -> None:
    # discovery failed (e2e requested, zero suites); e2e leg then skipped.
    errors = evaluate("success", "failure", "skipped")
    assert len(errors) == 1
    assert "discovery" in errors[0]


def test_e2e_leg_fail() -> None:
    errors = evaluate("success", "success", "failure")
    assert len(errors) == 1
    assert "e2e suites" in errors[0]


def test_multiple_failures_all_reported() -> None:
    errors = evaluate("failure", "failure", "failure")
    assert len(errors) == 3


def test_e2e_cancelled_is_failure() -> None:
    assert evaluate("success", "success", "cancelled") != []


# --- render() (display strings shared with the summary table) --------------


def test_render_all_pass() -> None:
    out = render("success", "success", "success")
    assert out["passed"] == "true"
    assert out["tests-status"] == "✅ Passed"
    assert out["e2e-status"] == "✅ Passed"
    assert out["overall-status"] == "✅ All passed"


def test_render_e2e_not_requested() -> None:
    out = render("success", "skipped", "skipped")
    assert out["passed"] == "true"
    assert "add `e2e` label" in out["e2e-status"]
    assert out["overall-status"] == "✅ All passed"


def test_render_discovery_failed_requested_but_empty() -> None:
    out = render("success", "failure", "skipped")
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ No suites discovered (e2e was requested)"
    assert out["overall-status"] == "❌ Some failed"


def test_render_e2e_leg_failed() -> None:
    out = render("success", "success", "failure")
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ Failed"


def test_render_tests_failed() -> None:
    out = render("failure", "skipped", "skipped")
    assert out["passed"] == "false"
    assert out["tests-status"] == "❌ Failed"


# --- CLI wrapper (emits outputs; never exits non-zero — job enforces) -------


def test_main_always_exits_zero_and_emits_passed_true(capsys) -> None:
    rc = main(["--tests", "success", "--discover-e2e", "skipped", "--e2e", "skipped"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "passed=true" in out
    assert "tests-status=✅ Passed" in out


def test_main_emits_passed_false_and_annotates_on_fail(capsys) -> None:
    rc = main(["--tests", "success", "--discover-e2e", "success", "--e2e", "failure"])
    captured = capsys.readouterr()
    assert rc == 0  # never fails itself; the gate job enforces via `passed`
    assert "passed=false" in captured.out
    assert "::error::" in captured.err
