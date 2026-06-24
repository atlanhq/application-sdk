"""Tests for C004 ForceExternalRuntimeDrift check."""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.force_external_runtime_drift import (
    discover,
    main,
    scan_text,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema import SarifReport, derive_disposition, validate_sarif
from conformance.suite.schema.disposition import Disposition, EnforcementTier

_WORKFLOW = """\
name: Tests
jobs:
  tests:
    steps:
      - uses: atlanhq/application-sdk/.github/actions/connector-integration-tests@main
        with:
          app-name: postgres
          force-external-runtime: "true"
"""


# ── detection ─────────────────────────────────────────────────────────────────


def test_flags_quoted_true() -> None:
    findings = scan_text(_WORKFLOW, ".github/workflows/tests.yaml")
    assert len(findings) == 1
    assert findings[0].rule_id == "C004"
    assert findings[0].line == 8
    assert not findings[0].suppressed


def test_flags_bare_true() -> None:
    text = "        force-external-runtime: true\n"
    findings = scan_text(text, ".github/workflows/tests.yaml")
    assert len(findings) == 1
    assert findings[0].rule_id == "C004"
    assert findings[0].column == 9  # 1-based, after 8 spaces of indent


def test_flags_single_quoted_true() -> None:
    text = "          force-external-runtime: 'true'\n"
    assert len(scan_text(text, "w.yaml")) == 1


def test_does_not_flag_false() -> None:
    text = '          force-external-runtime: "false"\n'
    assert scan_text(text, "w.yaml") == []


def test_does_not_flag_bare_false() -> None:
    text = "          force-external-runtime: false\n"
    assert scan_text(text, "w.yaml") == []


def test_does_not_flag_commented_line() -> None:
    text = '      # force-external-runtime: "true"  # left here on purpose\n'
    assert scan_text(text, "w.yaml") == []


def test_flags_value_with_trailing_comment() -> None:
    text = '          force-external-runtime: "true"  # TODO migrate to embedded\n'
    assert len(scan_text(text, "w.yaml")) == 1


# ── suppression ───────────────────────────────────────────────────────────────


def test_suppressed_same_line() -> None:
    text = (
        '          force-external-runtime: "true"  '
        "# conformance: ignore[C004] still on SDK 3.12\n"
    )
    findings = scan_text(text, "w.yaml")
    assert len(findings) == 1
    assert findings[0].suppressed is True
    assert "SDK 3.12" in (findings[0].suppression_justification or "")


def test_suppressed_line_above() -> None:
    text = (
        "          # conformance: ignore[C004] pinned to old SDK\n"
        '          force-external-runtime: "true"\n'
    )
    findings = scan_text(text, "w.yaml")
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_wildcard_ignore_suppresses() -> None:
    text = '          force-external-runtime: "true"  # conformance: ignore reason\n'
    findings = scan_text(text, "w.yaml")
    assert len(findings) == 1
    assert findings[0].suppressed is True


def test_unrelated_rule_id_does_not_suppress() -> None:
    text = (
        '          force-external-runtime: "true"  # conformance: ignore[C001] nope\n'
    )
    findings = scan_text(text, "w.yaml")
    assert len(findings) == 1
    assert findings[0].suppressed is False


# ── discovery ─────────────────────────────────────────────────────────────────


def test_discover_finds_workflow_files(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yaml").write_text(_WORKFLOW)
    (wf / "release.yml").write_text("name: r\n")
    # an action file must NOT be discovered (flag lives in caller workflows)
    action = tmp_path / ".github" / "actions" / "x"
    action.mkdir(parents=True)
    (action / "action.yaml").write_text("name: a\n")
    found = {p.name for p in discover(tmp_path)}
    assert found == {"tests.yaml", "release.yml"}


# ── registry ──────────────────────────────────────────────────────────────────


def test_rule_registered_as_warn_app() -> None:
    rule = get_rule("C004")
    assert rule.name == "ForceExternalRuntimeDrift"
    assert rule.tier == EnforcementTier.WARN


# ── end-to-end via main() ─────────────────────────────────────────────────────


def test_main_exit_0_warn_only(tmp_path: Path) -> None:
    """C004 is WARN — a finding must NOT fail the gate (exit 0)."""
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yaml").write_text(_WORKFLOW)
    code = main(["--root", str(tmp_path), str(tmp_path / ".github")])
    assert code == 0


def test_main_sarif_has_c004_warning(tmp_path: Path) -> None:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "tests.yaml").write_text(_WORKFLOW)
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / ".github"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    validate_sarif(report)
    warnings = [
        r
        for r in report.runs[0].results
        if derive_disposition(r) == Disposition.WARNING
    ]
    assert any(r.rule_id == "C004" for r in warnings)
