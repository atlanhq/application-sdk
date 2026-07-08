"""Tests for .github/scripts/build_callback_summary.py."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import build_callback_summary as mod


def test_determine_conclusion_success_and_success():
    assert mod.determine_conclusion("success", "success") == "success"


def test_determine_conclusion_success_and_skipped():
    assert mod.determine_conclusion("success", "skipped") == "success"


def test_determine_conclusion_tests_failed():
    assert mod.determine_conclusion("failure", "success") == "failure"


def test_determine_conclusion_e2e_failed():
    assert mod.determine_conclusion("success", "failure") == "failure"


def test_determine_conclusion_tests_cancelled():
    assert mod.determine_conclusion("cancelled", "skipped") == "failure"


def test_resolve_summary_file_prefers_artifact(tmp_path):
    artifact = tmp_path / "pr-comment-body.md"
    artifact.write_text("rendered report")
    fallback = tmp_path / "fallback.md"

    result = mod.resolve_summary_file(
        str(artifact), str(fallback), "success", "success", ""
    )

    assert result == str(artifact)
    assert not fallback.exists()


def test_resolve_summary_file_falls_back_when_artifact_missing(tmp_path):
    artifact = tmp_path / "missing.md"
    fallback = tmp_path / "fallback.md"

    result = mod.resolve_summary_file(
        str(artifact), str(fallback), "success", "skipped", "5 passed"
    )

    assert result == str(fallback)
    content = fallback.read_text()
    assert "**tests:** success (5 passed)" in content
    assert "**e2e:** skipped" in content


def test_build_fallback_summary_defaults_when_no_summary():
    body = mod.build_fallback_summary("failure", "success", "")
    assert "**tests:** failure (no summary)" in body


def test_main_writes_github_output(tmp_path, monkeypatch):
    artifact = tmp_path / "connector-results" / "pr-comment-body.md"
    artifact.parent.mkdir()
    artifact.write_text("rendered")
    output_file = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))

    rc = mod.main(
        [
            "--tests-result",
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


def test_main_prints_when_no_github_output(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)

    rc = mod.main(
        [
            "--tests-result",
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
    assert "summary_file=" in out
