"""Meta-tests for the P-series prescription checks (P001).

These checks are shipped in the conformance package and fanned out across the
fleet — a buggy check false-positives across hundreds of apps and triggers
spurious remediations (BLDX-1394).  So each rule is tested to fire *exactly*
when it should and stay silent otherwise: both false positives and false
negatives are guarded.
"""

from __future__ import annotations

import json
from pathlib import Path

from conformance.suite.checks.prescriptions import main, scan_text
from conformance.suite.rules import get_rule
from conformance.suite.schema import SarifReport, derive_disposition, validate_sarif
from conformance.suite.schema.disposition import Disposition, EnforcementTier


def _ids(src: str) -> list[str]:
    return [f.rule_id for f in scan_text(src, "x.py")]


# ── P001 UnboundedContractFields ───────────────────────────────────────────────


def test_p001_fires_on_unbounded_input() -> None:
    src = "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    findings = scan_text(src, "x.py")
    assert [f.rule_id for f in findings] == ["P001"]
    assert findings[0].line == 1


def test_p001_fires_with_mixin_and_other_bases() -> None:
    src = "class O(PublishInputMixin, Output, allow_unbounded_fields=True):\n    x: str = ''\n"
    assert _ids(src) == ["P001"]


def test_p001_silent_on_plain_contract() -> None:
    assert _ids("class MyInput(Input):\n    pass\n") == []


def test_p001_silent_when_explicitly_false() -> None:
    src = "class MyInput(Input, allow_unbounded_fields=False):\n    pass\n"
    assert _ids(src) == []


def test_p001_silent_on_unrelated_class_keyword() -> None:
    # A different class keyword (e.g. metaclass) must not trip the rule.
    src = "class MyInput(Input, metaclass=ABCMeta):\n    pass\n"
    assert _ids(src) == []


def test_p001_suppressed_by_directive_line_above() -> None:
    src = (
        "# conformance: ignore[P001] generic cleanup payload — fields vary by app\n"
        "class CleanupInput(Input, allow_unbounded_fields=True):\n"
        "    pass\n"
    )
    findings = scan_text(src, "x.py")
    assert len(findings) == 1
    assert findings[0].rule_id == "P001"
    assert findings[0].suppressed is True
    assert "cleanup" in (findings[0].suppression_justification or "")


def test_p001_suppressed_by_trailing_directive() -> None:
    src = "class CleanupInput(Input, allow_unbounded_fields=True):  # conformance: ignore[P001] legit\n    pass\n"
    findings = scan_text(src, "x.py")
    assert findings[0].suppressed is True


# ── tier / disposition / gate ───────────────────────────────────────────────────


def test_p001_is_block_tier() -> None:
    # P-series is suppress-only / above-the-bar (BLDX-1428).
    assert get_rule("P001").tier is EnforcementTier.BLOCK


def test_p001_block_violation_fails_the_gate(tmp_path: Path) -> None:
    """An unsuppressed P001 (BLOCK) declaration fails the gate (exit 1)."""
    (tmp_path / "m.py").write_text(
        "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 1


def test_p001_suppressed_declaration_keeps_gate_green(tmp_path: Path) -> None:
    """A justified inline suppression clears the gate (exit 0) — suppress-only path."""
    (tmp_path / "m.py").write_text(
        "# conformance: ignore[P001] generic cleanup payload\n"
        "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    )
    code = main(["--root", str(tmp_path), str(tmp_path / "m.py")])
    assert code == 0


def test_p001_result_is_failing_disposition(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    dispositions = [derive_disposition(r) for r in report.runs[0].results]
    assert dispositions == [Disposition.FAILING]


def test_p001_sarif_output_validates(tmp_path: Path) -> None:
    (tmp_path / "m.py").write_text(
        "class MyInput(Input, allow_unbounded_fields=True):\n    pass\n"
    )
    sarif_file = tmp_path / "out.sarif"
    main(
        [
            "--root",
            str(tmp_path),
            str(tmp_path / "m.py"),
            "--sarif-output",
            str(sarif_file),
        ]
    )
    report = SarifReport.model_validate(json.loads(sarif_file.read_text()))
    validate_sarif(report)
