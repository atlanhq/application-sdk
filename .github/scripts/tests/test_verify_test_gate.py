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

from verify_test_gate import (  # noqa: E402
    cancelled_only,
    evaluate,
    main,
    render,
    stood_down,
)

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


# --- the stand-down: an EXPLAINED skipped matrix (FND-701) -----------------
#
# The connector-side recheck skips lease-tenant and the e2e legs when the
# application-sdk commit under test is no longer the head of the PR that
# dispatched the run. That produces the exact tuple the anomaly above exists to
# catch — discovery success, matrix skipped, no install-path failure — so the
# gate needs telling the difference. Getting it wrong in either direction is a
# real cost: unsuppressed, every stand-down reds the gate AND mirrors
# conclusion=failure onto the dispatching SDK commit ("your change broke the
# connector", the FND-218 misattribution); over-suppressed, a future re-wiring of
# the e2e `if` greens the gate by skipping it.

_STOOD_DOWN = ("success", "success", "success", "success", "skipped")


def test_a_stand_down_is_not_an_anomaly() -> None:
    assert evaluate(*_STOOD_DOWN, superseded="true") == []


def test_a_stand_down_greens_the_gate_and_says_why() -> None:
    """`conclusion` is the part that travels: report-to-sdk feeds it straight
    into the check run on the dispatching SDK commit."""
    out = render(*_STOOD_DOWN, superseded="true")

    assert out["passed"] == "true"
    assert out["conclusion"] == "success"
    assert out["e2e-status"] == "⊘ Stood down — superseded SDK commit"
    assert out["overall-status"] == "✅ All passed"


@pytest.mark.parametrize(
    "superseded", ["", "false", "False", "no", "1", "yes", "TRUE "]
)
def test_only_a_positive_assertion_explains_the_skip(superseded: str) -> None:
    """Everything except the literal "true" leaves the anomaly firing. An absent
    or unparseable value means the recheck job did not answer — it skipped, or it
    died — and an unanswered skip is still unexplained, which is the state worth
    reddening. "TRUE " is included because it IS accepted: the value is trimmed
    and lowercased, and a workflow expression that renders with whitespace must
    not silently stop explaining."""
    errors = evaluate(*_STOOD_DOWN, superseded=superseded)

    if stood_down(superseded):
        assert errors == []
    else:
        assert any("matrix was skipped" in error for error in errors)


def test_the_anomaly_survives_for_an_unexplained_skip() -> None:
    """The regression this pairing has to keep passing: the suppression is
    conditional, not a removal."""
    errors = evaluate(*_STOOD_DOWN)

    assert len(errors) == 1
    assert "matrix was skipped" in errors[0]


def test_a_stand_down_does_not_excuse_a_real_failure() -> None:
    """Standing e2e down says nothing about the unit tier. A superseded run whose
    unit tests failed must still fail the gate — otherwise the stand-down becomes
    a way to green anything."""
    errors = evaluate(
        "failure", "success", "success", "success", "skipped", superseded="true"
    )

    assert len(errors) == 1
    assert "unit tests" in errors[0]


def test_a_stand_down_leaves_a_cancellation_readable_as_one() -> None:
    """cancelled_only short-circuits the anomaly tuple to False, on the grounds
    that the anomaly is never cancellation-attributable. A stand-down is not the
    anomaly, so it must not take that branch — or a genuinely cancelled unit job
    would be relabelled "failure" and sent to the wrong reader."""
    assert (
        cancelled_only(
            "cancelled", "success", "success", "success", "skipped", superseded="true"
        )
        is True
    )
    assert (
        render(
            "cancelled", "success", "success", "success", "skipped", superseded="true"
        )["conclusion"]
        == "cancelled"
    )


def test_an_install_path_failure_still_names_itself_under_a_stand_down() -> None:
    """The stand-down suppresses the anomaly, never a named job. A lease that
    actually failed has to keep saying so — "the tenant was busy" and "the commit
    was superseded" call for opposite responses."""
    errors = evaluate(
        "success",
        "success",
        "success",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "skipped",
        "failure",
        "true",
    )

    assert len(errors) == 1
    assert "the tenant lease" in errors[0]


def test_the_flag_defaults_to_unexplained(capsys: pytest.CaptureFixture) -> None:
    """A caller pinned at @main that has not wired the recheck job keeps the
    previous behaviour exactly, which is what makes this deployable without
    touching a single connector repo."""
    main(
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
            "skipped",
        ]
    )

    assert "passed=false" in capsys.readouterr().out


