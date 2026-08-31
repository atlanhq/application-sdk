#!/usr/bin/env python3
"""Run one phase of an `@sdk-loop` round — either the review or the resolve.

One invocation, one job, one runner. The two phases never share a process, a
working tree or an agent session; all that crosses between them is the small
structured handoff this script emits as step outputs (verdict, head, ledger).

Both phases follow the same skeleton:

    fence  ->  run the agent on its existing playbook  ->  prove an effect  ->
    emit the handoff

with three rules that matter more than the skeleton:

* **The exit code is not the result.** `opencode` exits 0 on fatal errors.
  Review succeeded if a verdict comment landed; resolve succeeded if the tree
  changed and the push was accepted. Nothing else counts.
* **Fence before acting and again before pushing.** A head that moved under us
  invalidates the round's work, which is discarded rather than applied.
* **Never fix against a review of a different sha.** When the head moves, the
  loop re-aims to REVIEW, never straight to resolve.

Environment:
    PHASE               "review" | "resolve"
    ROUND               1-based round number
    REPO, PR_NUMBER, HEAD_REF
    BASE_SHA            the sha this round is fenced against
    OURS                comma-separated shas this run pushed already
    COMMENT_ID          triggering comment, for ANSWERS_TRIGGER
    LEDGER              dismissal ledger JSON carried from earlier rounds
    GH_TOKEN            App token — write scope ONLY in the resolve phase
    LITELLM_API_KEY     gateway bearer
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable

from sdk_loop_common import (
    MAX_ROUNDS,
    PLAYBOOK_RESOLVE,
    PLAYBOOK_REVIEW,
    RESOLVE_MODEL,
    REVIEW_MODEL,
    AgentResult,
    DismissalLedger,
    budget_exceeded,
    emit_outputs,
    gateway_spend,
    head_state,
    is_verdict_comment,
    parse_answers_trigger,
    parse_reviewed_head,
    parse_verdict,
    reaim_exhausted,
    run_agent,
    run_budget,
    spend_delta,
)

#: Wall-clock per phase. Review is the slower of the two: it walks the whole
#: five-phase playbook including sub-agents, where a resolve round is mostly
#: targeted edits.
TIMEOUT_REVIEW_S = 45 * 60
TIMEOUT_RESOLVE_S = 30 * 60

#: Outcomes a phase can report to the next job. Only `ok` continues to the
#: paired phase; `reaim` sends the loop back to review on the new head.
OUTCOME_OK = "ok"
OUTCOME_CLEAN = "clean"
OUTCOME_REAIM = "reaim"
OUTCOME_NO_PROGRESS = "no_progress"
OUTCOME_TERMINAL_VERDICT = "terminal_verdict"
OUTCOME_FAILED = "failed"
#: Refused before starting, because the run has spent its allowance.
OUTCOME_BUDGET = "budget_exhausted"
#: Gave up re-aiming: the loop never got a clean pass at one commit.
OUTCOME_REAIM_EXHAUSTED = "reaim_exhausted"


def _as_float(raw: str | None) -> float | None:
    """Cumulative spend handed down the chain; empty means not measured."""
    if not (raw or "").strip():
        return None
    try:
        return float(raw)  # type: ignore[arg-type]
    except ValueError:
        return None


def running_total(spent_so_far: float | None, phase_cost: float | None) -> float | None:
    """Carry the tally forward, treating an unmeasured phase as a gap not a zero."""
    if spent_so_far is None and phase_cost is None:
        return None
    return (spent_so_far or 0.0) + (phase_cost or 0.0)


def _sh(args: list[str], runner: Callable[..., Any] = subprocess.run, **kw: Any) -> Any:
    return runner(args, capture_output=True, text=True, check=False, **kw)


def live_head(
    repo: str, head_ref: str, runner: Callable[..., Any] = subprocess.run
) -> str:
    """The branch's current sha, read from the remote, never from the checkout."""
    proc = _sh(
        ["gh", "api", f"repos/{repo}/git/ref/heads/{head_ref}", "--jq", ".object.sha"],
        runner=runner,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"could not read {head_ref}: {proc.stderr.strip()}")
    return (proc.stdout or "").strip()


