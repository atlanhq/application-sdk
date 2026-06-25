"""Tests for .github/scripts/parse_scan_results.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import parse_scan_results as psr


def _doc(*sevs: str) -> dict:
    return {
        "Results": [
            {
                "Vulnerabilities": [
                    {
                        "Severity": s,
                        "PkgName": "p",
                        "InstalledVersion": "1",
                        "FixedVersion": "2",
                        "VulnerabilityID": f"CVE-{i}",
                    }
                    for i, s in enumerate(sevs)
                ]
            }
        ]
    }


def test_count_by_severity():
    counts = psr.count_by_severity(_doc("CRITICAL", "HIGH", "HIGH", "low", "UNKNOWN"))
    assert counts == {"CRITICAL": 1, "HIGH": 2, "MEDIUM": 0, "LOW": 1}


def test_count_empty_or_none():
    assert psr.count_by_severity(None) == dict.fromkeys(psr.SEVERITIES, 0)
    assert psr.count_by_severity({}) == dict.fromkeys(psr.SEVERITIES, 0)


def test_build_summary_with_findings():
    image = {"CRITICAL": 1, "HIGH": 0, "MEDIUM": 2, "LOW": 0}
    fs = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 0, "LOW": 3}
    out = psr.build_summary(image, fs, "img:1")
    assert "img:1" in out
    assert "**7 findings" in out  # 1+2+1+3
    # Medium total is rendered as 2 (regression guard for the key-suffix bug).
    assert "| 🟡 Medium | 2 | 0 | **2** |" in out


def test_build_summary_no_findings():
    zero = dict.fromkeys(psr.SEVERITIES, 0)
    out = psr.build_summary(zero, zero, "img:1")
    assert "✅ **No findings**" in out


def test_main_emits_output_and_summary(tmp_path, monkeypatch):
    (tmp_path / "trivy-image-results.json").write_text(json.dumps(_doc("CRITICAL")))
    (tmp_path / "trivy-fs-results.json").write_text(json.dumps(_doc("HIGH", "MEDIUM")))
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out.txt"
    summ = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summ))
    monkeypatch.setenv("TARGET_IMAGE", "img:1")

    assert psr.main() == 0
    assert "total_actionable=3" in out.read_text()
    assert "Vulnerability Summary" in summ.read_text()


def test_main_no_files_is_zero(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert psr.main() == 0
    assert "total_actionable=0" in out.read_text()