def test_the_flag_reaches_the_driver(capsys: pytest.CaptureFixture) -> None:
    main(
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
            "skipped",
            "--superseded",
            "true",
        ]
    )
    out = capsys.readouterr().out

    assert "passed=true" in out
    assert "e2e-status=⊘ Stood down — superseded SDK commit" in out


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


# --- the install path ------------------------------------------------------
# build-e2e-image (per-arch matrix) → merge-e2e-image (manifest list) →
# prepare-tenant → e2e. Every one of those gates the next job's `if`, and all
# three are named in the e2e matrix's own `if`, so a failure in any of them
# drops the matrix to a *skip*. The anomaly rule above already refused to green
# that, but it reported the symptom ("matrix skipped") rather than the cause,
# and the cause is what a reviewer needs: the first real occurrence was one
# architecture's `FROM` failing on a single-arch base image. Trailing kwargs
# defaulting to "skipped" for cross-repo @main back-compat, as with
# detect-merge-queue.

# (evaluate/render positional order after e2e: detect_merge_queue,
# build_e2e_image, merge_e2e_image, prepare_tenant.)
_GREEN_UPTO_E2E = ("success", "success", "success", "success")


def test_image_legs_default_to_skipped() -> None:
    # A caller that has not wired the jobs at all still passes.
    assert evaluate("success", "success", "success", "success", "success") == []


@pytest.mark.parametrize("result", ["success", "skipped"])
def test_install_path_ok_states_pass(result) -> None:
    # "success" = the install path ran; "skipped" = install path off / no e2e.
    assert (
        evaluate(
            *_GREEN_UPTO_E2E,
            "success",
            "skipped",
            result,
            result,
            result,
        )
        == []
    )


def test_failed_image_build_fails_the_gate_and_names_the_cause() -> None:
    # The exact shape of the real failure: one arch leg died, the merge and the
    # matrix skipped behind it, and everything else was green.
    errors = evaluate(
        "success",
        "success",
        "success",
        "success",
        "skipped",
        "success",
        "failure",
        "skipped",
    )
    assert len(errors) == 1, (
        "the image-build failure explains the skipped matrix, so the anomaly "
        f"rule must not also fire and bury it: {errors}"
    )
    assert "e2e image build" in errors[0]


def test_failed_manifest_merge_fails_the_gate_and_names_the_cause() -> None:
    errors = evaluate(
        "success",
        "success",
        "success",
        "success",
        "skipped",
        "success",
        "success",
        "failure",
    )
    assert len(errors) == 1
    assert "manifest merge" in errors[0]


@pytest.mark.parametrize("result", ["failure", "cancelled", "timed_out"])
def test_image_build_non_success_states_all_fail(result) -> None:
    assert (
        evaluate(
            "success",
            "success",
            "success",
            "success",
            "skipped",
            "success",
            result,
            "skipped",
        )
        != []
    )


def test_failed_tenant_install_fails_the_gate_and_names_the_cause() -> None:
    # The third install-path job, and the one the e2e legs care most about: a
    # failed install leaves the tenant on whatever it was running, so legs that
    # ran anyway would silently test the wrong version.
    errors = evaluate(
        *_GREEN_UPTO_E2E,
        "skipped",
        "success",
        "success",
        "success",
        "failure",
    )
    assert len(errors) == 1
    assert "tenant install" in errors[0]


def test_anomaly_rule_still_fires_when_the_install_path_is_clean() -> None:
    # Unexplained skip (e.g. a caller re-wired the e2e `if`) — the anomaly rule
    # is the only thing standing between that and a green gate, so suppressing
    # it must be strictly conditional on an install-path job having actually
    # failed.
    errors = evaluate(
        *_GREEN_UPTO_E2E,
        "skipped",
        "success",
        "success",
        "success",
        "success",
    )
    assert len(errors) == 1
    assert "matrix was skipped" in errors[0]


def test_every_install_path_job_is_judged() -> None:
    """Each of the three, one at a time, must fail the gate on its own.

    They are exactly the jobs named in the e2e matrix's `if`; a job present
    there but absent here is a skip the gate would read as benign.
    """
    for position in range(3):
        results = ["success", "success", "success"]
        results[position] = "failure"
        errors = evaluate(*_GREEN_UPTO_E2E, "skipped", "success", *results)
        assert len(errors) == 1, f"install-path job {position} is not judged"
        assert "matrix was skipped" not in errors[0], (
            "the anomaly rule fired instead of the specific job — the cause is "
            "buried under the symptom again"
        )