# --------------------------------------------------------------------------
# Review phase
# --------------------------------------------------------------------------


def review_prompt(
    pr: int,
    round_no: int,
    sha: str,
    ledger: DismissalLedger,
    prior_sha: str = "",
) -> str:
    """Point the agent at the playbook. The playbook is NOT restated here.

    Everything about what a good review is lives in
    `.mothership/pr-review/ORCHESTRATION.md`, unchanged and read from this
    runner's own checkout. Duplicating any of it into this prompt would create
    a second copy to keep in sync, which is the failure this lane is designed
    to avoid.
    """
    parts = [
        f"Read {PLAYBOOK_REVIEW} and follow it exactly for PR #{pr}.",
        "",
        f"You are reviewing sha {sha}. Stamp that sha as REVIEWED_HEAD.",
        f"This is round {round_no} of {MAX_ROUNDS} of an @sdk-loop run;",
        f"stamp the footer line `Round {round_no} of {MAX_ROUNDS} · @sdk-loop`.",
        "",
        "You are READ-ONLY. Your token carries no write scope — do not attempt",
        "to push, and do not treat a push failure as something to work around.",
        "",
        "SKIP §2b (the Wave 2 cross-model adversarial). Two reasons, and either",
        "alone is sufficient:",
        "",
        "  * It cannot run here. §2b curls `$PROXY_BASE/proxy/litellm/...` with",
        "    `$PROXY_JWT`; both are mothership sandbox variables and neither",
        "    exists on this runner. Attempting it wastes the run's most",
        "    expensive optional step on a call that cannot succeed, and logs",
        "    'adversarial: unavailable' as though something were broken.",
        "  * It would be redundant. In this lane the resolve phase opens by",
        "    contesting every finding you raise (pr-resolve §3d, 'Fix every",
        "    finding or prove it false'), so the challenge happens either way —",
        "    by an agent that can also act on the answer.",
        "",
        "Record it as `Cross-model adversarial: skipped (@sdk-loop — resolve",
        "phase contests findings)`, NOT as unavailable.",
        "",
    ]
    if prior_sha:
        # Handed over so §2e labelling and the §2e′ nit rules do not have to
        # re-derive the range. It is ADDITIONAL context, never a substitute for
        # the full diff: §2e′ is explicit that Critical/High/Important findings
        # are raised on any line, including code the resolver just pushed, so a
        # delta-scoped review would hide precisely the regressions this loop is
        # most likely to introduce.
        parts += [
            f"A previous round of this run reviewed {prior_sha}. The incremental",
            f"change since then is `git diff {prior_sha}..{sha}`.",
            "",
            "Use it for §2e labelling (RESOLVED / STILL PRESENT / NEW) and for the",
            "§2e′ nit rules. Do NOT narrow the review to it — Critical, High and",
            "Important findings are still raised on any line of the PR.",
            "",
        ]
    section = ledger.as_prompt_section()
    if section:
        parts.append(section)
    return "\n".join(parts)


def newest_verdict(
    comments: list[dict[str, Any]],
    since_id: str | None = None,
    answers_trigger: str | None = None,
) -> dict[str, Any] | None:
    """The verdict THIS phase produced — not merely the newest one present.

    A PR usually already carries verdicts: from `@sdk-review`, from an earlier
    loop, from a re-review days ago. Accepting the newest of those makes a
    phase that produced nothing look like it produced whatever was lying
    around. Not hypothetical — it is exactly how a crashed agent got reported
    as a re-aim, four rounds running, with the stale verdict's older
    REVIEWED_HEAD supplying the "the head moved" signal.

    So the match is on `ANSWERS_TRIGGER` when this run's trigger id is known.
    A verdict answering someone else's request is someone else's verdict.
    """
    best: dict[str, Any] | None = None
    for comment in comments:
        body = comment.get("body") or ""
        if not is_verdict_comment(body) or parse_verdict(body) is None:
            continue
        if answers_trigger and parse_answers_trigger(body) != str(answers_trigger):
            continue
        if since_id and int(comment.get("id", 0)) <= int(since_id):
            continue
        if best is None or int(comment.get("id", 0)) > int(best.get("id", 0)):
            best = comment
    return best


