"""Tests for C001 UnpinnedActionReference check."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from suite.checks.actions_pinning import is_violation, iter_uses, main, scan_text
from suite.schema import SarifReport, derive_disposition, validate_sarif
from suite.schema.disposition import Disposition

# ── is_violation truth table ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "ref,expected",
    [
        # digest pins — not violations (40-hex git commit SHA only)
        ("actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10", False),
        # 64-hex is NOT a valid Actions pin — treated as a violation
        ("some/action@" + "a" * 64, True),
        # version tags — violations
        ("actions/checkout@v4", True),
        ("actions/checkout@v4.1.2", True),
        ("actions/setup-python@v5", True),
        # branch refs — violations
        ("someorg/someaction@main", True),
        ("someorg/someaction@develop", True),
        # atlanhq org — exempt regardless of ref
        ("atlanhq/application-sdk/.github/actions/setup-deps@main", False),
        ("atlanhq/application-sdk/.github/actions/setup-deps@v3.16.0", False),
        (
            "atlanhq/application-sdk/.github/actions/setup-deps@df4cb1c069e1874edd31b4311f1884172cec0e10",
            False,
        ),
        ("atlanhq/.github/.github/workflows/reusable.yml@main", False),
        # local refs — exempt
        ("./.github/actions/my-action", False),
        # docker refs — exempt
        ("docker://ubuntu:22.04", False),
        # templated refs — skip (can't evaluate statically)
        ("actions/checkout@${{ inputs.ref }}", False),
        # third-party reusable workflow ref — violation
        ("someorg/repo/.github/workflows/x.yml@v1", True),
    ],
)
def test_is_violation(ref: str, expected: bool) -> None:
    assert is_violation(ref) == expected


# ── iter_uses parsing ─────────────────────────────────────────────────────────


def test_iter_uses_skips_comment_line() -> None:
    text = "# uses: actions/checkout@v4\n"
    assert list(iter_uses(text, "test.yml")) == []


def test_iter_uses_inline_comment_does_not_contaminate_digest() -> None:
    """A digest pin with trailing # comment is parsed correctly (not flagged)."""
    text = "      - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6.0.3\n"
    uses = list(iter_uses(text, "test.yml"))
    assert len(uses) == 1
    assert (
        uses[0].raw_ref == "actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10"
    )


def test_iter_uses_version_pin_with_comment_is_violation() -> None:
    """A version-pinned action with trailing comment should still be flagged."""
    text = "        uses: actions/checkout@v4 # please pin this!\n"
    uses = list(iter_uses(text, "test.yml"))
    assert len(uses) == 1
    assert uses[0].raw_ref == "actions/checkout@v4"
    assert is_violation(uses[0].raw_ref) is True


def test_iter_uses_quoted_form() -> None:
    text = '      uses: "actions/checkout@v4"\n'
    uses = list(iter_uses(text, "test.yml"))
    assert len(uses) == 1
    assert uses[0].raw_ref == "actions/checkout@v4"


def test_iter_uses_line_number() -> None:
    text = "jobs:\n  steps:\n    - uses: actions/checkout@v4\n"
    uses = list(iter_uses(text, "test.yml"))
    assert uses[0].line == 3


# ── scan_text ─────────────────────────────────────────────────────────────────


def test_scan_text_finds_violation() -> None:
    text = "    - uses: actions/checkout@v4\n"
    findings = scan_text(text, ".github/workflows/test.yml")
    assert len(findings) == 1
    assert findings[0].rule_id == "C001"
    assert findings[0].line == 1


def test_scan_text_no_violation_for_digest() -> None:
    text = (
        "    - uses: actions/checkout@df4cb1c069e1874edd31b4311f1884172cec0e10 # v6\n"
    )
    assert scan_text(text, ".github/workflows/test.yml") == []


def test_scan_text_exempts_atlanhq() -> None:
    text = "    - uses: atlanhq/application-sdk/.github/actions/setup-deps@main\n"
    assert scan_text(text, ".github/workflows/test.yml") == []


# ── end-to-end via main() ─────────────────────────────────────────────────────


def test_main_exit_1_on_violation(tmp_path: Path) -> None:
    """main() exits 1 when a violation is found."""
    github = tmp_path / ".github" / "workflows"
    github.mkdir(parents=True)
    (github / "test.yml").write_text("steps:\n  - uses: actions/checkout@v4\n")
    code = main(["--root", str(tmp_path), str(tmp_path / ".github")])
    assert code == 1


def test_main_exit_0_when_clean(tmp_path: Path) -> None:
    """main() exits 0 when all actions are digest-pinned."""
    github = tmp_path / ".github" / "workflows"
    github.mkdir(parents=True)
    digest = "df4cb1c069e1874edd31b4311f1884172cec0e10"
    (github / "test.yml").write_text(f"steps:\n  - uses: actions/checkout@{digest}\n")
    code = main(["--root", str(tmp_path), str(tmp_path / ".github")])
    assert code == 0


def test_main_sarif_output_validates(tmp_path: Path) -> None:
    """Emitted SARIF validates against the official schema."""
    github = tmp_path / ".github" / "workflows"
    github.mkdir(parents=True)
    (github / "test.yml").write_text("steps:\n  - uses: actions/checkout@v4\n")
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


def test_main_c001_result_is_failing(tmp_path: Path) -> None:
    """C001 violation produces a FAILING disposition (exit 1)."""
    github = tmp_path / ".github" / "workflows"
    github.mkdir(parents=True)
    (github / "test.yml").write_text("steps:\n  - uses: actions/checkout@v4\n")
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
    failing = [
        r
        for r in report.runs[0].results
        if derive_disposition(r) == Disposition.FAILING
    ]
    assert len(failing) == 1
    assert failing[0].rule_id == "C001"
