"""Tests for .github/scripts/build_callback_summary.py.

Signature: determine_conclusion / build_fallback_summary / resolve_summary_file
take (unit, integration, detect_integration, e2e, ...).

Also guards the WIRING of the report-to-sdk job that runs this script, because
the bug this file's script was at the centre of was not a bug in any function
here — it was that the callback answered "did the connector tests pass?" from a
different, smaller input set than the Tests Gate did, and so reported success on
the dispatching application-sdk PR for a connector run whose own gate was red.
The guards below pin the two consumers to one driver and one input set.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import build_callback_summary as mod

_REPO_ROOT = Path(__file__).resolve().parents[3]
_REUSABLE = _REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"


def test_determine_conclusion_all_success():
    assert (
        mod.determine_conclusion("success", "success", "success", "success")
        == "success"
    )


def test_determine_conclusion_integration_skipped_is_success():
    # Integration skipped (PR / no suite) is a pass; on a PR detect-integration
    # is skipped too — also a pass.
    assert (
        mod.determine_conclusion("success", "skipped", "skipped", "success")
        == "success"
    )


def test_determine_conclusion_integration_skipped_no_suite_is_success():
    # Non-PR, no integration suite: detect-integration succeeds, integration
    # skips cleanly — a pass.
    assert (
        mod.determine_conclusion("success", "skipped", "success", "success")
        == "success"
    )


def test_determine_conclusion_e2e_skipped_is_success():
    assert (
        mod.determine_conclusion("success", "success", "success", "skipped")
        == "success"
    )


def test_determine_conclusion_unit_failed():
    assert (
        mod.determine_conclusion("failure", "success", "success", "success")
        == "failure"
    )


def test_determine_conclusion_integration_failed():
    assert (
        mod.determine_conclusion("success", "failure", "success", "success")
        == "failure"
    )


def test_determine_conclusion_detect_integration_failed():
    # A detection failure drops integration to a skip; the callback must still
    # report failure rather than a silent success.
    assert (
        mod.determine_conclusion("success", "skipped", "failure", "success")
        == "failure"
    )


def test_determine_conclusion_e2e_failed():
    assert (
        mod.determine_conclusion("success", "success", "success", "failure")
        == "failure"
    )


def test_determine_conclusion_unit_cancelled():
    assert (
        mod.determine_conclusion("cancelled", "skipped", "skipped", "skipped")
        == "failure"
    )


def test_resolve_summary_file_prefers_artifact(tmp_path):
    artifact = tmp_path / "pr-comment-body.md"
    artifact.write_text("rendered report")
    fallback = tmp_path / "fallback.md"

    result = mod.resolve_summary_file(
        str(artifact), str(fallback), "success", "success", "success", "success", "", ""
    )

    assert result == str(artifact)
    assert not fallback.exists()


def test_resolve_summary_file_falls_back_when_artifact_missing(tmp_path):
    artifact = tmp_path / "missing.md"
    fallback = tmp_path / "fallback.md"

    result = mod.resolve_summary_file(
        str(artifact),
        str(fallback),
        "success",
        "skipped",
        "success",
        "skipped",
        "12 passed",
        "",
    )

    assert result == str(fallback)
    content = fallback.read_text()
    assert "**unit:** success (12 passed)" in content
    assert "**integration:** skipped (no summary)" in content
    assert "**e2e:** skipped" in content


def test_build_fallback_summary_defaults_when_no_summary():
    body = mod.build_fallback_summary(
        "failure", "success", "success", "success", "", "3 passed"
    )
    assert "**unit:** failure (no summary)" in body
    assert "**integration:** success (3 passed)" in body


def test_build_fallback_summary_detect_integration_failed():
    # When detection failed, the integration line reports that rather than a
    # misleading "skipped".
    body = mod.build_fallback_summary(
        "success", "skipped", "failure", "skipped", "", ""
    )
    assert "suite detection failure" in body
    assert "**integration:** skipped" not in body


def test_main_writes_github_output(tmp_path, monkeypatch):
    artifact = tmp_path / "connector-results" / "pr-comment-body.md"
    artifact.parent.mkdir()
    artifact.write_text("rendered")
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    rc = mod.main(
        [
            "--unit-result",
            "success",
            "--integration-result",
            "success",
            "--detect-integration-result",
            "success",
            "--e2e-result",
            "success",
            "--artifact-summary-path",
            str(artifact),
            "--fallback-path",
            str(tmp_path / "fallback.md"),
        ]
    )

    assert rc == 0
    content = output_file.read_text()
    assert "conclusion=success" in content
    assert f"summary_file={artifact}" in content


def test_main_detect_integration_failure_reports_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    rc = mod.main(
        [
            "--unit-result",
            "success",
            "--integration-result",
            "skipped",
            "--detect-integration-result",
            "failure",
            "--e2e-result",
            "skipped",
            "--artifact-summary-path",
            str(tmp_path / "missing.md"),
            "--fallback-path",
            str(tmp_path / "fallback.md"),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "conclusion=failure" in out


def test_main_prints_when_no_github_output(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    rc = mod.main(
        [
            "--unit-result",
            "failure",
            "--integration-result",
            "skipped",
            "--detect-integration-result",
            "skipped",
            "--e2e-result",
            "skipped",
            "--artifact-summary-path",
            str(tmp_path / "missing.md"),
            "--fallback-path",
            str(tmp_path / "fallback.md"),
        ]
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "conclusion=failure" in out
    assert "summary_file=" in out


# --- report-to-sdk wiring --------------------------------------------------
# The regression guards for the divergence itself. `Connector E2E run / <app>`
# on the dispatching SDK PR reported success while the connector's own Tests
# Gate reported failure, because the two verdicts were computed independently:
# the gate from six job results plus an anomaly rule, the callback from four.
# Nothing in either implementation was wrong on its own terms — the defect was
# that there were two. These assert there is one.

_GATE_ACTION = "atlanhq/application-sdk/.github/actions/verify-test-gate@main"


@pytest.fixture(scope="module")
def reusable() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(_REUSABLE.read_text(encoding="utf-8"))


def _gate_step(job: dict) -> dict:  # type: ignore[type-arg]
    """The step in `job` that evaluates the shared Tests Gate driver."""
    steps = [s for s in job["steps"] if s.get("uses") == _GATE_ACTION]
    assert len(steps) == 1, (
        f"expected exactly one {_GATE_ACTION} step, found {len(steps)}. Both the "
        "gate job and the cross-repo callback must reach the verdict through "
        "this action and nothing else."
    )
    return steps[0]


def test_the_callback_reports_the_gates_verdict_not_its_own(reusable) -> None:
    """The fix, stated directly.

    `--conclusion` must be fed from the shared gate driver. Reading it back from
    the summary-building step (which is what shipped, and which cannot see
    discover-e2e or the image legs at all) is the bug: it lets a red connector
    run complete the SDK-side check run as green.
    """
    job = reusable["jobs"]["report-to-sdk"]
    gate_id = _gate_step(job)["id"]

    complete = next(
        s for s in job["steps"] if "complete_check_run.py" in str(s.get("run", ""))
    )
    assert f"steps.{gate_id}.outputs.conclusion" in complete["run"], (
        "the check run on application-sdk must be completed with the Tests Gate "
        "driver's conclusion. Any other source is a second implementation of "
        "'did the connector tests pass', and the two will drift."
    )
    assert "steps.report.outputs.conclusion" not in complete["run"], (
        "build_callback_summary.py's conclusion is a deprecated back-compat "
        "shim, not the verdict — see that script's module docstring."
    )


def test_the_callback_and_the_gate_judge_the_same_inputs(reusable) -> None:
    """One driver is only one verdict if both callers feed it the same thing.

    A caller that omits an input gets its default ("skipped" — a pass), so
    dropping `discover-e2e-result` here would silently restore the old blind
    spot while still routing through the shared action.
    """
    callback_inputs = _gate_step(reusable["jobs"]["report-to-sdk"])["with"]
    gate_inputs = _gate_step(reusable["jobs"]["tests-passed"])["with"]

    assert callback_inputs == gate_inputs, (
        "the cross-repo callback and the required Tests Gate must pass identical "
        "inputs to the gate driver; they disagree on "
        f"{sorted(set(callback_inputs) ^ set(gate_inputs)) or 'the values'}"
    )


@pytest.mark.parametrize("job_name", ["report-to-sdk", "tests-passed"])
def test_every_judged_job_is_also_needed(reusable, job_name: str) -> None:
    """`needs.<job>.result` is empty for a job absent from `needs`.

    Empty is not one of the driver's pass states, so a missing `needs` entry
    would fail closed rather than green — but it would fail EVERY run, and the
    fix under pressure would be to drop the input rather than add the need. Pin
    both halves together.
    """
    job = reusable["jobs"][job_name]
    needed = set(job["needs"])
    for value in _gate_step(job)["with"].values():
        referenced = str(value).split("needs.")[1].split(".result")[0]
        assert referenced in needed, (
            f"{job_name} judges `{referenced}` but does not need it, so its "
            "result is always empty"
        )
