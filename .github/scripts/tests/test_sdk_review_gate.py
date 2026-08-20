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
    assert (
        gate.last_reviewed_head([{"body": "<!-- SDK_REVIEW -->\n## SDK Review"}])
        is None
    )


def test_tolerates_missing_body_key():
    assert gate.last_reviewed_head([{}, summary(HEAD)]) == HEAD


# --- fetch_comments() ----------------------------------------------------


def test_flattens_slurped_pages():
    pages = [[summary(OTHER)], [summary(HEAD)]]
    comments = gate.fetch_comments("o/r", "1", fake_runner(pages))
    assert len(comments) == 2
    assert gate.last_reviewed_head(comments) == HEAD


def test_prior_review_load_last_across_pages():
    """S5: the §6b bootstrap uses --paginate --slurp so `last` picks the
    genuinely newest SDK_REVIEW comment across ALL pages, not the last one
    on page 1 (which is what --paginate --jq returns when jq runs per page).

    Invariant: given two SDK_REVIEW comments on separate pages — an older
    one on page 1 and a newer one on page 2 — fetch_comments+last_reviewed_head
    returns the page-2 sha, not the page-1 sha.
    """
    non_review = {"body": "LGTM, looks good!"}
    page1 = [non_review, summary(OTHER), non_review]  # older SDK_REVIEW on p1
    page2 = [non_review, summary(HEAD)]  # newer SDK_REVIEW on p2
    comments = gate.fetch_comments("o/r", "1", fake_runner([page1, page2]))
    # Must return HEAD (page 2), not OTHER (page 1).
    assert gate.last_reviewed_head(comments) == HEAD


def test_gh_failure_returns_empty_so_the_gate_fails_open():
    assert gate.fetch_comments("o/r", "1", fake_runner([], returncode=1)) == []


def test_malformed_json_returns_empty_so_the_gate_fails_open():
    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="not json", stderr=""
        )

    assert gate.fetch_comments("o/r", "1", _run) == []


def test_fail_open_means_a_bot_retrigger_still_reviews():
    """An API outage must never silently stop reviews from running."""
    reviewed = gate.last_reviewed_head(
        gate.fetch_comments("o/r", "1", fake_runner([], returncode=1))
    )
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