def test_render_image_build_failure_shows_in_the_e2e_row() -> None:
    out = render(
        "success",
        "success",
        "success",
        "success",
        "skipped",
        "success",
        "failure",
        "skipped",
    )
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ e2e image build failed", (
        "the row must not read as a benign skip when the image build is why "
        "nothing ran"
    )


def test_render_manifest_merge_failure_shows_in_the_e2e_row() -> None:
    out = render(
        "success",
        "success",
        "success",
        "success",
        "skipped",
        "success",
        "success",
        "failure",
    )
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ e2e image manifest merge failed"


def test_render_tenant_install_failure_shows_in_the_e2e_row() -> None:
    out = render(
        *_GREEN_UPTO_E2E,
        "skipped",
        "success",
        "success",
        "success",
        "failure",
    )
    assert out["passed"] == "false"
    assert out["e2e-status"] == "❌ Tenant install failed"


def test_the_e2e_row_names_the_first_install_path_job_that_failed() -> None:
    # When the build fails, everything behind it skips — but a cancelled run can
    # fail several at once. The row names the earliest, which is the one worth
    # looking at; the annotations still list them all.
    out = render(
        *_GREEN_UPTO_E2E,
        "skipped",
        "success",
        "failure",
        "cancelled",
        "cancelled",
    )
    assert out["e2e-status"] == "❌ e2e image build failed"
    assert (
        len(
            evaluate(
                *_GREEN_UPTO_E2E,
                "skipped",
                "success",
                "failure",
                "cancelled",
                "cancelled",
            )
        )
        == 3
    )


def test_main_image_build_failure_annotates(capsys) -> None:
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
            "--build-e2e-image",
            "failure",
            "--merge-e2e-image",
            "skipped",
            "--e2e",
            "skipped",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "passed=false" in captured.out
    assert "e2e image build" in captured.err


def test_main_omitting_the_image_flags_still_passes(capsys) -> None:
    # Cross-repo @main back-compat at the CLI boundary.
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


# --- conclusion output (what the cross-repo callback reports) --------------
# report-to-sdk completes the `Connector E2E run / <app>` check run on the
# dispatching application-sdk PR with this value. It exists so that mapping the
# verdict onto a Checks API conclusion happens HERE rather than in workflow YAML
# — a `passed == 'true' && 'success' || 'failure'` expression in the callback
# would be a second place the verdict is decided.


def test_conclusion_is_success_when_the_gate_passes() -> None:
    out = render("success", "skipped", "skipped", "skipped", "skipped")
    assert out["passed"] == "true"
    assert out["conclusion"] == "success"


def test_conclusion_is_failure_when_the_gate_fails() -> None:
    out = render("success", "success", "success", "success", "failure")
    assert out["passed"] == "false"
    assert out["conclusion"] == "failure"


def test_conclusion_tracks_passed_across_every_scenario_in_this_module() -> None:
    """The two outputs are one verdict; nothing may make them disagree.

    A future edit that adds a rule to `passed` but forgets `conclusion` would
    reopen exactly the gap this change closed — the SDK-side check reporting
    something other than what the gate decided.
    """
    scenarios = [
        (
            "success",
            "success",
            "success",
            "success",
            "success",
            "success",
            "success",
            "success",
        ),
        (
            "failure",
            "success",
            "success",
            "skipped",
            "skipped",
            "skipped",
            "skipped",
            "skipped",
        ),
        (
            "success",
            "skipped",
            "failure",
            "skipped",
            "skipped",
            "skipped",
            "skipped",
            "skipped",
        ),
        (
            "success",
            "skipped",
            "skipped",
            "skipped",
            "skipped",
            "failure",
            "skipped",
            "skipped",
        ),
        (
            "success",
            "success",
            "success",
            "failure",
            "skipped",
            "success",
            "skipped",
            "skipped",
        ),
        (
            "success",
            "success",
            "success",
            "success",
            "skipped",
            "success",
            "failure",
            "skipped",
        ),
        (
            "success",
            "success",
            "success",
            "success",
            "skipped",
            "success",
            "success",
            "failure",
        ),
        (
            "success",
            "success",
            "success",
            "success",
            "skipped",
            "success",
            "success",
            "success",
        ),
        (
            "success",
            "success",
            "success",
            "success",
            "cancelled",
            "success",
            "success",
            "success",
        ),
    ]
    for scenario in scenarios:
        out = render(*scenario)
        # A blocked gate spells itself "cancelled" rather than "failure" when
        # nothing actually failed (see the cancellation tests below) — so the
        # invariant is that `conclusion` agrees with `passed` about PASS/BLOCK,
        # not that BLOCK has exactly one spelling.
        if out["passed"] == "true":
            assert out["conclusion"] == "success", f"disagreed on {scenario}"
        else:
            assert out["conclusion"] in (
                "failure",
                "cancelled",
            ), f"disagreed on {scenario}"
            assert out["conclusion"] != "success", f"disagreed on {scenario}"


