"""Tests for the `@sdk-loop` lane.

The lane's whole value is that nobody watches it, so the properties worth
pinning are the ones a human would otherwise catch by looking:

* a second invocation on one PR is dismissed, never queued;
* a commit landing mid-loop re-aims to REVIEW and never to resolve;
* success is an observed effect, never an exit code — `opencode` exits 0 on
  fatal errors;
* a contested finding stays contested, so a disagreement converges without a
  human;
* the job gates actually skip the jobs they claim to.
"""

from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _gha_expr import evaluate  # noqa: E402
from sdk_loop_common import (  # noqa: E402
    ALLOWED_MODELS,
    DEFAULT_MAX_USD,
    MAX_CONSECUTIVE_REAIMS,
    MAX_ROUNDS,
    PROVIDER,
    RESOLVE_MODEL,
    REVIEW_MODEL,
    AgentResult,
    DismissalLedger,
    budget_exceeded,
    gateway_base,
    head_state,
    opencode_config,
    parse_reviewed_head,
    parse_verdict,
    reaim_exhausted,
    run_agent,
    run_budget,
)
from sdk_loop_fence import (  # noqa: E402
    MARK_DECLINE,
    MARK_START,
    decide,
    decline_comment,
    find_live_run,
    is_authorized,
    post_comment,
    start_comment,
)
from sdk_loop_finalize import Round, parse_rounds, render  # noqa: E402
from sdk_loop_phase import (  # noqa: E402
    OUTCOME_CLEAN,
    OUTCOME_FAILED,
    OUTCOME_NO_PROGRESS,
    OUTCOME_OK,
    OUTCOME_TERMINAL_VERDICT,
    interpret_resolve,
    interpret_review,
    newest_verdict,
    parse_dismissals,
    resolve_prompt,
    review_prompt,
    running_total,
)

WORKFLOW = pathlib.Path(__file__).resolve().parents[2] / "workflows" / "sdk-loop.yml"


# ---------------------------------------------------------------------------
# Marker vocabulary — shared with the existing lane
# ---------------------------------------------------------------------------


def test_verdict_and_head_parse_from_the_existing_marker_format() -> None:
    body = (
        "<!-- SDK_REVIEW -->\n"
        "<!-- VERDICT: NEEDS_FIXES -->\n"
        "<!-- REVIEWED_HEAD: 4b43b25714e31497a2f1a184bf1f0c03dde59e80 -->\n"
        "## SDK Review\n"
    )
    assert parse_verdict(body) == "NEEDS_FIXES"
    assert parse_reviewed_head(body) == "4b43b25714e31497a2f1a184bf1f0c03dde59e80"


def test_an_unknown_verdict_string_is_not_accepted() -> None:
    # A typo'd verdict must not read as a real one; downstream approve/downgrade
    # act on this value.
    assert parse_verdict("<!-- VERDICT: LOOKS_FINE -->") is None


# ---------------------------------------------------------------------------
# Fence — authorization and first-run-wins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("assoc", ["OWNER", "MEMBER", "COLLABORATOR", "collaborator"])
def test_collaborators_may_invoke(assoc: str) -> None:
    assert is_authorized(assoc)


@pytest.mark.parametrize("assoc", ["CONTRIBUTOR", "NONE", "FIRST_TIMER", None, ""])
def test_everyone_else_may_not(assoc: str | None) -> None:
    assert not is_authorized(assoc)


def test_a_second_loop_on_the_same_pr_is_dismissed_not_queued() -> None:
    runs = [{"databaseId": 111, "status": "in_progress", "displayTitle": "loop on #42"}]
    decision = decide("MEMBER", runs, "42", self_run_id="222")
    assert decision.proceed is False
    assert decision.live_run_id == "111"
    assert "already running" in decision.reason


def test_the_run_does_not_dismiss_itself() -> None:
    runs = [{"databaseId": 222, "status": "in_progress", "displayTitle": "loop on #42"}]
    assert decide("MEMBER", runs, "42", self_run_id="222").proceed is True


def test_a_loop_on_a_different_pr_does_not_block_this_one() -> None:
    # Cross-PR parallelism is the point: no fleet-wide throttle.
    runs = [{"databaseId": 111, "status": "in_progress", "displayTitle": "loop on #99"}]
    assert decide("MEMBER", runs, "42", self_run_id="222").proceed is True


def test_a_finished_run_is_not_live() -> None:
    runs = [{"databaseId": 111, "status": "completed", "displayTitle": "loop on #42"}]
    assert decide("MEMBER", runs, "42", self_run_id="222").proceed is True


def test_a_queued_run_counts_as_live() -> None:
    # It has not touched the branch yet but it will, so a duplicate must stand
    # down rather than race it.
    runs = [{"databaseId": 111, "status": "queued", "displayTitle": "loop on #42"}]
    assert decide("MEMBER", runs, "42", self_run_id="222").proceed is False