def test_main_does_not_query_github_for_a_human_trigger(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
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


def test_head_resolution_failure_emits_proceed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
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


# --- resolve_head() ------------------------------------------------------


def sha_runner(*results):
    """A runner that returns each (stdout, returncode) in turn."""
    queue = list(results)
    calls: list[list[str]] = []

    def _run(args, *_a, **_kw):
        calls.append(args)
        stdout, code = queue.pop(0)
        return subprocess.CompletedProcess(
            args=args, returncode=code, stdout=stdout, stderr=""
        )

    _run.calls = calls  # type: ignore[attr-defined]
    return _run


def test_resolve_head_returns_the_sha_on_the_first_attempt():
    runner = sha_runner((f"{HEAD}\n", 0))
    slept: list[float] = []
    assert gate.resolve_head("o/r", "1", runner, slept.append) == (HEAD, True)
    assert slept == []


def test_resolve_head_retries_a_transient_failure():
    runner = sha_runner(("", 1), ("", 1), (f"{HEAD}\n", 0))
    slept: list[float] = []
    assert gate.resolve_head("o/r", "1", runner, slept.append) == (HEAD, True)
    assert slept == [5, 10]


def test_resolve_head_treats_empty_stdout_as_a_failure():
    """A 0 exit with no sha is not a resolution — `gh` does that on some 5xx."""
    runner = sha_runner(("\n", 0), (f"{HEAD}\n", 0))
    assert gate.resolve_head("o/r", "1", runner, lambda _s: None) == (HEAD, True)


def test_resolve_head_gives_up_after_three_attempts():
    runner = sha_runner(("", 1), ("", 1), ("", 1))
    assert gate.resolve_head("o/r", "1", runner, lambda _s: None) == ("", False)
    assert len(runner.calls) == gate.HEAD_ATTEMPTS


# --- inflight_sibling_run() ----------------------------------------------


def starter(
    head: str, run_id: str, stamped: bool = False, folded: bool = False
) -> dict:
    """A `review starting` comment as the dispatch job writes it."""
    lines = [
        gate.STARTER_MARKER,
        f"<!-- SDK_REVIEW_STARTED_HEAD: {head} -->",
        f"<!-- SDK_REVIEW_STARTED_RUN: {run_id} -->",
        "🔍 **SDK Review (mothership)** triggered by @someone at 2026-08-19T18:20:48Z.",
    ]
    if folded:
        # The starter step folds earlier starters by dropping line 1 and
        # re-emitting the rest inside a <details> block — the head/run stamps
        # survive that, which is what keeps a folded-but-live claim visible.
        lines = [
            gate.STARTER_MARKER,
            "<!-- SDK_REVIEW_STARTED_FOLDED -->",
            "<details><summary>Earlier @sdk-review trigger</summary>",
            "",
            *lines[1:],
            "</details>",
        ]
    if stamped:
        lines += [
            "",
            "---",
            "✅ **Completed** — status `completed`, cost `$1.20`, duration 8m 3s.",
        ]
    return {"body": "\n".join(lines)}


def test_inflight_detects_an_unstamped_starter_from_another_run():
    comments = [starter(HEAD, "32286821875")]
    assert gate.inflight_sibling_run(comments, HEAD, "32286845311") == "32286821875"


def test_inflight_ignores_our_own_starter():
    comments = [starter(HEAD, "32286845311")]
    assert gate.inflight_sibling_run(comments, HEAD, "32286845311") is None


def test_inflight_ignores_a_finished_run():
    """The cost stamp is `always()`, so its presence means that run ended."""
    comments = [starter(HEAD, "32286821875", stamped=True)]
    assert gate.inflight_sibling_run(comments, HEAD, "32286845311") is None


def test_inflight_ignores_a_claim_on_a_different_head():
    comments = [starter(OTHER, "32286821875")]
    assert gate.inflight_sibling_run(comments, HEAD, "32286845311") is None


def test_inflight_sees_a_folded_but_unfinished_claim():
    """A newer trigger folds the older starter; the older run is still live."""
    comments = [starter(HEAD, "32286821875", folded=True)]
    assert gate.inflight_sibling_run(comments, HEAD, "32286845311") == "32286821875"


def test_inflight_ignores_starters_written_before_the_stamps_existed():
    """Rollout safety: a legacy starter must not skip a legitimate trigger."""
    legacy = {
        "body": f"{gate.STARTER_MARKER}\n🔍 **SDK Review (mothership)** triggered."
    }
    assert gate.inflight_sibling_run([legacy], HEAD, "32286845311") is None


def test_inflight_ignores_the_verdict_comment():
    assert gate.inflight_sibling_run([summary(HEAD)], HEAD, "1") is None


def test_inflight_needs_a_head_to_compare():
    comments = [starter(HEAD, "32286821875")]
    assert gate.inflight_sibling_run(comments, "", "32286845311") is None


def test_inflight_returns_the_newest_matching_claim():
    comments = [starter(HEAD, "111"), starter(HEAD, "222")]
    assert gate.inflight_sibling_run(comments, HEAD, "333") == "222"


# --- decide() with a sibling in flight -----------------------------------


def test_bot_skips_while_a_sibling_run_is_in_flight():
    decision, reason, message = gate.decide(
        "issue_comment", "mothership-ai[bot]", HEAD, None, "32286821875"
    )
    assert decision == "skip"
    assert reason == "sibling-run-in-flight"
    assert "32286821875" in message


def test_human_proceeds_even_while_a_sibling_run_is_in_flight():
    decision, reason, _ = gate.decide(
        "issue_comment", "vaibhavatlan", HEAD, None, "32286821875"
    )
    assert decision == "proceed"
    assert reason == "human-trigger"


def test_already_reviewed_wins_over_sibling_in_flight():
    """Both are skips; the reviewed-head reason is the more useful one."""
    _, reason, _ = gate.decide("issue_comment", "atlan-ci", HEAD, HEAD, "32286821875")
    assert reason == "unchanged-head-bot-retrigger"


def test_no_sibling_in_flight_still_proceeds_on_a_new_head():
    decision, reason, _ = gate.decide("issue_comment", "atlan-ci", HEAD, OTHER, None)
    assert decision == "proceed"
    assert reason == "bot-trigger-new-head"


# --- main(): phases ------------------------------------------------------


def _bot_env(monkeypatch: pytest.MonkeyPatch, out_path, **extra: str) -> None:
    monkeypatch.setenv("GITHUB_OUTPUT", str(out_path))
    monkeypatch.setenv("REPO", "atlanhq/application-sdk")
    monkeypatch.setenv("PR_NUMBER", "3285")
    monkeypatch.setenv("HEAD_SHA", HEAD)
    monkeypatch.setenv("EVENT_NAME", "issue_comment")
    monkeypatch.setenv("TRIGGER_ACTOR", "mothership-ai[bot]")
    monkeypatch.delenv("HEAD_RESOLVED", raising=False)
    for key, value in extra.items():
        monkeypatch.setenv(key, value)


def test_locked_phase_skips_a_burst_trigger_while_the_first_run_is_live(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """The #3285 shape: five bot triggers, only the first earns a run."""
    out = tmp_path / "gh_output"
    _bot_env(monkeypatch, out, GATE_PHASE="locked", RUN_ID="32286845311")

    assert gate.main(fake_runner([[starter(HEAD, "32286821875")]])) == 0

    written = out.read_text()
    assert "decision=skip" in written
    assert "reason=sibling-run-in-flight" in written


def test_preflight_phase_does_not_gate_on_a_sibling_run(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Outside the lock a live sibling is the normal state, not a duplicate."""
    out = tmp_path / "gh_output"
    _bot_env(monkeypatch, out, GATE_PHASE="preflight", RUN_ID="32286845311")

    assert gate.main(fake_runner([[starter(HEAD, "32286821875")]])) == 0

    written = out.read_text()
    assert "decision=proceed" in written
    assert "reason=bot-trigger-new-head" in written


def test_phase_defaults_to_preflight(tmp_path, monkeypatch: pytest.MonkeyPatch):
    out = tmp_path / "gh_output"
    _bot_env(monkeypatch, out, RUN_ID="32286845311")
    monkeypatch.delenv("GATE_PHASE", raising=False)

    assert gate.main(fake_runner([[starter(HEAD, "32286821875")]])) == 0
    assert "decision=proceed" in out.read_text()


def test_locked_phase_still_skips_an_already_reviewed_head(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "gh_output"
    _bot_env(monkeypatch, out, GATE_PHASE="locked", RUN_ID="32286845311")

    assert gate.main(fake_runner([[summary(HEAD)]])) == 0

    written = out.read_text()
    assert "decision=skip" in written
    assert "reason=unchanged-head-bot-retrigger" in written


# --- main(): head resolution now lives here ------------------------------


def test_main_resolves_head_when_the_caller_supplies_none(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "gh_output"
    _bot_env(monkeypatch, out, GATE_PHASE="locked", RUN_ID="1")
    monkeypatch.setenv("HEAD_SHA", "")

    def _run(args, *_a, **_kw):
        if "pulls/3285" in " ".join(args):
            return subprocess.CompletedProcess(args, 0, f"{HEAD}\n", "")
        return subprocess.CompletedProcess(args, 0, json.dumps([[summary(HEAD)]]), "")

    assert gate.main(_run, lambda _s: None) == 0

    written = out.read_text()
    assert f"head_sha={HEAD}" in written
    assert "head_resolved=true" in written
    assert "reason=unchanged-head-bot-retrigger" in written


def test_main_fails_open_when_it_cannot_resolve_head_itself(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """No HEAD_RESOLVED env needed any more — exhaustion is seen in-script."""
    out = tmp_path / "gh_output"
    _bot_env(monkeypatch, out, GATE_PHASE="locked", RUN_ID="1")
    monkeypatch.setenv("HEAD_SHA", "")

    def _run(args, *_a, **_kw):
        assert "pulls/3285" in " ".join(args), "must not read comments without a head"
        return subprocess.CompletedProcess(args, 1, "", "boom")

    assert gate.main(_run, lambda _s: None) == 0

    written = out.read_text()
    assert "decision=proceed" in written
    assert "reason=head-resolution-failed" in written
    assert "head_resolved=false" in written


def test_main_exports_the_head_it_decided_against(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    out = tmp_path / "gh_output"
    _bot_env(monkeypatch, out, GATE_PHASE="preflight", RUN_ID="1")

    assert gate.main(fake_runner([[]])) == 0
    assert f"head_sha={HEAD}" in out.read_text()
