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
import subprocess
import sys

import pytest
import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from _gha_expr import evaluate  # noqa: E402
from sdk_loop_common import (  # noqa: E402
    ALLOWED_MODELS,
    MAX_ROUNDS,
    PROVIDER,
    RESOLVE_MODEL,
    REVIEW_MODEL,
    AgentResult,
    DismissalLedger,
    gateway_base,
    head_state,
    opencode_config,
    parse_reviewed_head,
    parse_verdict,
)
from sdk_loop_fence import (  # noqa: E402
    decide,
    find_live_run,
    is_allowed_actor,
    is_authorized,
    parse_allowlist,
)
from sdk_loop_finalize import Round, parse_rounds, render  # noqa: E402
from sdk_loop_phase import (  # noqa: E402
    OUTCOME_CLEAN,
    OUTCOME_FAILED,
    OUTCOME_NO_PROGRESS,
    OUTCOME_OK,
    OUTCOME_REAIM,
    OUTCOME_TERMINAL_VERDICT,
    interpret_resolve,
    interpret_review,
    newest_verdict,
    parse_dismissals,
    resolve_prompt,
    review_prompt,
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


ALLOWED = frozenset({"alice", "bob"})


@pytest.mark.parametrize(
    "raw", ["alice,bob", " alice , bob ", "@alice,@bob", "Alice,BOB"]
)
def test_the_allowlist_tolerates_how_people_actually_write_handles(raw: str) -> None:
    assert parse_allowlist(raw) == ALLOWED


def test_an_allowlisted_collaborator_may_run_the_lane() -> None:
    d = decide("MEMBER", [], "42", "1", actor="alice", allowlist=ALLOWED)
    assert d.proceed is True


def test_a_collaborator_not_on_the_list_may_not() -> None:
    d = decide("MEMBER", [], "42", "1", actor="mallory", allowlist=ALLOWED)
    assert d.proceed is False
    assert "not on the `SDK_LOOP_ALLOWED` list" in d.reason


def test_being_on_the_list_does_not_survive_losing_repo_access() -> None:
    # Association is checked first on purpose: someone who has left the org
    # must not keep the lane just because their handle is still listed.
    d = decide("NONE", [], "42", "1", actor="alice", allowlist=ALLOWED)
    assert d.proceed is False
    assert "collaborators" in d.reason


def test_an_unset_allowlist_permits_nobody() -> None:
    # Fail closed. A forgotten variable silently widening a lane that pushes is
    # the failure nobody notices; a lane refusing to start is the one everybody
    # notices immediately.
    d = decide("OWNER", [], "42", "1", actor="alice", allowlist=frozenset())
    assert d.proceed is False
    assert "no one is on" in d.reason


def test_handle_matching_is_case_insensitive() -> None:
    assert is_allowed_actor("ALICE", ALLOWED)
    assert is_allowed_actor("@Alice", ALLOWED)
    assert not is_allowed_actor("alice2", ALLOWED)


def test_workflow_dispatch_cannot_bypass_the_allowlist() -> None:
    """The dispatch path forces association to OWNER, so the allowlist is the
    only thing standing between "has write access" and "can drive the lane"."""
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert "ALLOWED_ACTORS: ${{ vars.SDK_LOOP_ALLOWED }}" in wf
    # ACTOR must fall back to github.actor, which is who pressed the button.
    assert "github.event.comment.user.login || github.actor" in wf


def test_a_second_loop_on_the_same_pr_is_dismissed_not_queued() -> None:
    runs = [{"databaseId": 111, "status": "in_progress", "displayTitle": "loop on #42"}]
    decision = decide("MEMBER", runs, "42", "222", actor="alice", allowlist=ALLOWED)
    assert decision.proceed is False
    assert decision.live_run_id == "111"
    assert "already running" in decision.reason


def test_the_run_does_not_dismiss_itself() -> None:
    runs = [{"databaseId": 222, "status": "in_progress", "displayTitle": "loop on #42"}]
    assert (
        decide("MEMBER", runs, "42", "222", actor="alice", allowlist=ALLOWED).proceed
        is True
    )


def test_a_loop_on_a_different_pr_does_not_block_this_one() -> None:
    # Cross-PR parallelism is the point: no fleet-wide throttle.
    runs = [{"databaseId": 111, "status": "in_progress", "displayTitle": "loop on #99"}]
    assert (
        decide("MEMBER", runs, "42", "222", actor="alice", allowlist=ALLOWED).proceed
        is True
    )


def test_a_finished_run_is_not_live() -> None:
    runs = [{"databaseId": 111, "status": "completed", "displayTitle": "loop on #42"}]
    assert (
        decide("MEMBER", runs, "42", "222", actor="alice", allowlist=ALLOWED).proceed
        is True
    )


def test_a_queued_run_counts_as_live() -> None:
    # It has not touched the branch yet but it will, so a duplicate must stand
    # down rather than race it.
    runs = [{"databaseId": 111, "status": "queued", "displayTitle": "loop on #42"}]
    assert (
        decide("MEMBER", runs, "42", "222", actor="alice", allowlist=ALLOWED).proceed
        is False
    )


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
    result = AgentResult(exit_code=0, stdout="", stderr="Invalid model name: kimi-k4")
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


def test_a_verdict_stamped_against_another_sha_triggers_a_reaim() -> None:
    outcome = interpret_review(
        _ok(), _verdict_comment("NEEDS_FIXES", "b" * 40), "a" * 40
    )
    assert outcome.outcome == OUTCOME_REAIM


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


def test_the_review_prompt_references_the_playbook_and_never_restates_it() -> None:
    prompt = review_prompt(42, 3, "a" * 40, DismissalLedger())
    assert ".mothership/pr-review/ORCHESTRATION.md" in prompt
    assert f"round 3 of {MAX_ROUNDS}" in prompt
    # A copy of the review rules here would be a second thing to keep in sync.
    assert len(prompt) < 1200


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


def test_an_unknown_model_fails_at_config_time_not_as_a_paid_400() -> None:
    with pytest.raises(ValueError):
        opencode_config("gpt-nonexistent")


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


def test_rounds_json_from_a_skipped_job_is_tolerated() -> None:
    # Skipped jobs emit empty outputs; the summary must still render.
    raw = json.dumps(
        [{"number": 1, "phase": "review", "outcome": "", "verdict": "", "sha": ""}]
    )
    assert len(parse_rounds(raw)) == 1
    assert parse_rounds("not json") == []