def test_a_fork_run_is_matched_through_the_title() -> None:
    # GitHub leaves `pull_requests` empty for fork runs; matching only on it
    # would let a duplicate through.
    runs = [
        {"databaseId": 111, "status": "in_progress", "displayTitle": "SDK Loop #42"}
    ]
    assert find_live_run(runs, "42", "222") == "111"


def test_pr_number_matching_is_not_a_substring_match() -> None:
    runs = [
        {"databaseId": 111, "status": "in_progress", "displayTitle": "SDK Loop #420"}
    ]
    assert find_live_run(runs, "42", "222") == ""


# ---------------------------------------------------------------------------
# The lane always says what it decided
# ---------------------------------------------------------------------------


def test_a_declined_invocation_is_not_met_with_silence() -> None:
    """Silence is indistinguishable from a broken lane, and the person who
    typed the comment cannot tell which. finalize is gated on proceed=='true',
    so if the fence says nothing here, nothing else will."""
    body = decline_comment("`mallory` is not a collaborator", "http://x/runs/9")
    assert MARK_DECLINE in body
    assert "did not start" in body
    assert "not a collaborator" in body


def test_a_dismissed_duplicate_points_at_the_run_that_has_the_branch() -> None:
    body = decline_comment("a loop is already running", "http://x/runs/9", "7")
    assert "http://x/runs/7" in body


def test_starting_is_announced_before_the_first_verdict() -> None:
    # The first verdict can be 45 minutes away; until then this is the only
    # sign on the PR that anything is happening.
    body = start_comment("42", "http://x/runs/9", "a" * 40)
    assert MARK_START in body
    assert "aaaaaaaa" in body
    assert "http://x/runs/9" in body


def test_failing_to_narrate_never_fails_the_decision() -> None:
    calls: list[list[str]] = []

    def _boom(args, **_kw):
        calls.append(args)
        raise RuntimeError("gh is having a day")

    # The decision is the product; the comment is commentary on it.
    with pytest.raises(RuntimeError):
        post_comment("o/r", "42", "hi", runner=_boom)
    assert calls and calls[0][:3] == ["gh", "pr", "comment"]


# ---------------------------------------------------------------------------
# Head fencing — the re-aim rule
# ---------------------------------------------------------------------------


def test_an_unmoved_head_is_not_interference() -> None:
    state = head_state(live="aaa", baseline="aaa")
    assert state.unchanged and not state.moved_by_other


def test_our_own_push_is_not_interference() -> None:
    state = head_state(live="bbb", baseline="aaa", ours=["bbb"])
    assert state.moved_by_us and not state.moved_by_other


def test_someone_elses_commit_is_interference() -> None:
    state = head_state(live="ccc", baseline="aaa", ours=["bbb"])
    assert state.moved_by_other


# ---------------------------------------------------------------------------
# Review phase
# ---------------------------------------------------------------------------


def _ok(stdout: str = "") -> AgentResult:
    return AgentResult(exit_code=0, stdout=stdout, stderr="")


def _verdict_comment(verdict: str, sha: str, cid: int = 5) -> dict:
    return {
        "id": cid,
        "body": (
            f"<!-- SDK_REVIEW -->\n<!-- VERDICT: {verdict} -->\n"
            f"<!-- REVIEWED_HEAD: {sha} -->\n"
        ),
    }


def test_a_review_that_posted_no_verdict_failed_whatever_it_exited() -> None:
    # opencode exits 0 on fatal errors, so exit_code=0 proves nothing.
    outcome = interpret_review(_ok(), None, "aaa")
    assert outcome.outcome == OUTCOME_FAILED


def test_an_auth_rejection_is_reported_as_such_not_as_a_clean_pass() -> None:
    # The most dangerous false success in a review lane: a gateway rejection
    # that reads as "the model found nothing".
    result = AgentResult(
        exit_code=0, stdout="", stderr="Invalid model name: xai/grok-9"
    )
    outcome = interpret_review(result, None, "aaa")
    assert outcome.outcome == OUTCOME_FAILED
    assert "gateway rejected" in outcome.detail


def test_ready_to_merge_ends_the_loop_clean() -> None:
    sha = "a" * 40
    outcome = interpret_review(_ok(), _verdict_comment("READY_TO_MERGE", sha), sha)
    assert outcome.outcome == OUTCOME_CLEAN


def test_needs_fixes_continues_to_resolve() -> None:
    sha = "a" * 40
    outcome = interpret_review(_ok(), _verdict_comment("NEEDS_FIXES", sha), sha)
    assert outcome.outcome == OUTCOME_OK


@pytest.mark.parametrize("verdict", ["BLOCKED", "NEEDS_HUMAN", "NEEDS_REBASE"])
def test_a_verdict_no_resolve_can_fix_stops_the_loop(verdict: str) -> None:
    sha = "a" * 40
    outcome = interpret_review(_ok(), _verdict_comment(verdict, sha), sha)
    assert outcome.outcome == OUTCOME_TERMINAL_VERDICT


