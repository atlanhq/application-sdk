"""Tests for .github/scripts/security_scan_create_linear.py (per-severity)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

import security_scan_create_linear as ssl


def _finding(vid: str, severity: str, pkg: str = "libfoo") -> dict[str, Any]:
    return {
        "id": vid,
        "severity": severity,
        "package": pkg,
        "version": "1.0.0",
        "fixed": "1.0.1",
        "title": "",
        "source": "trivy",
        "scanner_target": "trivy-image-results.json",
    }


# ---------------------------------------------------------------------------
# build_description
# ---------------------------------------------------------------------------


def test_marker_lists_only_this_severitys_ids():
    bucket = [_finding("CVE-1", "HIGH"), _finding("CVE-2", "HIGH")]
    desc = ssl.build_description(bucket, "HIGH", "img:1", "2026-06-25", "http://run")
    ids = ssl.extract_vuln_ids_from_description(desc)
    assert ids == {"CVE-1", "CVE-2"}
    assert "High" in desc


def test_description_severity_heading():
    desc = ssl.build_description(
        [_finding("CVE-9", "CRITICAL")], "CRITICAL", "img:1", "2026-06-25", "http://run"
    )
    assert "Critical" in desc


# ---------------------------------------------------------------------------
# collect_findings (reads trivy json from cwd)
# ---------------------------------------------------------------------------


def test_collect_findings_dedups_and_sorts(tmp_path, monkeypatch):
    image = {
        "Results": [
            {
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-LOW",
                        "Severity": "LOW",
                        "PkgName": "a",
                        "InstalledVersion": "1",
                        "FixedVersion": "2",
                    },
                    {
                        "VulnerabilityID": "CVE-CRIT",
                        "Severity": "CRITICAL",
                        "PkgName": "b",
                        "InstalledVersion": "1",
                        "FixedVersion": "2",
                    },
                ]
            }
        ]
    }
    (tmp_path / "trivy-image-results.json").write_text(json.dumps(image))
    monkeypatch.chdir(tmp_path)
    findings = ssl.collect_findings()
    # CRITICAL sorts before LOW.
    assert [f["id"] for f in findings] == ["CVE-CRIT", "CVE-LOW"]


# ---------------------------------------------------------------------------
# main — per-severity ticket creation (gql mocked)
# ---------------------------------------------------------------------------


class _FakeGql:
    """Routes Linear GraphQL queries by substring and records issueCreate."""

    def __init__(self):
        self.created: list[dict] = []
        self._counter = 0

    def __call__(self, query: str, variables: dict) -> dict:
        if "issueLabels" in query:
            return {
                "issueLabels": {
                    "nodes": [
                        {
                            "id": "label-1",
                            "name": "vulnerabilities",
                            "team": {"id": "team-1"},
                        }
                    ]
                }
            }
        if "OpenLabelled" in query or "issues(" in query:
            return {"issues": {"pageInfo": {"hasNextPage": False}, "nodes": []}}
        if "TeamStates" in query or "team(id" in query:
            return {
                "team": {
                    "states": {
                        "nodes": [
                            {"id": "st-backlog", "name": "Backlog", "type": "backlog"}
                        ]
                    }
                }
            }
        if "issueCreate" in query:
            self._counter += 1
            self.created.append(variables["input"])
            ident = f"BLDX-{self._counter}"
            return {
                "issueCreate": {
                    "success": True,
                    "issue": {
                        "id": f"id-{self._counter}",
                        "identifier": ident,
                        "url": f"http://x/{ident}",
                    },
                }
            }
        raise AssertionError(f"unexpected query: {query[:60]}")


def _env(monkeypatch):
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.setenv("LINEAR_TEAM_ID", "team-1")
    monkeypatch.setenv("LINEAR_PROJECT_ID", "proj-1")
    monkeypatch.setenv("SCAN_DATE", "2026-06-25")
    monkeypatch.setenv("TARGET_IMAGE", "img:1")


def test_main_creates_one_issue_per_severity(tmp_path, monkeypatch):
    _env(monkeypatch)
    findings = [
        _finding("CVE-C", "CRITICAL"),
        _finding("CVE-H1", "HIGH"),
        _finding("CVE-H2", "HIGH"),
        _finding("CVE-M", "MEDIUM"),
    ]
    monkeypatch.setattr(ssl, "collect_findings", lambda: findings)
    fake = _FakeGql()
    monkeypatch.setattr(ssl, "gql", fake)
    out = tmp_path / "out.txt"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))

    assert ssl.main() == 0

    # One issue per distinct severity (CRITICAL, HIGH, MEDIUM) = 3.
    assert len(fake.created) == 3
    priorities = [c["priority"] for c in fake.created]
    # Ordered CRITICAL(1), HIGH(2), MEDIUM(3).
    assert priorities == [1, 2, 3]

    emitted = out.read_text()
    assert "new_issues=" in emitted
    payload = json.loads(emitted.split("new_issues=", 1)[1].strip())
    severities = {e["severity"] for e in payload}
    assert severities == {"CRITICAL", "HIGH", "MEDIUM"}


def test_main_dedups_against_open_issue(tmp_path, monkeypatch):
    _env(monkeypatch)
    findings = [_finding("CVE-C", "CRITICAL")]
    monkeypatch.setattr(ssl, "collect_findings", lambda: findings)

    class _Covered(_FakeGql):
        def __call__(self, query, variables):
            if "OpenLabelled" in query or "issues(" in query:
                return {
                    "issues": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "id": "i1",
                                "identifier": "BLDX-OLD",
                                "url": "http://x",
                                "title": "t",
                                "description": "<!-- vuln-ids: CVE-C -->",
                                "state": {"type": "started"},
                            }
                        ],
                    }
                }
            return super().__call__(query, variables)

    fake = _Covered()
    monkeypatch.setattr(ssl, "gql", fake)
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "out.txt"))

    assert ssl.main() == 0
    # Already covered → no new issue created.
    assert fake.created == []
