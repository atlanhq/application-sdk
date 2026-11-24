"""Tests for .github/scripts/_gha.py: step outputs and the step summary."""

from __future__ import annotations

import re
from pathlib import Path

import _gha
import pytest


def _parse_github_output(text: str) -> dict[str, str]:
    """Read a $GITHUB_OUTPUT file the way the Actions runner does."""
    outputs: dict[str, str] = {}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if not line:
            continue
        heredoc = re.match(r"^([^=<]+)<<(.+)$", line)
        if heredoc:
            name, delimiter = heredoc.groups()
            body: list[str] = []
            while lines[i] != delimiter:
                body.append(lines[i])
                i += 1
            i += 1
            outputs[name] = "\n".join(body)
        else:
            name, _, value = line.partition("=")
            outputs[name] = value
    return outputs


@pytest.fixture
def output_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    return path


def test_single_line_values_are_key_equals_value(output_file: Path) -> None:
    _gha.set_output("violation", "true")
    _gha.set_output("empty", "")
    assert output_file.read_text(encoding="utf-8") == "violation=true\nempty=\n"


def test_set_outputs_keeps_order(output_file: Path) -> None:
    _gha.set_outputs({"b": "2", "a": "1"})
    assert output_file.read_text(encoding="utf-8") == "b=2\na=1\n"


def test_a_multi_line_value_is_heredoc_quoted(output_file: Path) -> None:
    _gha.set_outputs({"tag_prefix": "main", "tags": "a\nb\nc"})

    text = output_file.read_text(encoding="utf-8")
    assert re.search(r"^tags<<EOF_[0-9a-f]{32}$", text, re.M)
    assert _parse_github_output(text) == {"tag_prefix": "main", "tags": "a\nb\nc"}


def test_a_value_cannot_inject_a_second_output(output_file: Path) -> None:
    """A value that tries to close a guessed delimiter and add its own output."""
    hostile = "x\nEOF\ninjected=yes\nEOF_deadbeef\nmore=1"
    _gha.set_output("deploy_config", hostile)
    _gha.set_output("after", "ok")

    parsed = _parse_github_output(output_file.read_text(encoding="utf-8"))
    assert parsed == {"deploy_config": hostile, "after": "ok"}


def test_each_multi_line_value_gets_a_fresh_delimiter(output_file: Path) -> None:
    _gha.set_outputs({"a": "1\n2", "b": "3\n4"})
    delimiters = re.findall(r"<<(EOF_[0-9a-f]+)", output_file.read_text("utf-8"))
    assert len(set(delimiters)) == 2


def test_a_carriage_return_is_treated_as_multi_line(output_file: Path) -> None:
    _gha.set_output("v", "a\rb")
    assert "v<<EOF_" in output_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", ["", "a=b", "a<<b", "a\nb", "1abc", "a b"])
def test_an_invalid_name_is_refused(output_file: Path, name: str) -> None:
    with pytest.raises(ValueError):
        _gha.set_output(name, "v")
    assert not output_file.exists()


def test_appends_rather_than_truncates(output_file: Path) -> None:
    output_file.write_text("earlier=1\n", encoding="utf-8")
    _gha.set_output("later", "2")
    assert output_file.read_text(encoding="utf-8") == "earlier=1\nlater=2\n"


@pytest.mark.parametrize("value", ["", None])
def test_unset_github_output_prints_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], value
) -> None:
    if value is None:
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    else:
        monkeypatch.setenv("GITHUB_OUTPUT", value)

    _gha.set_outputs({"a": "1", "b": "x\ny"})

    out = capsys.readouterr().out
    assert _parse_github_output(out) == {"a": "1", "b": "x\ny"}


def test_append_summary_adds_a_trailing_newline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    _gha.append_summary("## Title")
    _gha.append_summary("line\n")

    assert summary.read_text(encoding="utf-8") == "## Title\nline\n"


def test_append_summary_prints_when_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    _gha.append_summary("## Title")
    assert capsys.readouterr().out == "## Title\n"