def test_the_real_failure_this_change_was_written_for() -> None:
    """End to end, in the shape it actually occurred.

    application-sdk#3074 dispatched e2e to atlan-openapi-app. Unit, integration
    and discovery were green; the arm64 leg of the image build failed on a
    single-arch base; merge and the matrix skipped behind it. The Tests Gate
    failed — and the callback, judging a smaller input set, completed the
    SDK-side check run as SUCCESS. Both must now read failure.
    """
    out = render(
        unit="success",
        integration="success",
        detect_integration="success",
        discover_e2e="success",
        e2e="skipped",
        detect_merge_queue="skipped",
        build_e2e_image="failure",
        merge_e2e_image="skipped",
    )
    assert out["passed"] == "false"
    assert out["conclusion"] == "failure", (
        "this is the bug: the check run mirrored onto the dispatching SDK PR "
        "must report the connector run's failure"
    )
    assert out["e2e-status"] == "❌ e2e image build failed"


# --- cancellation is not failure (FND-218) ---------------------------------
#
# GitHub keeps at most ONE pending run per concurrency group, so a third
# arrival cancels the queued one before it is ever given a runner. The gate
# must still BLOCK (an un-run test cannot green a merge) but must not spell
# that block "failure" — a mirrored failure on the dispatching SDK PR reads as
# "your change broke the connector" and sends a reviewer into the wrong diff.


def test_cancelled_integration_blocks_but_reports_cancelled() -> None:
    out = render("success", "cancelled", "success", "skipped", "skipped")
    assert out["passed"] == "false", "an un-run test must never green the gate"
    assert out["conclusion"] == "cancelled"
    assert out["overall-status"] == "🚫 Cancelled — no verdict, re-run"
    assert out["integration-status"] == "🚫 Cancelled — not run"


def test_cancelled_unit_reports_cancelled() -> None:
    out = render("cancelled", "skipped", "skipped", "skipped", "skipped")
    assert out["passed"] == "false"
    assert out["conclusion"] == "cancelled"
    assert out["unit-status"] == "🚫 Cancelled — not run"


def test_cancelled_e2e_leg_reports_cancelled() -> None:
    out = render("success", "success", "success", "success", "cancelled")
    assert out["passed"] == "false"
    assert out["conclusion"] == "cancelled"
    assert out["e2e-status"] == "🚫 Cancelled — not run"


def test_a_real_failure_alongside_a_cancellation_is_still_failure() -> None:
    # The discriminator is "did anything actually fail", not "was anything
    # cancelled". A cancelled sibling must not launder a genuine failure into
    # a benign "just re-run" verdict.
    out = render("success", "failure", "success", "success", "cancelled")
    assert out["passed"] == "false"
    assert out["conclusion"] == "failure"


@pytest.mark.parametrize(
    "kwargs,row",
    [
        ({"build_e2e_image": "cancelled"}, "🚫 e2e image build cancelled — not run"),
        (
            {"merge_e2e_image": "cancelled"},
            "🚫 e2e image manifest merge cancelled — not run",
        ),
        ({"prepare_tenant": "cancelled"}, "🚫 Tenant install cancelled — not run"),
    ],
)
def test_cancelled_install_path_jobs_report_cancelled(kwargs, row) -> None:
    out = render(
        unit="success",
        integration="success",
        detect_integration="success",
        discover_e2e="success",
        e2e="skipped",
        **kwargs,
    )
    assert out["passed"] == "false"
    assert out["conclusion"] == "cancelled"
    assert out["e2e-status"] == row