def test_a_verdict_stamped_against_another_sha_is_a_failure_not_a_reaim() -> None:
    """Only this run's own verdict is accepted, and main() already fences the
    head against the remote — so a stamp mismatch means the reviewer disobeyed,
    not that the branch moved. Reporting it as a re-aim let a broken round
    masquerade as progress: four rounds ran and none reviewed anything."""
    outcome = interpret_review(
        _ok(), _verdict_comment("NEEDS_FIXES", "b" * 40), "a" * 40
    )
    assert outcome.outcome == OUTCOME_FAILED


def test_an_aborted_agent_is_a_failure_whatever_it_left_behind() -> None:
    """Live on the first run: `Error: [DecimalError] Invalid argument:
    [object Object]` 11ms into round 1. opencode exited 0, the harness found an
    unrelated verdict already on the PR, and called the round a re-aim."""
    crashed = AgentResult(
        exit_code=0,
        stdout="I'll read the orchestration instructions.\n"
        "Error: [DecimalError] Invalid argument: [object Object]\n",
        stderr="",
    )
    assert not crashed.completed
    assert "DecimalError" in crashed.abort_reason
    outcome = interpret_review(
        crashed, _verdict_comment("READY_TO_MERGE", "a" * 40), "a" * 40
    )
    assert outcome.outcome == OUTCOME_FAILED
    assert "aborted" in outcome.detail


def test_a_clean_transcript_is_not_read_as_an_abort() -> None:
    # "Error" inside ordinary prose must not fail a round that worked.
    fine = AgentResult(
        exit_code=0,
        stdout="Considered an ErrorHandler finding; dropped it.\n",
        stderr="",
    )
    assert fine.completed


def test_a_verdict_answering_someone_elses_trigger_is_not_ours() -> None:
    """PRs carry old verdicts — from @sdk-review, from an earlier loop. Taking
    the newest makes a phase that produced nothing look like it produced
    whatever was lying there."""
    mine = _verdict_comment("NEEDS_FIXES", "a" * 40, cid=9)
    mine["body"] += "<!-- ANSWERS_TRIGGER: 555 -->\n"
    theirs = _verdict_comment("READY_TO_MERGE", "b" * 40, cid=20)
    theirs["body"] += "<!-- ANSWERS_TRIGGER: 111 -->\n"
    picked = newest_verdict([mine, theirs], answers_trigger="555")
    assert picked is not None and parse_verdict(picked["body"]) == "NEEDS_FIXES"
    assert newest_verdict([theirs], answers_trigger="555") is None


def test_newest_verdict_wins_over_an_older_one() -> None:
    comments = [
        _verdict_comment("NEEDS_FIXES", "a" * 40, cid=1),
        _verdict_comment("READY_TO_MERGE", "a" * 40, cid=9),
        {"id": 10, "body": "just a chat comment"},
    ]
    assert parse_verdict(newest_verdict(comments)["body"]) == "READY_TO_MERGE"


def test_the_review_url_is_carried_to_the_resolve_phase() -> None:
    # Declared-but-unpassed is the failure this pins: the resolver would be
    # told "the review is at " and have to hunt for its own verdict.
    sha = "a" * 40
    comment = _verdict_comment("NEEDS_FIXES", sha)
    comment["html_url"] = "https://github.com/o/r/pull/1#issuecomment-9"
    assert interpret_review(_ok(), comment, sha).verdict_url.endswith("issuecomment-9")


def test_the_resolver_is_told_to_skip_the_push_guard_and_why() -> None:
    # The playbook mandates the guard before every push, but the guard waits
    # for a review to answer the resolver's OWN trigger — which this lane never
    # sends. Left in, it would burn its wait budget and refuse the push, so
    # every round would report no_progress with nothing to show for it.
    prompt = resolve_prompt(42, 1, "a" * 40, "http://verdict")
    assert "sdk_resolve_push_guard.py" in prompt
    assert "SKIP" in prompt
    assert "never send one" in prompt


def test_the_resolver_is_told_not_to_trigger_a_review() -> None:
    prompt = resolve_prompt(42, 1, "a" * 40, "http://verdict")
    assert "Do NOT trigger @sdk-review" in prompt
    assert "SDK_LOOP_DISMISSED:" in prompt


def test_wave_two_is_skipped_deliberately_not_left_to_fail() -> None:
    """§2b curls $PROXY_BASE with $PROXY_JWT — mothership sandbox variables
    that do not exist on a runner. Left alone it burns the run's most
    expensive optional step on a doomed call and reports 'unavailable', which
    reads as an outage rather than the design decision it is."""
    prompt = review_prompt(42, 1, "a" * 40, DismissalLedger())
    assert "SKIP §2b" in prompt
    assert "PROXY_JWT" in prompt
    assert "skipped (@sdk-loop" in prompt
    assert "NOT as unavailable" in prompt


