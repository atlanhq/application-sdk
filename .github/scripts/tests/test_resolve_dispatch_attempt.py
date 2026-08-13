"""Tests for .github/scripts/resolve_dispatch_attempt.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "resolve_dispatch_attempt.py"
_spec = importlib.util.spec_from_file_location("resolve_dispatch_attempt", _MODULE_PATH)
assert _spec and _spec.loader
resolve_dispatch_attempt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(resolve_dispatch_attempt)

resolve = resolve_dispatch_attempt.resolve
summarise = resolve_dispatch_attempt.summarise
main = resolve_dispatch_attempt.main


FIRST_ONLY = {
    "FIRST_RUN_ID": "111",
    "FIRST_RUN_URL": "https://example.invalid/111",
    "FIRST_STATUS": "completed",
    "FIRST_CONCLUSION": "failure",
}

WITH_RETRY = {
    **FIRST_ONLY,
    "RETRY_RUN_ID": "222",
    "RETRY_RUN_URL": "https://example.invalid/222",
    "RETRY_STATUS": "completed",
    "RETRY_CONCLUSION": "success",
}


def test_no_retry_reports_the_first_attempt():
    resolved = resolve(FIRST_ONLY)
    assert resolved["run_id"] == "111"
    assert resolved["run_url"] == "https://example.invalid/111"
    assert resolved["conclusion"] == "failure"
    assert resolved["retried"] == "false"


def test_retry_reports_the_retry_attempt():
    resolved = resolve(WITH_RETRY)
    assert resolved["run_id"] == "222"
    assert resolved["run_url"] == "https://example.invalid/222"
    assert resolved["conclusion"] == "success"
    assert resolved["retried"] == "true"


def test_retry_that_never_dispatched_falls_back_to_the_first_attempt():
    """The re-key step can run and the dispatch still fail.

    If that emptied the conclusion, the job would go green on a blank string
    instead of failing on attempt 1's real result.
    """
    env = {**FIRST_ONLY, "RETRY_RUN_ID": "", "RETRY_CONCLUSION": ""}
    resolved = resolve(env)
    assert resolved["run_id"] == "111"
    assert resolved["conclusion"] == "failure"
    assert resolved["retried"] == "false"


def test_whitespace_only_retry_run_id_is_not_a_retry():
    resolved = resolve({**FIRST_ONLY, "RETRY_RUN_ID": "   "})
    assert resolved["retried"] == "false"
    assert resolved["conclusion"] == "failure"


def test_a_failing_retry_reports_the_retrys_conclusion():
    env = {**WITH_RETRY, "RETRY_CONCLUSION": "failure"}
    resolved = resolve(env)
    assert resolved["conclusion"] == "failure"
    assert resolved["retried"] == "true"


def test_missing_env_resolves_to_empty_strings_not_none():
    """Outputs are written to $GITHUB_OUTPUT as text; None would render 'None'
    and compare unequal to '' in the workflow's `!= ''` guards."""
    resolved = resolve({})
    assert set(resolved) == {"run_id", "run_url", "status", "conclusion", "retried"}
    assert all(isinstance(v, str) for v in resolved.values())
    assert resolved["run_id"] == ""
    assert resolved["retried"] == "false"


def test_no_summary_line_when_there_was_no_retry():
    assert summarise(FIRST_ONLY, resolve(FIRST_ONLY)) == []


def test_summary_names_both_attempts_when_a_retry_masked_a_failure():
    lines = summarise(WITH_RETRY, resolve(WITH_RETRY))
    assert len(lines) == 1
    assert "attempt 1 concluded `failure`" in lines[0]
    assert "attempt 2 concluded `success`" in lines[0]
    assert "masked a transient first-attempt failure" in lines[0]


def test_summary_says_the_retry_did_not_recover_when_it_also_failed():
    env = {**WITH_RETRY, "RETRY_CONCLUSION": "failure"}
    lines = summarise(env, resolve(env))
    assert "did not recover" in lines[0]
    assert "masked" not in lines[0]


def test_main_writes_outputs_and_step_summary(tmp_path, monkeypatch, capsys):
    output = tmp_path / "gh_output"
    summary = tmp_path / "gh_summary"
    for key, value in WITH_RETRY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))

    assert main() == 0

    written = dict(
        line.split("=", 1) for line in output.read_text().splitlines() if line
    )
    assert written["run_id"] == "222"
    assert written["conclusion"] == "success"
    assert written["retried"] == "true"
    # The retry must stay visible in both channels a human actually reads.
    assert "attempt 1 concluded" in summary.read_text()
    assert "::warning::" in capsys.readouterr().out


def test_main_is_a_noop_on_files_outside_actions(monkeypatch):
    for key in list(WITH_RETRY):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    assert main() == 0