@pytest.mark.parametrize(
    "kwargs,row",
    [
        (
            {"detect_merge_queue": "cancelled"},
            "🚫 Merge-queue detection cancelled — not run",
        ),
        (
            {"detect_integration": "cancelled"},
            "🚫 Integration-suite detection cancelled — not run",
        ),
    ],
)
def test_cancelled_detection_jobs_report_cancelled(kwargs, row) -> None:
    defaults = {
        "unit": "success",
        "integration": "skipped",
        "detect_integration": "success",
        "discover_e2e": "skipped",
        "e2e": "skipped",
    }
    out = render(**{**defaults, **kwargs})
    assert out["passed"] == "false"
    assert out["conclusion"] == "cancelled"
    assert out["integration-status"] == row


def test_cancelled_discovery_reports_cancelled() -> None:
    out = render("success", "success", "success", "cancelled", "skipped")
    assert out["passed"] == "false"
    assert out["conclusion"] == "cancelled"
    assert out["e2e-status"] == "🚫 e2e discovery cancelled — not run"


def test_the_matrix_skipped_anomaly_is_not_laundered_as_a_cancellation() -> None:
    # This anomaly errors while every result sits in the OK set, so a naive
    # "no failures ⇒ cancelled" rule would report it as "just re-run" and hide
    # a genuine misconfiguration. cancelled_only requires an actual
    # cancellation to be present.
    assert not cancelled_only("success", "success", "success", "success", "skipped")
    out = render("success", "success", "success", "success", "skipped")
    assert out["passed"] == "false"
    assert out["conclusion"] == "failure"


def test_cancelled_only_is_false_when_the_gate_passes() -> None:
    # No cancellation present, nothing blocking — there is nothing to explain.
    assert not cancelled_only("success", "success", "success", "success", "success")


def test_a_cancellation_does_not_mask_the_matrix_skipped_anomaly() -> None:
    # The anomaly error is raised by evaluate() independently of the raw job
    # results, so a cancellation elsewhere does not make it go away. cancelled_only
    # used to inspect raw results alone: a cancelled detection job coinciding with
    # the anomaly (discovery green, matrix skipped, install path clean) returned
    # True, and the gate spelled a genuine misconfiguration "just re-run".
    assert not cancelled_only(
        "success", "success", "success", "success", "skipped", "cancelled"
    )
    out = render("success", "success", "success", "success", "skipped", "cancelled")
    assert out["passed"] == "false"
    assert out["conclusion"] == "failure", (
        "the anomaly is not cancellation-attributable, so its presence must "
        "spell failure and surface the misconfiguration — never a benign re-run"
    )
    errors = evaluate(
        "success", "success", "success", "success", "skipped", "cancelled"
    )
    assert any("matrix was skipped" in e for e in errors)


def test_main_annotates_the_cancellation_guidance(capsys) -> None:
    rc = main(
        [
            "--unit",
            "success",
            "--integration",
            "cancelled",
            "--detect-integration",
            "success",
            "--discover-e2e",
            "skipped",
            "--e2e",
            "skipped",
        ]
    )
    assert rc == 0
    captured = capsys.readouterr()
    assert "conclusion=cancelled" in captured.out
    assert "passed=false" in captured.out
    # The per-job reason still fires; the guidance is additive, and follows it
    # so the annotation list reads cause-then-verdict.
    assert "integration tests did not succeed (result=cancelled)" in captured.err
    assert "were cancelled, not failed" in captured.err
    assert "Re-run rather than triage the diff." in captured.err


def test_main_does_not_annotate_the_guidance_on_a_real_failure(capsys) -> None:
    main(
        [
            "--unit",
            "success",
            "--integration",
            "failure",
            "--detect-integration",
            "success",
            "--discover-e2e",
            "skipped",
            "--e2e",
            "skipped",
        ]
    )
    captured = capsys.readouterr()
    assert "conclusion=failure" in captured.out
    assert "Re-run rather than triage" not in captured.err


# --- tenant lease (FND-250) ------------------------------------------------


def test_lease_tenant_skipped_is_a_pass() -> None:
    # The default for every caller pinned at @main that has not wired the job,
    # and the honest value when the install path is off.
    assert evaluate("success", "skipped", "skipped", "skipped", "skipped") == []


def test_lease_tenant_success_is_a_pass() -> None:
    assert (
        evaluate(
            "success",
            "success",
            "success",
            "success",
            "success",
            "success",
            "success",
            "success",
            "success",
            "success",
        )
        == []
    )


def test_lease_tenant_failure_fails_the_gate_by_name() -> None:
    # Without this the failure surfaces only as the downstream "matrix skipped"
    # anomaly, which reads as a workflow misconfiguration rather than "the tenant
    # was busy".
    errors = evaluate(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "skipped",
        "failure",
    )
    assert any("the tenant lease did not succeed (result=failure)" in e for e in errors)


