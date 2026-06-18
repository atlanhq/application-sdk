"""Tests for C003 GitignoreMissingEntry check.

Covers:
- Rule metadata (exists, tier=WARN, not autofixable)
- discover() always returns the .gitignore path
- Absent .gitignore → one C003 finding
- Complete .gitignore (from bootstrap) → no findings
- Missing a single required entry → one finding per missing entry
- Directory entry equivalences (.venv covers .venv/, **/node_modules/** covers node_modules/)
- Bootstrap writes .gitignore when absent; is no-op when present
"""

from __future__ import annotations

import pathlib

import pytest
from conformance.suite.checks.gitignore_entries import (
    REQUIRED_ENTRIES,
    _is_covered,
    discover,
    scan_path,
)
from conformance.suite.rules import get_rule
from conformance.suite.schema.disposition import EnforcementTier

# ---------------------------------------------------------------------------
# Rule metadata
# ---------------------------------------------------------------------------


def test_c003_rule_exists() -> None:
    rule = get_rule("C003")
    assert rule.name == "GitignoreMissingEntry"


def test_c003_tier_is_warn() -> None:
    assert get_rule("C003").tier == EnforcementTier.WARN


def test_c003_is_not_autofixable() -> None:
    assert get_rule("C003").autofixable is False


# ---------------------------------------------------------------------------
# discover()
# ---------------------------------------------------------------------------


def test_discover_returns_gitignore_path(tmp_path: pathlib.Path) -> None:
    paths = discover(tmp_path)
    assert len(paths) == 1
    assert paths[0] == tmp_path / ".gitignore"


def test_discover_returns_path_even_when_absent(tmp_path: pathlib.Path) -> None:
    paths = discover(tmp_path)
    assert not paths[0].exists()


# ---------------------------------------------------------------------------
# Absent .gitignore → one finding
# ---------------------------------------------------------------------------


def test_absent_gitignore_produces_one_finding(tmp_path: pathlib.Path) -> None:
    findings = scan_path(tmp_path / ".gitignore", tmp_path)
    assert len(findings) == 1
    assert findings[0].rule_id == "C003"


def test_absent_gitignore_finding_mentions_absent(tmp_path: pathlib.Path) -> None:
    findings = scan_path(tmp_path / ".gitignore", tmp_path)
    assert "absent" in findings[0].message.lower()


def test_absent_gitignore_finding_mentions_bootstrap(tmp_path: pathlib.Path) -> None:
    findings = scan_path(tmp_path / ".gitignore", tmp_path)
    assert "bootstrap" in findings[0].message


# ---------------------------------------------------------------------------
# Bootstrap scaffold → no findings
# ---------------------------------------------------------------------------


def _bootstrap(root: pathlib.Path) -> None:
    import os

    from conformance.cli import _cmd_bootstrap

    old = os.getcwd()
    os.chdir(root)
    try:
        _cmd_bootstrap([])
    finally:
        os.chdir(old)


def test_bootstrapped_gitignore_has_no_findings(tmp_path: pathlib.Path) -> None:
    _bootstrap(tmp_path)
    gi = tmp_path / ".gitignore"
    assert gi.exists()
    findings = scan_path(gi, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_bootstrap_does_not_overwrite_existing_gitignore(
    tmp_path: pathlib.Path,
) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("# custom\n.my-custom-entry/\n", encoding="utf-8")
    _bootstrap(tmp_path)
    assert ".my-custom-entry/" in gi.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Present but missing entries → one finding per missing entry
# ---------------------------------------------------------------------------


def test_empty_gitignore_produces_finding_per_required_entry(
    tmp_path: pathlib.Path,
) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("# empty\n", encoding="utf-8")
    findings = scan_path(gi, tmp_path)
    assert len(findings) == len(REQUIRED_ENTRIES)
    assert all(f.rule_id == "C003" for f in findings)


def test_finding_per_missing_entry_names_the_entry(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("# nothing useful\n", encoding="utf-8")
    findings = scan_path(gi, tmp_path)
    messages = [f.message for f in findings]
    for entry in REQUIRED_ENTRIES:
        assert any(entry in m for m in messages), f"no finding for {entry!r}"


def test_single_missing_entry_produces_one_finding(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    # Write all required entries except .claude/worktrees/
    content = "\n".join(e for e in REQUIRED_ENTRIES if e != ".claude/worktrees/")
    gi.write_text(content + "\n", encoding="utf-8")
    findings = scan_path(gi, tmp_path)
    assert len(findings) == 1
    assert ".claude/worktrees/" in findings[0].message


def test_no_findings_when_all_required_entries_present(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("\n".join(REQUIRED_ENTRIES) + "\n", encoding="utf-8")
    findings = scan_path(gi, tmp_path)
    assert findings == []


def test_comments_and_blank_lines_are_ignored(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    # All required entries plus noise lines
    lines = list(REQUIRED_ENTRIES) + ["# a comment", "", "   "]
    gi.write_text("\n".join(lines) + "\n", encoding="utf-8")
    findings = scan_path(gi, tmp_path)
    assert findings == []


# ---------------------------------------------------------------------------
# Equivalence rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "required,equivalent",
    [
        (".venv/", ".venv"),
        (".vscode/", ".vscode"),
        (".idea/", ".idea"),
        ("__pycache__/", "__pycache__"),
        (".pytest_cache/", ".pytest_cache"),
        (".mypy_cache/", ".mypy_cache"),
        (".ruff_cache/", ".ruff_cache"),
        ("htmlcov/", "htmlcov"),
        ("dist/", "dist"),
        ("node_modules/", "**/node_modules/**"),
        (".atlan/", ".atlan"),
        (".claude/worktrees/", ".claude/worktrees"),
        ("remediation/", "remediation"),
    ],
)
def test_equivalent_form_is_accepted(required: str, equivalent: str) -> None:
    assert _is_covered(required, frozenset({equivalent}))


def test_exact_form_always_accepted() -> None:
    for entry in REQUIRED_ENTRIES:
        assert _is_covered(
            entry, frozenset({entry})
        ), f"exact match failed for {entry!r}"


def test_unrelated_entry_does_not_cover() -> None:
    assert not _is_covered(".venv/", frozenset({"venv/", ".venv2"}))


def test_venv_no_slash_covers_venv_slash(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    content = "\n".join(e if e != ".venv/" else ".venv" for e in REQUIRED_ENTRIES)
    gi.write_text(content + "\n", encoding="utf-8")
    findings = scan_path(gi, tmp_path)
    assert findings == [], [f.message for f in findings]


def test_node_modules_recursive_glob_covers_slash_form(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    content = "\n".join(
        e if e != "node_modules/" else "**/node_modules/**" for e in REQUIRED_ENTRIES
    )
    gi.write_text(content + "\n", encoding="utf-8")
    findings = scan_path(gi, tmp_path)
    assert findings == [], [f.message for f in findings]
