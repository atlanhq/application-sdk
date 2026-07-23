"""Tests for .github/scripts/build_callback_summary.py.

Signature: determine_conclusion / build_fallback_summary / resolve_summary_file
take (unit, integration, detect_integration, e2e, ...).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import build_callback_summary as mod


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
