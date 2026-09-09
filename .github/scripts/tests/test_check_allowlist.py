"""Tests for .github/scripts/check_allowlist.py."""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_allowlist

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trivy(vulns: list[dict[str, Any]], tmp_path: Path) -> Path:
    """Write a minimal Trivy JSON output file."""
    data = {"Results": [{"Vulnerabilities": vulns}]}
    p = tmp_path / "trivy.json"
    p.write_text(json.dumps(data))
    return p


def _allowlist(entries: dict[str, Any], tmp_path: Path) -> Path:
    """Write a base-allowlist.json and return its path."""
    p = tmp_path / "base-allowlist.json"
    p.write_text(json.dumps(entries))
    return p


def _vuln(vid: str, severity: str = "CRITICAL", pkg: str = "libfoo") -> dict[str, Any]:
    return {
        "VulnerabilityID": vid,
        "Severity": severity,
        "PkgName": pkg,
        "InstalledVersion": "1.0.0",
        "FixedVersion": "1.0.1",
    }


def _future(days: int = 365) -> str:
    return (date.today() + timedelta(days=days)).strftime("%Y-%m-%d")


def _past(days: int = 1) -> str:
    return (date.today() - timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGatePasses:
    def test_no_vulns_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trivy_file = _trivy([], tmp_path)
        allowlist_file = _allowlist({}, tmp_path)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FAIL_ON_FINDINGS", "true")
        (tmp_path / "_sdk" / ".security").mkdir(parents=True)
        allowlist_file.rename(tmp_path / "_sdk" / ".security" / "base-allowlist.json")
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(trivy_file)]
        ):
            try:
                check_allowlist.main()
            except SystemExit as e:
                assert e.code == 0 or e.code is None

    def test_allowlisted_cve_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trivy_file = _trivy([_vuln("CVE-2024-0001")], tmp_path)
        allowlist_data = {"CVE-2024-0001": {"expires": _future(), "reason": "test"}}
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text(json.dumps(allowlist_data))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FAIL_ON_FINDINGS", "true")
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(trivy_file)]
        ):
            # Should NOT raise SystemExit(1)
            try:
                check_allowlist.main()
            except SystemExit as e:
                assert e.code != 1, "Allowlisted CVE should not fail the gate"


class TestGateFails:
    def test_new_critical_cve_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trivy_file = _trivy([_vuln("CVE-2024-9999", "CRITICAL")], tmp_path)
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text(json.dumps({}))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FAIL_ON_FINDINGS", "true")
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(trivy_file)]
        ):
            with pytest.raises(SystemExit) as exc:
                check_allowlist.main()
        assert exc.value.code == 1

    def test_expired_allowlist_entry_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trivy_file = _trivy([_vuln("CVE-2024-0002")], tmp_path)
        allowlist_data = {"CVE-2024-0002": {"expires": _past(), "reason": "old"}}
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text(json.dumps(allowlist_data))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FAIL_ON_FINDINGS", "true")
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(trivy_file)]
        ):
            with pytest.raises(SystemExit) as exc:
                check_allowlist.main()
        assert exc.value.code == 1

    def test_non_blocking_mode_warns_not_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trivy_file = _trivy([_vuln("CVE-2024-9998", "HIGH")], tmp_path)
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text(json.dumps({}))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FAIL_ON_FINDINGS", "false")
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(trivy_file)]
        ):
            # Must not raise SystemExit(1)
            try:
                check_allowlist.main()
            except SystemExit as e:
                assert e.code != 1, "FAIL_ON_FINDINGS=false should not exit 1"


