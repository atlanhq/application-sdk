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

from verify_test_gate import evaluate, main  # noqa: E402

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


# --- CLI wrapper -----------------------------------------------------------


def test_main_returns_zero_on_pass(capsys) -> None:
    rc = main(["--tests", "success", "--discover-e2e", "skipped", "--e2e", "skipped"])
    assert rc == 0
    assert "All test jobs passed." in capsys.readouterr().out


def test_main_returns_one_and_annotates_on_fail(capsys) -> None:
    rc = main(["--tests", "success", "--discover-e2e", "success", "--e2e", "failure"])
    assert rc == 1
    assert "::error::" in capsys.readouterr().err