@dataclass(frozen=True)
class ReviewOutcome:
    outcome: str
    verdict: str = ""
    reviewed_head: str = ""
    detail: str = ""
    #: Where the verdict landed. The resolve phase is pointed at this; without
    #: it the resolver is told "the review is at " and has to go hunting.
    verdict_url: str = ""


def interpret_review(
    result: AgentResult,
    verdict_comment: dict[str, Any] | None,
    expected_sha: str,
) -> ReviewOutcome:
    """Decide what the review phase actually achieved.

    Deliberately ignores `result.exit_code`. A review that posted no verdict
    did not happen, whatever it exited with — and an auth rejection reads as a
    silent findings-free pass unless it is caught here, which is the most
    dangerous false success this lane can produce.
    """
    if not result.completed:
        return ReviewOutcome(
            outcome=OUTCOME_FAILED,
            detail=f"the agent aborted: {result.abort_reason}",
        )
    if verdict_comment is None:
        detail = (
            "the gateway rejected the request (auth or model)"
            if not result.looks_authenticated
            else "the phase produced no verdict comment"
        )
        return ReviewOutcome(outcome=OUTCOME_FAILED, detail=detail)

    body = verdict_comment.get("body") or ""
    verdict = parse_verdict(body) or ""
    stamped = parse_reviewed_head(body) or ""

    if (
        stamped
        and not expected_sha.startswith(stamped)
        and not stamped.startswith(expected_sha[:7])
    ):
        # Now that only THIS run's verdict is accepted, a stamp mismatch is the
        # reviewer disobeying, not evidence the branch moved — main() already
        # fences the head against the remote before we get here. Reporting it
        # as a re-aim let a broken round masquerade as progress.
        return ReviewOutcome(
            outcome=OUTCOME_FAILED,
            verdict=verdict,
            reviewed_head=stamped,
            detail=f"verdict stamps {stamped[:8]}, round expected {expected_sha[:8]}",
        )

    url = str(verdict_comment.get("html_url") or "")
    if verdict in ("READY_TO_MERGE",):
        return ReviewOutcome(OUTCOME_CLEAN, verdict, stamped, verdict_url=url)
    if verdict in ("BLOCKED", "NEEDS_HUMAN", "NEEDS_REBASE"):
        return ReviewOutcome(
            OUTCOME_TERMINAL_VERDICT,
            verdict,
            stamped,
            detail=f"{verdict} is not something a resolve phase can fix",
            verdict_url=url,
        )
    return ReviewOutcome(OUTCOME_OK, verdict, stamped, verdict_url=url)


# --------------------------------------------------------------------------
# Resolve phase
# --------------------------------------------------------------------------


def resolve_prompt(pr: int, round_no: int, sha: str, verdict_url: str) -> str:
    parts = [
        f"Read {PLAYBOOK_RESOLVE} and follow it exactly for PR #{pr}.",
        "",
        f"The review for sha {sha} is at {verdict_url}.",
        f"This is round {round_no} of {MAX_ROUNDS} of an @sdk-loop run.",
        "",
        "Differences from a standalone @sdk-resolve run, both of which narrow",
        "your job rather than widening it:",
        "",
        "1. Do NOT trigger @sdk-review and do NOT wait for one. The loop runs",
        "   the next review itself, as a separate job, once you finish. Phase",
        "   3a/3b of the playbook is handled by the harness.",
        "   Because of that, SKIP sdk_resolve_push_guard.py. The guard blocks",
        "   until an in-flight review answers YOUR trigger; you never send one,",
        "   so there is no trigger→verdict window to respect and the guard has",
        "   nothing to wait for. The harness fences the push instead, by",
        "   comparing the live head against the sha that was reviewed.",
        "2. Begin with §3d. Contest the findings BEFORE fixing anything: for",
        "   each one, either fix it or prove it false with a concrete rationale.",
        "   There is no separate adversarial reviewer in this lane — you are it,",
        "   and a finding you disprove is recorded so the next review cannot",
        "   simply re-raise it.",
        "3. Stop when the findings are cleared. Do not merge; do not approve.",
        "",
        "Emit your dismissals as a JSON array on a line beginning",
        '`SDK_LOOP_DISMISSED:` — each entry {"id": ..., "rationale": ...}.',
    ]
    return "\n".join(parts)