class TestGateEdgeCases:
    def test_missing_allowlist_disables_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trivy_file = _trivy([_vuln("CVE-2024-7777", "CRITICAL")], tmp_path)
        # No _sdk/.security/base-allowlist.json created
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FAIL_ON_FINDINGS", "true")
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(trivy_file)]
        ):
            # Gate disabled when allowlist missing — must not exit 1
            try:
                check_allowlist.main()
            except SystemExit as e:
                assert e.code != 1, "Missing allowlist should disable gate, not fail"

    def test_malformed_trivy_json_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad_trivy = tmp_path / "bad.json"
        bad_trivy.write_text("not valid json {{{")
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text(json.dumps({}))
        monkeypatch.chdir(tmp_path)
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(bad_trivy)]
        ):
            with pytest.raises(SystemExit) as exc:
                check_allowlist.main()
        assert exc.value.code != 0

    def test_malformed_allowlist_json_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trivy_file = _trivy([], tmp_path)
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text("not json <<<")
        monkeypatch.chdir(tmp_path)
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(trivy_file)]
        ):
            with pytest.raises(SystemExit) as exc:
                check_allowlist.main()
        assert exc.value.code != 0

    def test_missing_trivy_results_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text(json.dumps({}))
        monkeypatch.chdir(tmp_path)
        with patch(
            "sys.argv",
            ["check_allowlist.py", "--trivy-results", str(tmp_path / "missing.json")],
        ):
            with pytest.raises(SystemExit) as exc:
                check_allowlist.main()
        assert exc.value.code != 0

    def test_low_severity_not_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LOW findings in Trivy results are filtered by Trivy itself (--severity CRITICAL,HIGH).
        If they slip through, they appear as new_vulns with sev_order 9 (below HIGH threshold).
        Gate still checks them — this test confirms the script handles them without crashing."""
        low_vuln = _vuln("CVE-2024-0003", "LOW")
        trivy_file = _trivy([low_vuln], tmp_path)
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text(json.dumps({}))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("FAIL_ON_FINDINGS", "true")
        with patch(
            "sys.argv", ["check_allowlist.py", "--trivy-results", str(trivy_file)]
        ):
            # LOW in new_vulns still triggers gate failure — script should not crash
            try:
                check_allowlist.main()
            except SystemExit:
                pass  # exit code behaviour with LOW is acceptable either way


class TestMissingResultsReason:
    """The gate is the required check, so its log is where anyone debugging a
    red PR lands. Three unrelated transient failures — a build-cache write, a
    deleted PR merge ref, an artifact-service 403 — all surface here as the same
    absent file, so the message has to name which upstream job caused it."""

    def test_a_failed_build_is_named_rather_than_the_scanner(self) -> None:
        reason = check_allowlist.missing_results_reason("failure", "skipped")
        assert "image build" in reason
        assert "failure" in reason

    def test_a_skipped_build_is_not_treated_as_the_cause(self) -> None:
        """`build` is skipped whenever a prebuilt image is supplied. That is the
        normal path, not a failure, so it must not shadow the real cause."""
        reason = check_allowlist.missing_results_reason("skipped", "failure")
        assert "scan job" in reason
        assert "image build" not in reason

    def test_a_failed_scan_is_named_when_the_build_succeeded(self) -> None:
        reason = check_allowlist.missing_results_reason("success", "failure")
        assert "scan job" in reason

    def test_two_green_upstream_jobs_point_at_the_artifact_service(self) -> None:
        """Both jobs green and no results means the download failed twice —
        the one case where re-running this job alone is the right move."""
        reason = check_allowlist.missing_results_reason("success", "success")
        assert "artifact service" in reason
        assert "re-run" in reason

    def test_absent_env_does_not_claim_the_upstream_was_green(self) -> None:
        """Run outside CI the outcomes are unset, and guessing 'everything
        succeeded' would send the reader to the artifact service for a cause
        this check cannot see."""
        reason = check_allowlist.missing_results_reason("", "")
        assert "unknown" in reason
        assert "artifact service" not in reason

    def test_the_reason_is_printed_when_results_are_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """End to end: the env the workflow sets reaches the printed message."""
        sdk_sec = tmp_path / "_sdk" / ".security"
        sdk_sec.mkdir(parents=True)
        (sdk_sec / "base-allowlist.json").write_text(json.dumps({}))
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BUILD_RESULT", "failure")
        monkeypatch.setenv("SCAN_RESULT", "skipped")
        with patch(
            "sys.argv",
            ["check_allowlist.py", "--trivy-results", str(tmp_path / "missing.json")],
        ):
            with pytest.raises(SystemExit) as exc:
                check_allowlist.main()
        assert exc.value.code != 0
        assert "image build" in capsys.readouterr().out
