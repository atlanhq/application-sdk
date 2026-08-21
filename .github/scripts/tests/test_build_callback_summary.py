"""Tests for .github/scripts/build_callback_summary.py.

Signature: build_fallback_summary / resolve_summary_file take
(unit, integration, detect_integration, e2e, ...).

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


def test_the_script_emits_no_verdict():
    """The removal, stated as a property rather than an absence.

    `determine_conclusion` was this script's own answer to "did the connector
    tests pass", computed from four job results while the Tests Gate used nine
    plus an anomaly rule — the divergence that let a red connector run report
    green on the dispatching SDK PR. It outlived the fix only as a shim for
    workflow/script ref skew, and every fleet caller now pins the reusable at
    @main, so nothing reads it. A reintroduced verdict here is the bug class
    returning, not a new feature.
    """
    assert not hasattr(mod, "determine_conclusion")


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
    assert f"summary_file={artifact}" in content
    assert "conclusion=" not in content, (
        "the callback's verdict comes from the Tests Gate driver; an output "
        "named `conclusion` here is a second one waiting to be wired up"
    )


def test_main_renders_detect_integration_failure_in_the_body(tmp_path, monkeypatch):
    # A detection failure drops integration to a skip. main() must render that
    # as the detection failure it was, not as a clean skip — the body is now
    # this script's only product, so this is the wiring that matters.
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    fallback = tmp_path / "fallback.md"

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
            str(fallback),
        ]
    )

    assert rc == 0
    assert "suite detection failure" in fallback.read_text()


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
    assert "summary_file=" in out
    assert "conclusion=" not in out


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
        "build_callback_summary.py emits no conclusion — it builds the body "
        "only. Reading one back from it means someone reintroduced the second "
        "verdict; see that script's module docstring."
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
    """Any `needs.<job>.…` reference is empty for a job absent from `needs`.

    Empty is not one of the driver's pass states, so a missing `needs` entry
    would fail closed rather than green — but it would fail EVERY run, and the
    fix under pressure would be to drop the input rather than add the need. Pin
    both halves together.

    Matched on the job name alone rather than on `.result`, because not every
    judged value is a result: `superseded` reads `needs.<job>.outputs.…`, where
    a missing need is WORSE than fail-closed. It renders empty, empty reads as
    "the skip is unexplained", and the gate then reds every stood-down run with
    the anomaly it was told to suppress (FND-701).
    """
    job = reusable["jobs"][job_name]
    needed = set(job["needs"])
    for value in _gate_step(job)["with"].values():
        referenced = str(value).split("needs.")[1].split(".")[0]
        assert referenced in needed, (
            f"{job_name} judges `{referenced}` but does not need it, so its "
            "value is always empty"
        )