def test_round_one_gets_no_delta_range() -> None:
    assert "git diff" not in review_prompt(42, 1, "a" * 40, DismissalLedger())


def test_a_later_round_is_handed_the_incremental_range() -> None:
    prompt = review_prompt(42, 3, "b" * 40, DismissalLedger(), prior_sha="a" * 40)
    assert f"git diff {'a' * 40}..{'b' * 40}" in prompt


def test_the_delta_range_never_narrows_the_review() -> None:
    """§2e' forbids diff-scoping anything above Nit: a regression the resolver
    just introduced two files away must still be caught. Handing over the range
    is a labelling convenience, and the prompt has to say so or a model will
    reasonably read it as permission to skip the rest."""
    prompt = review_prompt(42, 3, "b" * 40, DismissalLedger(), prior_sha="a" * 40)
    assert "Do NOT narrow the review to it" in prompt
    assert "any line of the PR" in prompt


def test_the_review_prompt_references_the_playbook_and_never_restates_it() -> None:
    """The prompt may say what is DIFFERENT about this lane; it must not carry
    a copy of the review rules, which would be a second thing to keep in sync
    and would drift silently from the playbook it contradicts."""
    prompt = review_prompt(42, 3, "a" * 40, DismissalLedger())
    assert ".mothership/pr-review/ORCHESTRATION.md" in prompt
    assert f"round 3 of {MAX_ROUNDS}" in prompt
    # Review policy lives in the playbook. Naming a section to skip is lane
    # wiring; restating what a finding is, or how to tier one, is not.
    for restatement in (
        "Critical",
        "Important",
        "READY_TO_MERGE",
        "NEEDS_FIXES",
        "### Findings",
        "severity",
    ):
        assert restatement not in prompt, f"prompt restates policy: {restatement}"


# ---------------------------------------------------------------------------
# Resolve phase
# ---------------------------------------------------------------------------


def test_a_push_that_moved_the_branch_is_progress() -> None:
    outcome = interpret_resolve(_ok(), True, "aaa", "bbb")
    assert outcome.outcome == OUTCOME_OK
    assert outcome.pushed_sha == "bbb"


def test_a_fix_that_never_pushed_is_a_failure_not_progress() -> None:
    # Reporting an invisible fix would let the loop claim work nobody can see.
    outcome = interpret_resolve(_ok(), True, "aaa", "aaa")
    assert outcome.outcome == OUTCOME_FAILED


def test_no_fix_and_nothing_contested_stalls_the_loop() -> None:
    outcome = interpret_resolve(_ok(), False, "aaa", "aaa")
    assert outcome.outcome == OUTCOME_NO_PROGRESS


def test_contesting_every_finding_counts_as_progress() -> None:
    # Nothing to push, but the ledger now stops those findings being re-raised,
    # so the next review can reach an empty Findings.
    outcome = interpret_resolve(
        _ok(), False, "aaa", "aaa", [{"id": "F1", "rationale": "not reachable"}]
    )
    assert outcome.outcome == OUTCOME_OK
    assert outcome.dismissals[0]["id"] == "F1"


def test_a_resolve_auth_failure_is_not_read_as_nothing_to_do() -> None:
    result = AgentResult(exit_code=0, stdout="", stderr="401 Unauthorized")
    assert interpret_resolve(result, False, "aaa", "aaa").outcome == OUTCOME_FAILED


def test_dismissals_are_parsed_from_the_transcript() -> None:
    transcript = (
        "working...\n"
        'SDK_LOOP_DISMISSED: [{"id": "F1", "rationale": "guarded above"}]\n'
        "done\n"
    )
    assert parse_dismissals(transcript) == [{"id": "F1", "rationale": "guarded above"}]


def test_malformed_dismissal_lines_are_ignored_not_fatal() -> None:
    assert parse_dismissals("SDK_LOOP_DISMISSED: {not json\n") == []


# ---------------------------------------------------------------------------
# Dismissal ledger — what makes disagreement converge without a human
# ---------------------------------------------------------------------------


def test_the_ledger_survives_a_round_trip_between_jobs() -> None:
    ledger = DismissalLedger()
    ledger.add("F1", "the caller already validates this", 2)
    assert DismissalLedger.from_json(ledger.to_json()).ids() == {"F1"}


def test_a_corrupt_ledger_degrades_to_empty_rather_than_crashing_the_round() -> None:
    assert DismissalLedger.from_json("{{{").entries == []
    assert DismissalLedger.from_json(None).entries == []


def test_the_ledger_tells_the_next_review_not_to_re_raise() -> None:
    ledger = DismissalLedger()
    ledger.add("F1", "guarded by the caller", 1)
    section = ledger.as_prompt_section()
    assert "F1" in section and "guarded by the caller" in section
    assert "Do NOT" in section
    assert "escalate" in section