DISMISSAL_PREFIX = "SDK_LOOP_DISMISSED:"


def parse_dismissals(transcript: str) -> list[dict[str, str]]:
    """Pull the resolver's contested findings out of its transcript."""
    found: list[dict[str, str]] = []
    for line in (transcript or "").splitlines():
        stripped = line.strip()
        if not stripped.startswith(DISMISSAL_PREFIX):
            continue
        payload = stripped[len(DISMISSAL_PREFIX) :].strip()
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, list):
            continue
        for entry in parsed:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("id"), str)
                and entry["id"]
            ):
                found.append(
                    {
                        "id": entry["id"],
                        "rationale": str(entry.get("rationale", "")).strip()
                        or "no rationale given",
                    }
                )
    return found


@dataclass(frozen=True)
class ResolveOutcome:
    outcome: str
    pushed_sha: str = ""
    dismissals: tuple[dict[str, str], ...] = ()
    detail: str = ""


def interpret_resolve(
    result: AgentResult,
    tree_changed: bool,
    head_before: str,
    head_after: str,
    dismissals: list[dict[str, str]] | None = None,
) -> ResolveOutcome:
    """Success is an observed effect, never an exit code.

    A resolve round counts only when the branch actually moved. A round that
    changed the tree but failed to push is a FAILURE, not progress: the loop
    must not report a fix that no one can see.
    """
    dismissed = tuple(dismissals or ())
    if not result.looks_authenticated:
        return ResolveOutcome(
            OUTCOME_FAILED, detail="the gateway rejected the request (auth or model)"
        )
    if head_after != head_before:
        return ResolveOutcome(OUTCOME_OK, pushed_sha=head_after, dismissals=dismissed)
    if tree_changed:
        return ResolveOutcome(
            OUTCOME_FAILED,
            dismissals=dismissed,
            detail="the tree changed but nothing was pushed",
        )
    if dismissed:
        # Everything the review raised was contested. Nothing to push, but the
        # round DID make progress: the ledger now blocks those findings from
        # being re-raised, so the next review can reach an empty Findings.
        return ResolveOutcome(
            OUTCOME_OK,
            dismissals=dismissed,
            detail="all findings contested; no code change needed",
        )
    return ResolveOutcome(
        OUTCOME_NO_PROGRESS, detail="no fix, no push and nothing contested"
    )


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    phase = os.environ["PHASE"]
    round_no = int(os.environ.get("ROUND", "1"))
    repo = os.environ["REPO"]
    pr = int(os.environ["PR_NUMBER"])
    head_ref = os.environ["HEAD_REF"]
    baseline = os.environ["BASE_SHA"]
    ours = [s for s in os.environ.get("OURS", "").split(",") if s]
    ledger = DismissalLedger.from_json(os.environ.get("LEDGER"))

    state = head_state(live_head(repo, head_ref), baseline, ours)
    reaims = int(os.environ.get("REAIMS_SO_FAR") or 0)
    if state.moved_by_other and reaim_exhausted(reaims):
        # Stop rather than spend another round on a target that keeps moving.
        emit_outputs(
            outcome=OUTCOME_REAIM_EXHAUSTED,
            new_base_sha=state.live,
            reaims=str(reaims),
            detail=f"{reaims} consecutive re-aims without a clean pass at one commit",
        )
        print(f"giving up after {reaims} consecutive re-aims")
        return 0
    if state.moved_by_other:
        # Re-aim: discard whatever this round would have done and send the loop
        # back to review on the new head. Never resolve against a stale review.
        emit_outputs(
            outcome=OUTCOME_REAIM,
            new_base_sha=state.live,
            reaims=str(reaims + 1),
            detail=f"head moved to {state.live[:8]} outside this run",
        )
        print(f"re-aim: head is {state.live[:8]}, expected {baseline[:8]}")
        return 0

    # Budget before work: a phase refused at the boundary costs nothing, while
    # one killed mid-flight is money spent for no verdict and no fix.
    spent_so_far = _as_float(os.environ.get("SPENT_SO_FAR"))
    budget = run_budget()
    if budget_exceeded(spent_so_far, budget):
        emit_outputs(
            outcome=OUTCOME_BUDGET,
            spent_total=f"{spent_so_far:.4f}",
            new_base_sha=state.live,
            detail=f"run has spent ${spent_so_far:,.2f} of its ${budget:,.2f} allowance",
        )
        print(f"budget: ${spent_so_far:.2f} of ${budget:.2f} spent — refusing round")
        return 0

    workspace = os.environ.get("GITHUB_WORKSPACE", ".")
    # Bracket the agent call, not the whole job: checkout and token minting
    # cost nothing and would only widen the window other traffic can leak into.
    spend_before = gateway_spend()
    transcript = os.path.join(workspace, f"sdk-loop-{phase}-{round_no}.log")
    if phase == "review":
        print(f"::group::review round {round_no} — agent transcript")
        result = run_agent(
            REVIEW_MODEL,
            review_prompt(
                pr, round_no, state.live, ledger, os.environ.get("PRIOR_SHA", "")
            ),
            workspace,
            TIMEOUT_REVIEW_S,
            transcript_path=transcript,
        )
        print("::endgroup::")
        comments = json.loads(
            _sh(
                ["gh", "api", f"repos/{repo}/issues/{pr}/comments", "--paginate"]
            ).stdout
            or "[]"
        )
        outcome = interpret_review(
            result,
            newest_verdict(comments, answers_trigger=os.environ.get("COMMENT_ID")),
            state.live,
        )
        cost = spend_delta(spend_before, gateway_spend())
        emit_outputs(
            outcome=outcome.outcome,
            verdict=outcome.verdict,
            reviewed_head=outcome.reviewed_head,
            verdict_url=outcome.verdict_url,
            detail=outcome.detail,
            reaims="0",
            new_base_sha=state.live,
            cost="" if cost is None else f"{cost:.4f}",
            spent_total=(
                ""
                if running_total(spent_so_far, cost) is None
                else f"{running_total(spent_so_far, cost):.4f}"
            ),
        )
        print(f"review round {round_no}: {outcome.outcome} {outcome.verdict}")
        return 0 if outcome.outcome != OUTCOME_FAILED else 1

    if phase == "resolve":
        before = state.live
        print(f"::group::resolve round {round_no} — agent transcript")
        result = run_agent(
            RESOLVE_MODEL,
            resolve_prompt(pr, round_no, before, os.environ.get("VERDICT_URL", "")),
            workspace,
            TIMEOUT_RESOLVE_S,
            transcript_path=transcript,
        )
        print("::endgroup::")
        # Exclude our own transcript: it lands in the workspace and would
        # otherwise read as "the resolver changed something".
        dirty = "\n".join(
            line
            for line in _sh(
                ["git", "status", "--porcelain"], cwd=workspace
            ).stdout.splitlines()
            if "sdk-loop-" not in line
        ).strip()
        after = live_head(repo, head_ref)
        dismissals = parse_dismissals(f"{result.stdout}\n{result.stderr}")
        outcome = interpret_resolve(result, bool(dirty), before, after, dismissals)
        cost = spend_delta(spend_before, gateway_spend())
        for entry in outcome.dismissals:
            ledger.add(entry["id"], entry["rationale"], round_no)
        emit_outputs(
            outcome=outcome.outcome,
            pushed_sha=outcome.pushed_sha,
            reaims="0",
            new_base_sha=after,
            ledger=ledger.to_json(),
            detail=outcome.detail,
            cost="" if cost is None else f"{cost:.4f}",
            spent_total=(
                ""
                if running_total(spent_so_far, cost) is None
                else f"{running_total(spent_so_far, cost):.4f}"
            ),
        )
        print(f"resolve round {round_no}: {outcome.outcome}")
        return 0 if outcome.outcome != OUTCOME_FAILED else 1

    print(f"unknown phase {phase!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
