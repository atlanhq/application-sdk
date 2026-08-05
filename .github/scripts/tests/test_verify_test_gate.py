"""Tests for .github/actions/verify-test-gate/verify_test_gate.py.

Co-located module (checked out with the composite action in consumer repos);
the test lives here with the other action-script tests.

Signature: evaluate/render take
(unit, integration, detect_integration, discover_e2e, e2e).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "actions" / "verify-test-gate")
)

from verify_test_gate import evaluate, main, render  # noqa: E402

# --- passing states --------------------------------------------------------


def test_all_pass() -> None:
    assert evaluate("success", "success", "success", "success", "success") == []


def test_integration_skipped_is_pass() -> None:
    # Integration is skipped on PRs and when a connector has no integration
    # suite — both legitimate. On a PR detect-integration is also skipped.
    # Unit passing + e2e not requested ⇒ gate passes.
    assert evaluate("success", "skipped", "skipped", "skipped", "skipped") == []


def test_integration_skipped_no_suite_is_pass() -> None:
    # Non-PR, connector ships no integration suite: detect-integration succeeds
    # (count=0 is legitimate) and integration skips cleanly — a pass.
    assert evaluate("success", "skipped", "success", "skipped", "skipped") == []


# --- failing states --------------------------------------------------------


def test_unit_fail() -> None:
    errors = evaluate("failure", "success", "success", "skipped", "skipped")
    assert len(errors) == 1
    assert "unit tests" in errors[0]


def test_integration_fail() -> None:
    errors = evaluate("success", "failure", "success", "skipped", "skipped")
    assert len(errors) == 1
    assert "integration tests" in errors[0]


def test_integration_cancelled_is_failure() -> None:
    assert evaluate("success", "cancelled", "success", "skipped", "skipped") != []


def test_detect_integration_fail_fails_gate() -> None:
    # The core hole this closes: a detection failure drops integration to a
    # skip (which on its own reads as a pass), so the failed detection must
    # itself fail the gate.
    errors = evaluate("success", "skipped", "failure", "skipped", "skipped")
    assert len(errors) == 1
    assert "integration-suite detection" in errors[0]


def test_detect_integration_cancelled_is_failure() -> None:
    assert evaluate("success", "skipped", "cancelled", "skipped", "skipped") != []


def test_discover_fail_requested_but_empty() -> None:
    # discovery failed (e2e requested, zero suites); e2e leg then skipped.
    errors = evaluate("success", "success", "success", "failure", "skipped")
    assert len(errors) == 1
    assert "discovery" in errors[0]


def test_e2e_leg_fail() -> None:
    errors = evaluate("success", "success", "success", "success", "failure")
    assert len(errors) == 1
    assert "e2e suites" in errors[0]


def test_multiple_failures_all_reported() -> None:
    # unit, integration, detect-integration, discovery, e2e all bad → 5 reasons.
    errors = evaluate("failure", "failure", "failure", "failure", "failure")
    assert len(errors) == 5


def test_e2e_cancelled_is_failure() -> None:
    assert evaluate("success", "success", "success", "success", "cancelled") != []


def test_e2e_skipped_when_discovery_succeeded_is_failure() -> None:
    # Discovery success ⇒ suites exist ⇒ the matrix must run. A skipped matrix
    # here (e.g. a future caller re-wired the e2e `if`) must not green the gate.
    errors = evaluate("success", "success", "success", "success", "skipped")
    assert len(errors) == 1
    assert "matrix was skipped" in errors[0]
    out = render("success", "success", "success", "success", "skipped")
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ Matrix skipped despite discovered suites"


# --- render() (display strings shared with the summary table) --------------


def test_render_all_pass() -> None:
    out = render("success", "success", "success", "success", "success")
    assert out["passed"] == "true"
    assert out["unit-status"] == "✅ Passed"
    assert out["integration-status"] == "✅ Passed"
    assert out["e2e-status"] == "✅ Passed"
    assert out["overall-status"] == "✅ All passed"


def test_render_integration_skipped() -> None:
    out = render("success", "skipped", "skipped", "skipped", "skipped")
    assert out["passed"] == "true"
    assert "Skipped" in out["integration-status"]
    assert "add `e2e` label" in out["e2e-status"]
    assert out["overall-status"] == "✅ All passed"


def test_render_detect_integration_failed() -> None:
    # A detection failure fails the gate and the integration row surfaces the
    # detection failure rather than the benign "skipped" string.
    out = render("success", "skipped", "failure", "skipped", "skipped")
    assert out["passed"] == "false"
    assert out["integration-status"] == "❌ Integration-suite detection failed"
    assert out["overall-status"] == "❌ Some failed"


def test_render_discovery_failed_requested_but_empty() -> None:
    out = render("success", "success", "success", "failure", "skipped")
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ No suites discovered (e2e was requested)"
    assert out["overall-status"] == "❌ Some failed"


def test_render_e2e_leg_failed() -> None:
    out = render("success", "success", "success", "success", "failure")
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ Failed"


def test_render_unit_failed() -> None:
    out = render("failure", "skipped", "skipped", "skipped", "skipped")
    assert out["passed"] == "false"
    assert out["unit-status"] == "❌ Failed"


def test_render_integration_failed() -> None:
    out = render("success", "failure", "success", "skipped", "skipped")
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
            "--detect-integration",
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
            "--detect-integration",
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


def test_main_detect_integration_failure_annotates(capsys) -> None:
    rc = main(
        [
            "--unit",
            "success",
            "--integration",
            "skipped",
            "--detect-integration",
            "failure",
            "--discover-e2e",
            "skipped",
            "--e2e",
            "skipped",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "passed=false" in captured.out
    assert "integration-suite detection" in captured.err


# --- merge-queue detection (integration tier routing) ----------------------
# detect-merge-queue decides WHERE the integration tier runs: in the merge queue
# when the base branch has one, else on the pull_request. It is the last
# positional arg and defaults to "skipped" because this driver is consumed
# cross-repo at @main.


def test_detect_merge_queue_defaults_to_skipped() -> None:
    # Back-compat: a caller that has not wired the job at all still passes.
    assert evaluate("success", "skipped", "skipped", "skipped", "skipped") == []


@pytest.mark.parametrize("result", ["success", "skipped"])
def test_detect_merge_queue_ok_states_pass(result) -> None:
    # "success" = a PR where detection ran; "skipped" = a non-PR event.
    assert evaluate("success", "success", "success", "skipped", "skipped", result) == []


@pytest.mark.parametrize("result", ["failure", "cancelled", "timed_out"])
def test_detect_merge_queue_failure_fails_the_gate(result) -> None:
    # A detection failure drops the integration tier to a skip, so it must fail
    # the gate rather than green it — the same hole closed for detect-integration.
    errors = evaluate("success", "skipped", "skipped", "skipped", "skipped", result)
    assert len(errors) == 1
    assert "merge-queue detection" in errors[0]


def test_detect_merge_queue_failure_shows_in_integration_row() -> None:
    # Display must not claim the tier was cleanly skipped when routing broke.
    out = render("success", "skipped", "skipped", "skipped", "skipped", "failure")
    assert out["passed"] == "false"
    assert out["integration-status"] == "❌ Merge-queue detection failed"


def test_integration_runs_on_pr_when_no_queue() -> None:
    # No queue ⇒ detect-merge-queue succeeded and integration ran on the PR.
    out = render("success", "success", "success", "skipped", "skipped", "success")
    assert out["passed"] == "true"
    assert out["integration-status"] == "✅ Passed"


def test_integration_failure_on_pr_blocks_the_gate() -> None:
    # The whole point: on a queue-less repo a broken integration suite now
    # blocks the PR instead of reddening main after the merge.
    out = render("success", "failure", "success", "skipped", "skipped", "success")
    assert out["passed"] == "false"
    assert out["integration-status"] == "❌ Failed"


def test_skipped_integration_string_does_not_promise_a_merge_queue() -> None:
    # Queue-less repos have no merge queue to defer to, so the row must not
    # assert one exists.
    out = render("success", "skipped", "success", "skipped", "skipped", "success")
    assert "no integration suite" in out["integration-status"]


def test_main_detect_merge_queue_failure_annotates(capsys) -> None:
    rc = main(
        [
            "--unit",
            "success",
            "--integration",
            "skipped",
            "--detect-integration",
            "skipped",
            "--detect-merge-queue",
            "failure",
            "--discover-e2e",
            "skipped",
            "--e2e",
            "skipped",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "passed=false" in captured.out
    assert "merge-queue detection" in captured.err


def test_main_omitting_detect_merge_queue_still_passes(capsys) -> None:
    # Cross-repo @main back-compat at the CLI boundary, not just the function.
    rc = main(
        [
            "--unit",
            "success",
            "--integration",
            "skipped",
            "--detect-integration",
            "skipped",
            "--discover-e2e",
            "skipped",
            "--e2e",
            "skipped",
        ]
    )
    assert rc == 0
    assert "passed=true" in capsys.readouterr().out
