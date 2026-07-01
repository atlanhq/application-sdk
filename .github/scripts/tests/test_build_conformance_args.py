"""Tests for .github/scripts/build_conformance_args.py."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from build_conformance_args import build_args, main


def test_minimal() -> None:
    result = build_args("C", "ci")
    assert result == ["--repo", ".", "--series", "C", "--output", "ci.sarif"]


def test_with_exclude() -> None:
    result = build_args("C", "ci", exclude="tools/,docs/")
    assert "--exclude" in result
    idx = result.index("--exclude")
    assert result[idx + 1] == "tools/,docs/"


def test_exit_zero_flag() -> None:
    result = build_args("C", "ci", exit_zero=True)
    assert "--exit-zero" in result


def test_no_exit_zero_by_default() -> None:
    result = build_args("C", "ci")
    assert "--exit-zero" not in result


def test_empty_exclude_not_added() -> None:
    result = build_args("C", "ci", exclude="")
    assert "--exclude" not in result


def test_slug_used_for_sarif_filename() -> None:
    result = build_args("E", "error-handling")
    assert "--output" in result
    idx = result.index("--output")
    assert result[idx + 1] == "error-handling.sarif"


def test_main_reads_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("EXCLUDE_PATHS", "tools/")
    monkeypatch.setenv("EXIT_ZERO", "true")
    main(["--series", "C", "--slug", "ci"])
    out = capsys.readouterr().out.splitlines()
    assert "--exclude" in out
    assert "--exit-zero" in out


def test_main_empty_env(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("EXCLUDE_PATHS", raising=False)
    monkeypatch.delenv("EXIT_ZERO", raising=False)
    main(["--series", "C", "--slug", "ci"])
    out = capsys.readouterr().out.splitlines()
    assert "--exclude" not in out
    assert "--exit-zero" not in out


def test_main_exit_zero_false_string(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # inputs.exit-zero: false renders as the literal string "false" in GitHub
    # Actions env blocks — the most common production path; must not add --exit-zero.
    monkeypatch.setenv("EXIT_ZERO", "false")
    main(["--series", "C", "--slug", "ci"])
    out = capsys.readouterr().out.splitlines()
    assert "--exit-zero" not in out
