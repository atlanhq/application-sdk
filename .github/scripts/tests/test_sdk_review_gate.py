"""Tests for the @sdk-review re-trigger gate."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "sdk_review_gate", Path(__file__).resolve().parents[1] / "sdk_review_gate.py"
)
gate = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(gate)


HEAD = "0cab6b6e4eff94f28f249e1319ab74d55e1f7abc"
OTHER = "51c160b06a2a350289c7d779f4ab887503f98685"


def summary(head: str) -> dict:
    return {
        "body": (
            "<!-- SDK_REVIEW -->\n"
            "<!-- VERDICT: READY_TO_MERGE -->\n"
            f"<!-- REVIEWED_HEAD: {head} -->\n"
            "## SDK Review (mothership)\n\n### Verdict: READY TO MERGE\n"
        )
    }


def fake_runner(payload, returncode: int = 0):
    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=returncode, stdout=json.dumps(payload), stderr=""
        )

    return _run


# --- decide() ------------------------------------------------------------


def test_human_proceeds_even_when_head_already_reviewed():
    decision, reason, _ = gate.decide("issue_comment", "vaibhavatlan", HEAD, HEAD)
    assert decision == "proceed"
    assert reason == "human-trigger"


def test_bot_skips_when_head_already_reviewed():
    decision, reason, _ = gate.decide("issue_comment", "mothership-ai[bot]", HEAD, HEAD)
    assert decision == "skip"
    assert reason == "unchanged-head-bot-retrigger"


def test_bot_proceeds_when_head_moved():
    decision, _, _ = gate.decide("issue_comment", "mothership-ai[bot]", HEAD, OTHER)
    assert decision == "proceed"


def test_bot_proceeds_when_pr_has_no_prior_review():
    decision, _, _ = gate.decide("issue_comment", "mothership-ai[bot]", HEAD, None)
    assert decision == "proceed"


def test_manual_dispatch_always_proceeds():
    decision, reason, _ = gate.decide("workflow_dispatch", "", HEAD, HEAD)
    assert decision == "proceed"
    assert reason == "manual-dispatch"


def test_skip_message_names_the_escape_hatch():
    _, _, message = gate.decide("issue_comment", "atlan-ci", HEAD, HEAD)
    assert "@sdk-review" in message
    assert HEAD[:7] in message


# --- last_reviewed_head() ------------------------------------------------


def test_reads_head_from_the_most_recent_summary():
    comments = [summary(OTHER), {"body": "unrelated chatter"}, summary(HEAD)]
    assert gate.last_reviewed_head(comments) == HEAD


def test_ignores_non_summary_comments():
    assert gate.last_reviewed_head([{"body": "@sdk-review"}, {"body": "lgtm"}]) is None


def test_returns_none_when_summary_predates_the_marker():
    """A summary from before REVIEWED_HEAD existed must not gate anything."""
    assert gate.last_reviewed_head([{"body": "<!-- SDK_REVIEW -->\n## SDK Review"}]) is None


def test_tolerates_missing_body_key():
    assert gate.last_reviewed_head([{}, summary(HEAD)]) == HEAD


# --- fetch_comments() ----------------------------------------------------


def test_flattens_slurped_pages():
    pages = [[summary(OTHER)], [summary(HEAD)]]
    comments = gate.fetch_comments("o/r", "1", fake_runner(pages))
    assert len(comments) == 2
    assert gate.last_reviewed_head(comments) == HEAD


def test_gh_failure_returns_empty_so_the_gate_fails_open():
    assert gate.fetch_comments("o/r", "1", fake_runner([], returncode=1)) == []


def test_malformed_json_returns_empty_so_the_gate_fails_open():
    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=0, stdout="not json", stderr="")

    assert gate.fetch_comments("o/r", "1", _run) == []


def test_fail_open_means_a_bot_retrigger_still_reviews():
    """An API outage must never silently stop reviews from running."""
    reviewed = gate.last_reviewed_head(gate.fetch_comments("o/r", "1", fake_runner([], returncode=1)))
    decision, _, _ = gate.decide("issue_comment", "mothership-ai[bot]", HEAD, reviewed)
    assert decision == "proceed"


# --- main() --------------------------------------------------------------


def test_main_writes_outputs(tmp_path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REPO", "atlanhq/application-sdk")
    monkeypatch.setenv("PR_NUMBER", "2987")
    monkeypatch.setenv("HEAD_SHA", HEAD)
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("TRIGGER_ACTOR", "mothership-ai[bot]")

    assert gate.main(fake_runner([[summary(HEAD)]])) == 0

    written = out.read_text()
    assert "decision=skip" in written
    assert "reason=unchanged-head-bot-retrigger" in written


def test_main_does_not_query_github_for_a_human_trigger(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """The gate must cost nothing on the common path."""
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_output"))
    monkeypatch.setenv("REPO", "atlanhq/application-sdk")
    monkeypatch.setenv("PR_NUMBER", "2987")
    monkeypatch.setenv("HEAD_SHA", HEAD)
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("TRIGGER_ACTOR", "vaibhavatlan")

    def _explode(*_args, **_kwargs):
        raise AssertionError("gate queried GitHub on a human trigger")

    assert gate.main(_explode) == 0
    assert "decision=proceed" in (tmp_path / "gh_output").read_text()


# --- head-resolution failure (B3) ----------------------------------------


def test_head_resolution_failure_emits_proceed(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Exhausted head-resolution retries must never silently drop the tag."""
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REPO", "atlanhq/application-sdk")
    monkeypatch.setenv("PR_NUMBER", "2987")
    monkeypatch.setenv("HEAD_SHA", "")
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("TRIGGER_ACTOR", "mothership-ai[bot]")
    monkeypatch.setenv("HEAD_RESOLVED", "false")

    def _should_not_be_called(*_args, **_kwargs):
        raise AssertionError("gate queried GitHub when head-resolution failed")

    assert gate.main(_should_not_be_called) == 0

    written = out.read_text()
    assert "decision=proceed" in written
    assert "reason=head-resolution-failed" in written


def test_head_resolution_failure_human_trigger_also_proceeds(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Even human triggers proceed (trivially) when head resolution failed."""
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REPO", "atlanhq/application-sdk")
    monkeypatch.setenv("PR_NUMBER", "2987")
    monkeypatch.setenv("HEAD_SHA", "")
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("TRIGGER_ACTOR", "vaibhavatlan")
    monkeypatch.setenv("HEAD_RESOLVED", "false")

    assert gate.main(fake_runner([])) == 0
    written = out.read_text()
    assert "decision=proceed" in written
    assert "reason=head-resolution-failed" in written


def test_head_resolved_true_uses_normal_flow(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """HEAD_RESOLVED=true must not short-circuit to fail-open."""
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.setenv("REPO", "atlanhq/application-sdk")
    monkeypatch.setenv("PR_NUMBER", "2987")
    monkeypatch.setenv("HEAD_SHA", HEAD)
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("TRIGGER_ACTOR", "mothership-ai[bot]")
    monkeypatch.setenv("HEAD_RESOLVED", "true")

    assert gate.main(fake_runner([[summary(HEAD)]])) == 0

    written = out.read_text()
    assert "decision=skip" in written
    assert "reason=unchanged-head-bot-retrigger" in written