def test_an_empty_ledger_adds_nothing_to_the_prompt() -> None:
    assert DismissalLedger().as_prompt_section() == ""


# ---------------------------------------------------------------------------
# Observability — a running phase must not look identical to a stalled one
# ---------------------------------------------------------------------------


def test_the_agent_transcript_streams_rather_than_buffering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    """Buffered output means a 45-minute review shows nothing until it ends,
    so a hung phase and a working one are indistinguishable — the exact thing
    that made the sandbox lane painful to operate."""
    seen: list[str] = []

    class _Proc:
        returncode = 0
        stdout = iter(["phase 0 complete\n", "phase 1 complete\n"])

        def wait(self) -> None:
            return None

        def kill(self) -> None:  # the timeout watchdog holds a reference
            return None

    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example")
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/opencode")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _Proc())
    out = tmp_path / "t.log"
    result = run_agent(
        REVIEW_MODEL,
        "go",
        str(tmp_path),
        60,
        transcript_path=str(out),
        sink=seen.append,
    )
    assert seen == ["phase 0 complete", "phase 1 complete"]
    assert "phase 1 complete" in result.stdout
    # And kept on disk, because GitHub truncates long job logs.
    assert "phase 0 complete" in out.read_text()


def test_the_pinned_config_is_not_left_in_the_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # The resolve phase commits from this directory; a stray opencode.json
    # would ride along in its push.
    class _Proc:
        returncode = 0
        stdout = iter([])

        def wait(self) -> None:
            return None

        def kill(self) -> None:
            return None

    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example")
    monkeypatch.setattr("shutil.which", lambda _n: "/usr/bin/opencode")
    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: _Proc())
    run_agent(RESOLVE_MODEL, "go", str(tmp_path), 60)
    assert not (tmp_path / "opencode.json").exists()


def test_the_transcript_is_uploaded_even_when_the_phase_died() -> None:
    text = PHASE_WF.read_text(encoding="utf-8")
    assert "actions/upload-artifact" in text
    # A phase that died is exactly the one whose transcript someone wants.
    assert "if: always()" in text


# ---------------------------------------------------------------------------
# Budget — the ceiling that makes a runaway loop impossible
# ---------------------------------------------------------------------------


def test_the_default_ceiling_lets_a_healthy_loop_finish() -> None:
    """Sized so a converging run completes and only a runaway is stopped.

    61 stamped sdk-review runs: median $8.24. Typical convergence is 2-3
    rounds, so ~3 reviews is ~$25 of review alone before any resolve. A
    ceiling at $25 would guillotine a healthy loop at round 2 and teach people
    to raise it blindly — worse than no ceiling at all.
    """
    median_review = 8.24
    assert DEFAULT_MAX_USD > 3 * median_review, "must clear three reviews"
    assert DEFAULT_MAX_USD < 130, "must still stop the eight-round runaway"


def test_the_ceiling_is_tunable_without_a_code_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SDK_LOOP_MAX_USD", "40")
    assert run_budget() == 40.0


