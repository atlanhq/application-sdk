"""Tests for the bootstrap command and its helpers.

Covers _bootstrap_file and _ensure_gitignore_entry in isolation, and the full
_cmd_bootstrap dispatch (SKILL.md + conformance.yaml + .gitignore) via the CLI
main() entrypoint so the tests exercise the same code path a caller would use.
"""

from __future__ import annotations

import pathlib

import pytest
from conformance.cli import (
    _CONFORMANCE_WORKFLOW,
    _SKILL_MD,
    _bootstrap_file,
    _cmd_bootstrap,
    _ensure_gitignore_entry,
)

# ---------------------------------------------------------------------------
# _bootstrap_file
# ---------------------------------------------------------------------------


def test_bootstrap_file_creates_new(tmp_path: pathlib.Path) -> None:
    dest = tmp_path / "sub" / "SKILL.md"
    _bootstrap_file(dest, _SKILL_MD, force=False)
    assert dest.read_text() == _SKILL_MD


def test_bootstrap_file_creates_parent_dirs(tmp_path: pathlib.Path) -> None:
    dest = tmp_path / "a" / "b" / "c" / "file.md"
    _bootstrap_file(dest, "content", force=False)
    assert dest.exists()


def test_bootstrap_file_skips_existing_without_force(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "SKILL.md"
    dest.write_text("original")
    _bootstrap_file(dest, "new content", force=False)
    assert dest.read_text() == "original"
    assert "already installed" in capsys.readouterr().out


def test_bootstrap_file_overwrites_with_force(tmp_path: pathlib.Path) -> None:
    dest = tmp_path / "SKILL.md"
    dest.write_text("original")
    _bootstrap_file(dest, "new content", force=True)
    assert dest.read_text() == "new content"


def test_bootstrap_file_prints_installed_for_new(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "SKILL.md"
    _bootstrap_file(dest, "content", force=False)
    assert "installed" in capsys.readouterr().out


def test_bootstrap_file_prints_updated_for_overwrite(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dest = tmp_path / "SKILL.md"
    dest.write_text("old")
    _bootstrap_file(dest, "new", force=True)
    assert "updated" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# _ensure_gitignore_entry
# ---------------------------------------------------------------------------


def test_gitignore_appends_entry_to_existing_file(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("# existing\n*.pyc\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert "remediation/" in gi.read_text()


def test_gitignore_creates_file_if_absent(tmp_path: pathlib.Path) -> None:
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert (tmp_path / ".gitignore").read_text().strip() == "remediation/"


def test_gitignore_does_not_duplicate_existing_entry(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    gi.write_text("remediation/\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert gi.read_text().count("remediation/") == 1


def test_gitignore_does_not_clobber_existing_content(tmp_path: pathlib.Path) -> None:
    gi = tmp_path / ".gitignore"
    original = "# my rules\n*.log\n.env\n"
    gi.write_text(original)
    _ensure_gitignore_entry(tmp_path, "remediation/")
    result = gi.read_text()
    assert result.startswith(original)
    assert "remediation/" in result


def test_gitignore_no_op_prints_ok(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".gitignore").write_text("remediation/\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert "ok" in capsys.readouterr().out


def test_gitignore_append_prints_appended(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / ".gitignore").write_text("*.pyc\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert "appended" in capsys.readouterr().out


def test_gitignore_entry_match_is_exact_line(tmp_path: pathlib.Path) -> None:
    """'remediation/' must not be considered present just because 'remediation/runs/' exists."""
    gi = tmp_path / ".gitignore"
    gi.write_text("remediation/runs/\n")
    _ensure_gitignore_entry(tmp_path, "remediation/")
    assert gi.read_text().count("remediation/") == 2


# ---------------------------------------------------------------------------
# _cmd_bootstrap (full integration)
# ---------------------------------------------------------------------------


def test_cmd_bootstrap_writes_skill_md(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    dest = tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md"
    assert dest.read_text() == _SKILL_MD


def test_cmd_bootstrap_writes_conformance_workflow(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    dest = tmp_path / ".github" / "workflows" / "conformance.yaml"
    assert dest.read_text() == _CONFORMANCE_WORKFLOW


def test_cmd_bootstrap_adds_remediation_to_gitignore(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    assert "remediation/" in (tmp_path / ".gitignore").read_text()


def test_cmd_bootstrap_idempotent_without_force(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    skill_mtime = (
        (tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md").stat().st_mtime
    )
    wf_mtime = (tmp_path / ".github" / "workflows" / "conformance.yaml").stat().st_mtime
    _cmd_bootstrap([])
    assert (
        tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md"
    ).stat().st_mtime == skill_mtime
    assert (
        tmp_path / ".github" / "workflows" / "conformance.yaml"
    ).stat().st_mtime == wf_mtime


def test_cmd_bootstrap_force_overwrites(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    skill = tmp_path / ".claude" / "skills" / "remediate" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("old")
    _cmd_bootstrap(["--force"])
    assert skill.read_text() == _SKILL_MD


def test_cmd_bootstrap_gitignore_not_duplicated_on_rerun(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    _cmd_bootstrap([])
    assert (tmp_path / ".gitignore").read_text().count("remediation/") == 1


def test_cmd_bootstrap_preserves_existing_gitignore_content(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    gi = tmp_path / ".gitignore"
    gi.write_text("*.pyc\n.env\n")
    _cmd_bootstrap([])
    content = gi.read_text()
    assert "*.pyc" in content
    assert ".env" in content
    assert "remediation/" in content


def test_cmd_bootstrap_returns_zero(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert _cmd_bootstrap([]) == 0


def test_conformance_workflow_contains_event_name(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bundled workflow uses event_name, not the stale sdk-ref input."""
    monkeypatch.chdir(tmp_path)
    _cmd_bootstrap([])
    content = (tmp_path / ".github" / "workflows" / "conformance.yaml").read_text()
    assert "event_name:" in content
    assert "sdk-ref" not in content
