"""A file the runner cannot parse must surface, not read as clean (FND-4664).

Every AST check catches ``SyntaxError`` and skips the file, and nearly every
one of them does so silently. Before this fix, a repo with one valid and one
unparseable file reported findings for the valid file only, said nothing about
the other, and set ``executionSuccessful: true``. Silence read as coverage.

The runner now parses each discovered Python file once and reports each
failure as a SARIF ``toolExecutionNotification``. The gate fails, because a
clean report must mean every discovered file was scanned.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import Any

import pytest

from conformance.suite import runner
from conformance.suite.checks._ast_common import parse_failure, runtime_python
from conformance.suite.schema import validate_sarif

_SWALLOW = "def f():\n    try:\n        pass\n    except Exception:\n        pass\n"
# Valid up to the swallow, then a line no Python version parses.
_BROKEN = _SWALLOW + "\ndef g(:\n    pass\n"


@pytest.fixture()
def app_repo(tmp_path: Path) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "atlan-demo-app"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "good.py").write_text(_SWALLOW, encoding="utf-8")
    (tmp_path / "app" / "broken.py").write_text(_BROKEN, encoding="utf-8")
    return tmp_path


def _run(repo: Path, *extra: str) -> tuple[int, dict[str, Any]]:
    out = repo / "out.sarif"
    code = runner.main(
        ["--repo", str(repo), "--series", "E", "--output", str(out), *extra]
    )
    return code, json.loads(out.read_text(encoding="utf-8"))


def test_unparseable_file_becomes_a_tool_execution_notification(app_repo: Path) -> None:
    _, doc = _run(app_repo, "--exit-zero")
    invocation = doc["runs"][0]["invocations"][0]

    assert invocation["executionSuccessful"] is False
    notes = invocation["toolExecutionNotifications"]
    assert len(notes) == 1
    note = notes[0]
    assert note["level"] == "error"
    assert note["descriptor"] == {"id": runner.PARSE_FAILURE_ID}
    location = note["locations"][0]["physicalLocation"]
    assert location["artifactLocation"]["uri"] == "app/broken.py"
    assert location["region"]["startLine"] == 7
    assert f"Python {runtime_python()}" in note["message"]["text"]


def test_valid_file_findings_are_still_reported(app_repo: Path) -> None:
    """The parse check adds a notification; it does not replace any result."""
    _, doc = _run(app_repo, "--exit-zero")
    files = {
        r["locations"][0]["physicalLocation"]["artifactLocation"]["uri"]
        for r in doc["runs"][0]["results"]
    }
    assert "app/good.py" in files
    assert "app/broken.py" not in files


def test_unparseable_file_fails_the_gate(app_repo: Path) -> None:
    (app_repo / "app" / "good.py").write_text("X = 1\n", encoding="utf-8")
    code, doc = _run(app_repo)
    invocation = doc["runs"][0]["invocations"][0]

    assert doc["runs"][0]["results"] == []
    assert code == 1
    assert invocation["exitCode"] == 1
    assert "could not be analysed" in invocation["exitCodeDescription"]


def test_exit_zero_softens_the_parse_failure_like_any_violation(app_repo: Path) -> None:
    code, doc = _run(app_repo, "--exit-zero")
    assert code == 0
    assert doc["runs"][0]["invocations"][0]["exitCode"] == 1


def test_excluded_unparseable_file_is_not_reported(app_repo: Path) -> None:
    """``--exclude`` takes a file out of scope; it is then not "unscanned"."""
    _, doc = _run(app_repo, "--exit-zero", "--exclude", "app/broken.py")
    invocation = doc["runs"][0]["invocations"][0]
    assert invocation["executionSuccessful"] is True
    assert "toolExecutionNotifications" not in invocation


def test_clean_repo_sarif_is_unchanged(app_repo: Path) -> None:
    (app_repo / "app" / "broken.py").unlink()
    (app_repo / "app" / "good.py").write_text("X = 1\n", encoding="utf-8")
    code, doc = _run(app_repo)
    invocation = doc["runs"][0]["invocations"][0]
    assert code == 0
    assert invocation["executionSuccessful"] is True
    assert "toolExecutionNotifications" not in invocation
    assert invocation["exitCodeDescription"] == "Gate passed."


def test_report_with_notifications_is_valid_sarif(app_repo: Path) -> None:
    _, doc = _run(app_repo, "--exit-zero")
    validate_sarif(doc)


def test_human_summary_lists_the_unscanned_file(
    app_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(app_repo, "--exit-zero")
    out = capsys.readouterr().out
    assert "1 file could not be parsed, so no check scanned it." in out
    assert "app/broken.py:7" in out


def test_github_annotation_is_anchored_to_the_file(
    app_repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    _run(app_repo)
    out = capsys.readouterr().out
    assert "::error file=app/broken.py,line=7," in out


class TestParseFailureReader:
    def test_valid_file_returns_none(self, tmp_path: Path) -> None:
        (tmp_path / "ok.py").write_text("X = 1\n", encoding="utf-8")
        assert parse_failure(tmp_path / "ok.py", tmp_path) is None

    def test_syntax_error_returns_a_record(self, tmp_path: Path) -> None:
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "bad.py").write_text("x = (\n", encoding="utf-8")
        failure = parse_failure(tmp_path / "pkg" / "bad.py", tmp_path)
        assert failure is not None
        assert failure.file == "pkg/bad.py"
        assert failure.line >= 1
        assert failure.column >= 1
        assert failure.python == runtime_python()

    def test_coding_declaration_is_honoured(self, tmp_path: Path) -> None:
        """Bytes, not UTF-8 text: a latin-1 file with its cookie imports fine."""
        (tmp_path / "latin.py").write_bytes(
            b"# -*- coding: latin-1 -*-\nNAME = '\xe9'\n"
        )
        assert parse_failure(tmp_path / "latin.py", tmp_path) is None

    def test_undeclared_non_utf8_is_a_parse_failure(self, tmp_path: Path) -> None:
        """Python cannot import it either, so it was never scanned."""
        (tmp_path / "bad.py").write_bytes(b"NAME = '\xff'\n")
        assert parse_failure(tmp_path / "bad.py", tmp_path) is not None

    def test_null_byte_is_a_parse_failure(self, tmp_path: Path) -> None:
        (tmp_path / "nul.py").write_bytes(b"X = 1\x00\n")
        assert parse_failure(tmp_path / "nul.py", tmp_path) is not None

    def test_syntax_warning_is_not_a_parse_failure(self, tmp_path: Path) -> None:
        """An invalid escape warns but parses; under ``-W error`` it would raise."""
        (tmp_path / "esc.py").write_text('PATTERN = "\\s+"\n', encoding="utf-8")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assert parse_failure(tmp_path / "esc.py", tmp_path) is None

    def test_unreadable_path_is_not_a_parse_failure(self, tmp_path: Path) -> None:
        assert parse_failure(tmp_path / "missing.py", tmp_path) is None