def test_lease_tenant_failure_suppresses_the_matrix_anomaly() -> None:
    # One cause, one annotation: the lease failing IS why the matrix skipped.
    errors = evaluate(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "skipped",
        "failure",
    )
    assert not any("matrix was skipped" in e for e in errors)


def test_lease_tenant_row_says_the_tenant_was_busy() -> None:
    # "Failed" here would send a reviewer into their own diff; the tenant was
    # occupied, which is a re-run, not a fix.
    row = render(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "skipped",
        "failure",
    )["e2e-status"]
    assert row == "⏳ Tenant busy — lease not acquired, re-run"


def test_lease_tenant_cancelled_row_reads_as_cancelled() -> None:
    row = render(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "skipped",
        "cancelled",
    )["e2e-status"]
    assert row == "🚫 Tenant lease cancelled — not run"


def test_lease_tenant_cancelled_is_reported_as_cancelled_not_failure() -> None:
    outputs = render(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "skipped",
        "cancelled",
    )
    assert outputs["passed"] == "false"
    assert outputs["conclusion"] == "cancelled"


def test_lease_tenant_failure_is_reported_as_failure() -> None:
    # A lease TIMEOUT is a genuine job failure, not a cancellation: nothing was
    # evicted, the run waited its full budget and gave up.
    outputs = render(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "skipped",
        "failure",
    )
    assert outputs["passed"] == "false"
    assert outputs["conclusion"] == "failure"


def test_lease_tenant_row_yields_to_an_earlier_install_path_failure() -> None:
    # Reported in pipeline order, so the row names the FIRST thing that broke —
    # a failed image build explains the lease never running.
    row = render(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "failure",
        "skipped",
        "skipped",
        "skipped",
    )["e2e-status"]
    assert row == "❌ e2e image build failed"


def test_lease_tenant_precedes_the_install_in_the_reported_order() -> None:
    # Both broken: the lease is reported, because it runs first and a tenant that
    # was never leased is why the install did not happen.
    row = render(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "failure",
        "failure",
    )["e2e-status"]
    assert row == "⏳ Tenant busy — lease not acquired, re-run"


def test_main_accepts_the_lease_tenant_flag(capsys) -> None:
    main(
        [
            "--unit",
            "success",
            "--integration",
            "skipped",
            "--detect-integration",
            "skipped",
            "--discover-e2e",
            "success",
            "--build-e2e-image",
            "success",
            "--merge-e2e-image",
            "success",
            "--lease-tenant",
            "failure",
            "--prepare-tenant",
            "skipped",
            "--e2e",
            "skipped",
        ]
    )
    captured = capsys.readouterr()
    assert "passed=false" in captured.out
    assert "the tenant lease did not succeed" in captured.err


def test_lease_tenant_flag_defaults_to_skipped(capsys) -> None:
    # Cross-repo compatibility: a caller pinned at @main that has not added the
    # job yet must keep passing.
    main(
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
    assert "passed=true" in capsys.readouterr().out


def test_a_cancelled_lease_that_skipped_the_matrix_is_still_a_cancellation() -> None:
    """Regression: the matrix-skipped anomaly guard must see the lease result.

    `cancelled_only` excludes the "discovery succeeded but the matrix was
    skipped" anomaly, because that anomaly fires on results that all sit in the
    OK set and must never be relabelled "just re-run". But a CANCELLED lease is
    exactly why the matrix skipped, and it is cancellation-attributable — so if
    the guard computes the install path without the lease result, it sees three
    OK jobs, concludes "anomaly", and reports a cancelled lease as `failure`:
    the wrong-diff misdirection this whole area exists to remove.
    """
    outputs = render(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "skipped",
        "cancelled",
    )
    assert outputs["conclusion"] == "cancelled"
    assert outputs["passed"] == "false"


def test_the_matrix_skipped_anomaly_still_reads_as_failure_with_a_clean_lease() -> None:
    # The other side of the guard: nothing cancelled anywhere, so a skipped
    # matrix is a genuine misconfiguration and must not say "re-run".
    outputs = render(
        "success",
        "skipped",
        "skipped",
        "success",
        "skipped",
        "skipped",
        "success",
        "success",
        "success",
        "success",
    )
    assert outputs["conclusion"] == "failure"
    assert outputs["passed"] == "false"
