"""Tests for .github/scripts/dispatch_vuln_triage.py."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import dispatch_vuln_triage as dvt


def _ok(*_args, **_kwargs):
    return subprocess.CompletedProcess(args=[], returncode=0)


def test_parse_issues_empty():
    assert dvt.parse_issues(None) == []
    assert dvt.parse_issues("") == []
    assert dvt.parse_issues("  ") == []
    assert dvt.parse_issues("[]") == []


def test_parse_issues_valid():
    raw = '[{"identifier": "BLDX-1", "severity": "CRITICAL"}]'
    assert dvt.parse_issues(raw) == [{"identifier": "BLDX-1", "severity": "CRITICAL"}]


def test_parse_issues_invalid_json():
    with pytest.raises(SystemExit):
        dvt.parse_issues("{not json")


def test_parse_issues_not_a_list():
    with pytest.raises(SystemExit):
        dvt.parse_issues('{"identifier": "BLDX-1"}')


def test_dispatch_runs_once_per_issue():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    issues = [
        {"identifier": "BLDX-1", "severity": "CRITICAL"},
        {"identifier": "BLDX-2", "severity": "HIGH"},
    ]
    n = dvt.dispatch(issues, ref="main", runner=runner)
    assert n == 2
    assert len(calls) == 2
    # Each command carries the ticket and severity as -f args.
    assert "ticket=BLDX-1" in calls[0]
    assert "severity=CRITICAL" in calls[0]
    assert "ticket=BLDX-2" in calls[1]
    assert "severity=HIGH" in calls[1]
    assert "--ref" in calls[0] and "main" in calls[0]


def test_dispatch_missing_identifier_raises():
    with pytest.raises(SystemExit):
        dvt.dispatch([{"severity": "HIGH"}], ref="main", runner=_ok)


def test_dispatch_nonzero_exit_raises():
    def runner(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=1)

    with pytest.raises(SystemExit):
        dvt.dispatch(
            [{"identifier": "BLDX-1", "severity": "HIGH"}], ref="main", runner=runner
        )


def test_dispatch_uses_custom_workflow_name():
    calls = []

    def runner(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    dvt.dispatch(
        [{"identifier": "BLDX-1", "severity": "LOW"}],
        ref="feature",
        workflow="custom.yml",
        runner=runner,
    )
    assert "custom.yml" in calls[0]