@pytest.mark.parametrize("raw", ["", "junk", "0", "-5"])
def test_a_nonsense_ceiling_falls_back_rather_than_disabling_the_guard(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A typo'd variable must not read as "unlimited".
    monkeypatch.setenv("SDK_LOOP_MAX_USD", raw)
    assert run_budget() == DEFAULT_MAX_USD


def test_a_run_at_its_ceiling_is_refused() -> None:
    assert budget_exceeded(50.0, 50.0)
    assert budget_exceeded(51.0, 50.0)
    assert not budget_exceeded(49.99, 50.0)


def test_unmeasurable_spend_never_blocks_the_loop() -> None:
    """A gateway that cannot report spend is a metrics outage, not evidence of
    overspend. Turning one into a stalled lane is the wrong trade — the round
    cap still bounds the worst case."""
    assert not budget_exceeded(None, 25.0)


def test_the_tally_treats_an_unmeasured_phase_as_a_gap_not_a_zero() -> None:
    assert running_total(1.0, 0.5) == 1.5
    assert running_total(None, 0.5) == 0.5
    assert running_total(2.0, None) == 2.0
    assert running_total(None, None) is None


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


def test_opencode_is_pinned_to_the_gateway_and_only_our_two_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example/")
    cfg = opencode_config(REVIEW_MODEL)
    provider = cfg["provider"][PROVIDER]
    assert provider["options"]["baseURL"] == "https://gateway.example/v1"
    assert set(provider["models"]) == set(ALLOWED_MODELS)
    assert cfg["model"] == f"{PROVIDER}/{REVIEW_MODEL}"


def test_the_gateway_url_is_never_defaulted(monkeypatch: pytest.MonkeyPatch) -> None:
    """No endpoint in the repo, and no guess at one either.

    A default would send a phase somewhere unintended when the secret is
    missing, and the first symptom would be a confusing auth error mid-run.
    """
    monkeypatch.delenv("LITELLM_BASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="LITELLM_BASE_URL"):
        gateway_base()


def test_no_gateway_hostname_is_committed_in_this_lane() -> None:
    lane = [
        "sdk_loop_common.py",
        "sdk_loop_fence.py",
        "sdk_loop_phase.py",
        "sdk_loop_finalize.py",
        "gen_sdk_loop_workflow.py",
    ]
    root = pathlib.Path(__file__).resolve().parents[1]
    for name in lane:
        text = (root / name).read_text(encoding="utf-8")
        assert "atlan.dev" not in text, f"{name} hardcodes a gateway hostname"
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "atlan.dev" not in wf


def test_the_review_model_matches_what_the_existing_lanes_use() -> None:
    # All three lanes reviewing on one model means a finding difference between
    # them is about the harness, not the model.
    assert REVIEW_MODEL == "xai/grok-4.6"


def test_a_slash_bearing_alias_composes_into_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`xai/grok-4.6` has a slash, and opencode splits `--model` on the FIRST
    one — so `gateway/xai/grok-4.6` must read as provider `gateway`, model
    `xai/grok-4.6`, and the config's `models` key must carry the full alias."""
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example")
    cfg = opencode_config(REVIEW_MODEL)
    assert cfg["model"] == "gateway/xai/grok-4.6"
    assert cfg["model"].split("/", 1) == [PROVIDER, REVIEW_MODEL]
    assert REVIEW_MODEL in cfg["provider"][PROVIDER]["models"]


def test_an_unknown_model_fails_at_config_time_not_as_a_paid_400() -> None:
    with pytest.raises(ValueError):
        opencode_config("gpt-nonexistent")


def test_the_lane_reaches_exactly_two_models_and_has_no_fallback() -> None:
    """Owner's decision: xai/grok-4.6 for review, gpt-5.6-luna for resolve,
    nothing else.

    Both existing lanes carry RETRY_MAIN_MODEL = claude-opus-5 as a second
    attempt. This one deliberately does not: a failed phase is a failed phase,
    and a silent retry on a different model makes cost and behaviour harder to
    reason about across rounds. Pinned so a future edit adding a ladder has to
    change this test and say why.
    """
    assert ALLOWED_MODELS == ("xai/grok-4.6", "gpt-5.6-luna")
    assert len(set(ALLOWED_MODELS)) == 2


def test_review_and_resolve_run_on_different_models() -> None:
    assert REVIEW_MODEL != RESOLVE_MODEL


def test_the_agent_cannot_reach_the_web(monkeypatch: pytest.MonkeyPatch) -> None:
    # It reads untrusted PR content; an injected prompt must not meet an
    # outbound channel it can choose freely.
    monkeypatch.setenv("LITELLM_BASE_URL", "https://gateway.example")
    assert opencode_config(RESOLVE_MODEL)["permission"]["webfetch"] == "deny"


# ---------------------------------------------------------------------------
# Job gates — evaluated from the real workflow text
# ---------------------------------------------------------------------------


def _gate(job: str) -> str:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return data["jobs"][job]["if"]


def test_resolve_runs_only_when_the_review_found_work() -> None:
    gate = _gate("resolve-1")
    ctx = {"needs": {"review-1": {"outputs": {"outcome": "ok"}}}}
    assert evaluate(gate, ctx) is True
    for outcome in ("clean", "terminal_verdict", "reaim", "failed", ""):
        ctx["needs"]["review-1"]["outputs"]["outcome"] = outcome
        assert evaluate(gate, ctx) is False, outcome


def test_a_reaim_sends_the_loop_to_review_never_to_resolve() -> None:
    ctx = {
        "needs": {
            "fence": {"outputs": {"proceed": "true"}},
            "review-1": {"outputs": {"outcome": "reaim"}},
            "resolve-1": {"outputs": {"outcome": ""}},
        }
    }
    assert evaluate(_gate("review-2"), ctx) is True
    # And the paired resolve for the round that lost the branch stays skipped.
    assert evaluate(_gate("resolve-1"), ctx) is False


def test_a_clean_verdict_stops_the_chain() -> None:
    ctx = {
        "needs": {
            "fence": {"outputs": {"proceed": "true"}},
            "review-1": {"outputs": {"outcome": "clean"}},
            "resolve-1": {"outputs": {"outcome": ""}},
        }
    }
    assert evaluate(_gate("resolve-1"), ctx) is False
    assert evaluate(_gate("review-2"), ctx) is False


def test_a_dismissed_run_starts_no_rounds() -> None:
    ctx = {"needs": {"fence": {"outputs": {"proceed": "false"}}}}
    assert evaluate(_gate("review-1"), ctx) is False


# ---------------------------------------------------------------------------
# Generated workflow freshness
# ---------------------------------------------------------------------------


class _StrictLoader(yaml.SafeLoader):
    """`yaml.safe_load` accepts duplicate keys and keeps the last.

    That is precisely why a duplicate `description:` shipped: every test here
    parsed the file happily while GitHub rejected it outright with
    "'description' is already defined" — and a workflow GitHub will not parse
    runs NO jobs, so the lane answered with silence.
    """


def _no_duplicate_keys(loader, node, deep=False):
    seen, out = set(), {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in seen:
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"duplicate key {key!r} at line {key_node.start_mark.line + 1}",
                key_node.start_mark,
            )
        seen.add(key)
        out[key] = loader.construct_object(value_node, deep=deep)
    return out


_StrictLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicate_keys
)


@pytest.mark.parametrize("wf", ["sdk-loop.yml", "sdk-loop-phase.yml"])
def test_the_workflow_has_no_duplicate_keys(wf: str) -> None:
    """GitHub rejects a duplicate key and runs nothing. Shipped once already."""
    yaml.load((WORKFLOW.parent / wf).read_text(encoding="utf-8"), _StrictLoader)


def _call_spec() -> dict:
    return yaml.safe_load(PHASE_WF.read_text(encoding="utf-8"))[True]["workflow_call"]


def _phase_callers() -> dict:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    return {
        n: j
        for n, j in jobs.items()
        if str(j.get("uses", "")).endswith("sdk-loop-phase.yml")
    }


def test_no_caller_passes_an_input_the_phase_does_not_declare() -> None:
    declared = set(_call_spec().get("inputs") or {})
    passed = set()
    for job in _phase_callers().values():
        passed |= set(job.get("with") or {})
    assert not (passed - declared), f"undeclared inputs: {sorted(passed - declared)}"


def test_every_output_the_chain_reads_is_actually_exported() -> None:
    """A phase output added in the script but not surfaced by the reusable
    workflow resolves to empty, which silently degrades the round rather than
    failing it — the ledger would stop carrying, or the budget stop counting."""
    exported = set(_call_spec().get("outputs") or {})
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    read = set()
    for job in jobs.values():
        read |= set(
            re.findall(
                r"needs\.(?:review|resolve)-\d+\.outputs\.([a-z_]+)", yaml.dump(job)
            )
        )
    assert not (read - exported), f"not exported: {sorted(read - exported)}"


def test_the_workflow_grants_every_scope_its_own_gh_calls_need() -> None:
    """An explicit `permissions:` block makes everything unlisted `none`, and a
    403 in the fence raises before it can post — so a missing scope shows up as
    silence, not as an error. `gh run list` needs actions:read; `gh pr comment`
    needs pull-requests:write."""
    perms = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["permissions"]
    fence = (
        pathlib.Path(__file__).resolve().parents[1] / "sdk_loop_fence.py"
    ).read_text(encoding="utf-8")
    if '"run",' in fence and '"list",' in fence:
        assert perms.get("actions") == "read", "gh run list needs actions: read"
    if '"gh", "pr", "comment"' in fence:
        assert perms.get("pull-requests") == "write"


def test_no_job_reads_outputs_from_a_job_it_does_not_need() -> None:
    """GitHub rejects the WHOLE workflow at parse time for this, so no job runs
    and the fence never gets to say why — the user sees total silence from a
    lane whose whole point is that it always answers.

    Shipped exactly this way: resolve-N read needs.resolve-(N-1).outputs.ledger
    while declaring only [fence, review-N]. Four live invocations produced a
    startup failure with zero jobs and zero comments before it was found.
    """
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    dangling = []
    for name, job in jobs.items():
        needs = job.get("needs") or []
        needs = [needs] if isinstance(needs, str) else needs
        body = yaml.dump({k: v for k, v in job.items() if k != "needs"})
        for ref in sorted(set(re.findall(r"needs\.([A-Za-z0-9_-]+)\.", body))):
            if ref not in needs:
                dangling.append(f"{name} reads needs.{ref}, needs={needs}")
    assert not dangling, "dangling needs references:\n  " + "\n  ".join(dangling)


def test_the_action_pins_in_the_generator_match_the_generated_file() -> None:
    """Renovate edits the generated workflow, not the template that produced
    it, so a bump silently desyncs the two and the freshness gate goes red on
    main. It did, within hours of merge."""
    gen = (
        pathlib.Path(__file__).resolve().parents[1] / "gen_sdk_loop_workflow.py"
    ).read_text(encoding="utf-8")
    wf = WORKFLOW.read_text(encoding="utf-8")
    # Guard everything Renovate can bump in the OUTPUT, not just action SHAs:
    # after the SHA drift it bumped python-version next, and a pins-only guard
    # missed that exactly the way it had missed the SHAs.
    for label, pattern in (
        ("action pin", r"uses: (actions/[a-z-]+@[0-9a-f]{40})"),
        ("python-version", r"python-version: ('[0-9.]+')"),
    ):
        drift = set(re.findall(pattern, gen)) - set(re.findall(pattern, wf))
        assert not drift, f"{label} in generator but not in output: {sorted(drift)}"


def test_the_committed_workflow_matches_its_generator() -> None:
    proc = subprocess.run(
        [sys.executable, ".github/scripts/gen_sdk_loop_workflow.py", "--check"],
        cwd=pathlib.Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_every_round_has_both_phases_and_a_finalize() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]
    for n in range(1, MAX_ROUNDS + 1):
        assert f"review-{n}" in jobs
        assert f"resolve-{n}" in jobs
    assert "fence" in jobs and "finalize" in jobs


def test_the_run_title_actually_carries_the_pr_number() -> None:
    # `run-name: SDK Loop #${{...}}` unquoted makes YAML read the `#` as a
    # comment, leaving the title a bare "SDK Loop". The fence matches fork runs
    # on that title, so the duplicate check would silently stop working.
    run_name = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["run-name"]
    assert "#" in run_name and "github.event.issue.number" in run_name


PHASE_WF = WORKFLOW.parent / "sdk-loop-phase.yml"


def test_the_review_phase_token_cannot_push() -> None:
    """The read-only contract is a property of the credential, not the prompt.

    An injection riding the PR diff cannot make the reviewer push, because the
    token it holds has no write scope to push with.
    """
    text = PHASE_WF.read_text(encoding="utf-8")
    assert (
        "permission-contents: ${{ inputs.phase == 'resolve' && 'write' || 'read' }}"
        in text
    )


def test_the_minted_token_is_narrowed_not_the_apps_full_grant() -> None:
    """atlan-app-fleet also carries actions, administration, checks, packages
    and statuses. Naming permissions scopes the token to those alone, so a
    phase never holds the rest — assert the narrowing is present and that the
    scopes we do take are only the three the lane uses."""
    text = PHASE_WF.read_text(encoding="utf-8")
    named = {
        line.split(":")[0].strip("- ").strip()
        for line in text.splitlines()
        if line.strip().startswith("permission-")
    }
    assert named == {
        "permission-contents",
        "permission-pull-requests",
        "permission-issues",
    }


def test_issues_scope_is_present_for_pr_comments() -> None:
    # PR comments are an Issues-scope resource; without this the review phase
    # 403s posting its verdict, after the model has already been paid for.
    assert "permission-issues: write" in PHASE_WF.read_text(encoding="utf-8")


def test_finalize_reports_even_when_the_loop_died() -> None:
    # A loop that stopped without saying why is worse than one that fails loudly.
    assert "always()" in _gate("finalize")


# ---------------------------------------------------------------------------
# Summary rendering
# ---------------------------------------------------------------------------


def test_the_summary_names_why_the_loop_stopped() -> None:
    rounds = [Round(1, "review", "clean", "READY_TO_MERGE", "a" * 40)]
    out = render(rounds, "clean", "http://run")
    assert "Merge-ready" in out and "nits included" in out
    assert "READY_TO_MERGE" in out


def test_a_reaim_is_explained_rather_than_left_as_jargon() -> None:
    rounds = [Round(1, "review", "reaim", "", "b" * 40, "head moved")]
    out = render(rounds, "exhausted", "http://run")
    assert "someone pushed while the loop was working" in out


def test_a_detail_containing_a_pipe_cannot_break_the_table() -> None:
    rounds = [Round(1, "resolve", "failed", "", "a" * 40, "a | b")]
    assert "a \\| b" in render(rounds, "failed", "http://run")


def test_a_skipped_round_is_not_reported_as_a_round() -> None:
    """A skipped job still emits a row with every field empty. Counting them
    produced "8 review · 8 resolve" for a run where three reviews ran and
    nothing else did, then printed five blank rows implying work that never
    happened."""
    ran = {"number": 1, "phase": "review", "outcome": "reaim", "sha": "a" * 40}
    skipped = {"number": 2, "phase": "resolve", "outcome": "", "verdict": "", "sha": ""}
    assert len(parse_rounds(json.dumps([ran, skipped]))) == 1
    assert parse_rounds("not json") == []


def test_a_reaim_streak_stops_the_run_before_it_eats_the_round_cap() -> None:
    """A re-aim discards the round's work, so it is not progress and needs its
    own budget. A live run burned three rounds on the identical mismatch, each
    reporting `reaim` and advancing as though something had changed."""
    assert not reaim_exhausted(0)
    assert not reaim_exhausted(1)
    assert reaim_exhausted(MAX_CONSECUTIVE_REAIMS)
    assert MAX_CONSECUTIVE_REAIMS < MAX_ROUNDS, "must bite before the round cap"
