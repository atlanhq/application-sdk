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
import pathlib
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from sdk_loop_by_design import load_by_design
from sdk_loop_common import (
    MAX_ROUNDS,
    PLAYBOOK_RESOLVE,
    PLAYBOOK_REVIEW,
    RESOLVE_MODEL,
    REVIEW_MODEL,
    AgentResult,
    DismissalLedger,
    classify_scope,
    dispatch_set,
    emit_outputs,
    format_usage,
    format_usd,
    head_state,
    is_verdict_comment,
    opencode_usage,
    parse_answers_trigger,
    parse_reviewed_head,
    parse_verdict,
    reaim_exhausted,
    run_agent,
    solo_scope,
    token_budget,
    token_budget_exceeded,
    usage_cost_usd,
    usage_total,
)
from sdk_loop_findings import audit_comment, load_severity
from sdk_loop_live import FINDINGS_RELPATH, RedGreenJob, deliver, post_comment
from sdk_loop_pack import build_pack
from sdk_loop_pack import render as render_pack
from sdk_loop_prep import (
    OUTCOME_UPDATED,
    PrepResult,
    decide,
    failing_checks,
    needs_agent,
    pr_state,
)
from sdk_loop_refute import CROSS_FAMILY
from sdk_loop_routing import load_routing
from sdk_loop_rules import full_text_budget, load_corpus
from sdk_loop_rules import render as render_rules
from sdk_loop_rules import select as select_rules

#: Wall-clock per phase. Review is the slower of the two: it walks the whole
#: five-phase playbook including sub-agents, where a resolve round is mostly
#: targeted edits.
# Outer backstops, deliberately left generous. The idle watchdog in
# `run_agent` is what actually catches a dead agent, and it fires on silence
# rather than on elapsed time — so these only ever cap a phase that is still
# talking. Cutting them would trade a real (if slow) review for no review.
TIMEOUT_REVIEW_S = 45 * 60
TIMEOUT_RESOLVE_S = 30 * 60
#: Prep is bookkeeping, not review. If a mechanical fix has not landed in ten
#: minutes it is not mechanical, and the review will say so far more cheaply.
TIMEOUT_PREP_S = 10 * 60

#: The challenger gets one call over the findings; it does not need the review's
#: budget, and a refuter that wanders for half an hour is not refuting.
TIMEOUT_REFUTE_S = 8 * 60

#: How long render waits for red-green after the model stages finish. It has
#: usually been running for minutes by then; this is the tail, not the run.
REDGREEN_JOIN_S = 4 * 60

REFUTE_BRIEF = ".mothership/pr-loop/REFUTE.md"

#: Prepended to a dispatched specialist's context, after the playbook. The
#: playbook's output section tells the reader to write one JSON file; a
#: specialist returns its findings to the primary instead, which assembles and
#: writes the file. Everything else in the playbook — what counts as a
#: finding, the nit rules, the class sweep, the schema — applies unchanged.
SUBAGENT_RETURN_NOTE = """## You are a dispatched specialist

The primary reviewer dispatched you for your domain. Everything above about
what counts as a finding, severity, nits and the class sweep applies to you
unchanged — with one difference: **do not write a file**. Return your findings
as the JSON `findings` array in your reply, in the schema above, and list the
files you examined. The primary assembles the payload."""

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


def _as_int(raw: str | None) -> int | None:
    """Cumulative spend handed down the chain; empty means not measured."""
    if not (raw or "").strip():
        return None
    try:
        return int(raw)  # type: ignore[arg-type]
    except ValueError:
        return None


def running_total(spent_so_far: int | None, phase_cost: int | None) -> int | None:
    """Carry the tally forward, treating an unmeasured phase as a gap not a zero."""
    if spent_so_far is None and phase_cost is None:
        return None
    return (spent_so_far or 0) + (phase_cost or 0)


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


#: Announces the lane to the review playbook. See the CONTRACT comment beside
#: it in .mothership/pr-review/ORCHESTRATION.md — the string is shared with
#: that file and a test asserts they still match.
LANE_MARKER = "LANE: sdk-loop"


def prep_prompt(pr: int, failing: tuple[str, ...]) -> str:
    """Brief for the ONE case prep hands to a model: red checks, before review.

    Deliberately narrow. Everything prep normally does is deterministic and
    already done by the time this runs, so the model is not being asked to
    orchestrate — it is being asked whether a specific red check is the kind
    tooling can fix, and to fix it if so.

    The hard part of this brief is what it refuses. "Make CI green" has no
    terminating condition when a test is genuinely broken by the PR, and a
    phase with write scope chasing that is strictly worse than a review
    saying so in one line.
    """
    names = "\n".join(f"  - {n}" for n in failing[:10])
    return "\n".join(
        [
            f"PR #{pr} has failing checks before its review has run:",
            names,
            "",
            "Fix ONLY what is mechanical — the class where the tooling, not",
            "judgement, determines the answer:",
            "",
            "  * formatting and lint (`uv run pre-commit run --files <changed>`)",
            "  * generated-artifact drift, by re-running the generator",
            "  * an obviously stale lockfile the repo's own command regenerates",
            "",
            "Everything else — a failing test, a type error, a real behavioural",
            "break — STOP and change nothing. Those are findings, and the review",
            "that runs after you exists to raise them properly. A fix you push",
            "here arrives with no review behind it.",
            "",
            "Do NOT resolve merge conflicts. Do NOT wait for checks to go green;",
            "you cannot, and the run does not need you to. Do NOT re-run checks",
            "hoping for a different answer.",
            "",
            "If you fix something: run the repo's pre-commit over the files you",
            "touched, commit with a `ci:` or `chore:` conventional prefix, and",
            "push. If you fix nothing, say so in one line and exit — that is a",
            "perfectly good outcome and costs the run nothing.",
        ]
    )


def pr_title(repo: str, pr: int) -> str:
    out = _sh(
        ["gh", "pr", "view", str(pr), "--json", "title", "-q", ".title"], cwd=repo
    )
    return (out.stdout or "").strip()


def pr_files(repo: str, pr: int) -> list[str]:
    """Paths the PR touches. Deleted files included — classifying from
    `+++ b/` diff headers alone would miss them, as §11 warns."""
    out = _sh(
        [
            "gh",
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "files",
            "--jq",
            ".files[].path",
        ]
    ).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


def diff_lines(repo: str, pr: int) -> int:
    """Added + removed lines, for the `minor` fast-path threshold."""
    out = _sh(["gh", "pr", "diff", str(pr), "--repo", repo]).stdout
    return sum(1 for line in out.splitlines() if line[:1] in "+-")


def _read(path: str) -> str:
    """A pr-loop artefact, or empty when it is missing.

    Empty rather than raising: a missing brief should degrade the review, not
    abort the round. The prompt states what it could not load so the reader can
    see a thinner review for what it is.
    """
    try:
        return pathlib.Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def review_prompt(
    pr: int,
    round_no: int,
    sha: str,
    ledger: DismissalLedger,
    prior_sha: str = "",
    scope: str = "",
    solo: str = "",
    agents: Sequence[str] = (),
    pack: str = "",
    output_path: str = "",
) -> str:
    """Hand the reviewer the playbook, its brief and its context — do not send it looking.

    This used to open with "Read <playbook> and follow it exactly". That one
    sentence bought eight measured orientation turns before the diff was
    touched: reading a 1,700-line router in two calls because a default Read
    truncates, re-reading it per specialist, grepping for callers, deriving a
    scope the harness had already computed.

    Injection removes all of it. The playbook is 8.1 KB, the brief is one to two
    KB, and the pack is assembled in milliseconds from facts this process
    already holds — so the reviewer's first turn contains the diff, the files
    worth reading, which specialist it is, and what the gate found.

    The instructions that used to live here are gone with the thing they
    described. There is no "read it in one call with an explicit limit" because
    nothing is read; no "do not read the brief of an agent you dispatch" because
    briefs arrive inlined; no pointer into `.mothership/pr-review/` at all.
    """
    playbook = _read(PLAYBOOK_REVIEW)
    parts: list[str] = []

    if playbook:
        parts += [playbook, "", "---", ""]
    else:
        parts += [
            f"WARNING: {PLAYBOOK_REVIEW} could not be read. Review conservatively",
            'and return `"status": "partial"`.',
            "",
        ]

    # Kept although the new playbook carries no lane conditionals: the marker is
    # free, and a future artefact that does branch on lane would otherwise fail
    # silently by waiting for a line nobody sends.
    parts += [LANE_MARKER, ""]

    if solo:
        brief = _read(f".mothership/pr-loop/agents/{solo}.md")
        if brief:
            parts += [brief, "", "---", ""]
        parts += [
            f"You are the `{solo}` specialist and the only one on this PR.",
            "Review it yourself; no sub-agents are registered, so `Task` has",
            "nothing to delegate to.",
            "",
        ]
    elif agents:
        parts += [
            f"review_scope = `{scope}`. Registered for this PR: {', '.join(agents)}.",
            "Those, and only those, can be dispatched — do them in parallel.",
            # The delegation tool is `Task` on this runtime, not `Agent`. Naming
            # it is not cosmetic: an agent that reaches for the wrong name burns
            # a turn discovering the tool does not exist, and the routing has
            # already decided that these specialists run.
            "The delegation tool on this runtime is called `Task`.",
            "Each dispatched agent arrives with its own brief and this context",
            "already inlined. Do NOT read the brief of an agent you dispatch —",
            "that loads instructions you never execute into every turn that",
            "follows, which a measured review paid for four times over.",
            "",
        ]

    if pack:
        parts += [pack, "", "---", ""]

    if output_path:
        # REVIEW.md: "Write one JSON object to the path named in your prompt."
        # This is the naming. Without it the reviewer has been told to write
        # to a path and given none — and the runtime reads nothing.
        parts += [
            f"Write your JSON to `{output_path}`. Create the directory if needed.",
            "Post nothing to the PR yourself; the runner renders and posts.",
            "",
        ]

    parts += [
        f"PR #{pr}, sha {sha}, round {round_no} of {MAX_ROUNDS} of an @sdk-loop run.",
        "You are READ-ONLY: your token carries no write scope. Do not attempt to",
        "push, and do not treat a push failure as something to work around.",
        "",
    ]

    if prior_sha:
        parts += [
            f"A previous round reviewed {prior_sha}; the incremental change is",
            f"`git diff {prior_sha}..{sha}`. Use it for delta labelling and the",
            "nit rules. Do NOT narrow the review to it — BLOCKING, CRITICAL and",
            "HIGH findings are raised on any line of the PR, including code the",
            "resolver just pushed.",
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


def audit_verdict_contract(verdict_comment: dict[str, Any] | None) -> list[str]:
    """Check the model's own verdict comment against the renderer's contract.

    Step 1 of the pr-loop migration runs `sdk_loop_findings` BEHIND the current
    playbook: the reviewer still composes and posts the comment, and this
    reports where that comment differs from what the renderer would have
    guaranteed. It is the evidence that de-risks handing the renderer the job
    for real — before anything on the approval path depends on it.

    Deliberately non-fatal, and deliberately not a byte comparison. The comment
    carries model prose no renderer reproduces, so only the part with one
    correct answer is checked: the markers, the verdict token, and the
    empty-Findings invariant. Failing a round on a diagnostic would make the
    measurement cost a review.

    Returns the violations so a caller can assert on them; the phase only
    prints.
    """
    if verdict_comment is None:
        return []
    problems = audit_comment(verdict_comment.get("body") or "")
    if not problems:
        print("verdict contract: clean")
        return []
    print(f"::warning::verdict contract: {len(problems)} violation(s)")
    for problem in problems:
        print(f"  - {problem}")
    return problems


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
        "4. You are the ONLY phase in this loop that can push, so you own the",
        "   state you leave behind. Before you finish: run the repo's",
        "   pre-commit over the files you touched and push the result, and",
        "   update the branch if it has fallen behind base while you worked.",
        "   The review that runs next holds NO write scope — anything you",
        "   leave untidy it can only report, spending its budget on something",
        "   one command would have settled here.",
        "   Do NOT wait for CI and do NOT re-run checks: you cannot make them",
        "   finish, the next round reads the real state anyway, and waiting",
        "   has no terminating condition when a check is genuinely broken.",
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
    spent_so_far = _as_int(os.environ.get("SPENT_SO_FAR"))
    budget = token_budget()
    if token_budget_exceeded(spent_so_far, budget):
        emit_outputs(
            outcome=OUTCOME_BUDGET,
            spent_total=str(spent_so_far),
            new_base_sha=state.live,
            detail=f"used {spent_so_far:,} of its {budget:,} token allowance",
        )
        print(f"budget: {spent_so_far:,} of {budget:,} tokens used — refusing round")
        return 0

    workspace = os.environ.get("GITHUB_WORKSPACE", ".")
    transcript = os.path.join(workspace, f"sdk-loop-{phase}-{round_no}.log")
    if phase == "prep":
        # Deterministic pass first, and for a healthy PR that is the WHOLE
        # phase — no agent, no gateway call, a handful of `gh` reads. A model
        # cannot improve on "is mergeStateStatus BEHIND", and most PRs enter
        # the loop current and green, so paying one to confirm that would be
        # the same waste this phase exists to remove from the review.
        state = pr_state(repo, pr)
        # Conflicts short-circuit BEFORE the checks read. There is nothing
        # useful to say about CI on a branch that cannot merge, and the read
        # is a round trip spent to reach an answer that changes nothing.
        conflicted = state is not None and (
            state.get("mergeStateStatus") == "CONFLICTING"
            or state.get("mergeable") == "CONFLICTING"
        )
        result = decide(state, () if conflicted else failing_checks(repo, pr), baseline)

        if needs_agent(result):
            # The one case worth a model: red checks a mechanical fix might
            # clear. Bounded hard, because this phase must never become the
            # thing that spends an hour on a genuinely broken test — that is
            # the review-and-resolve loop's job, and it does it properly.
            print("::group::prep — mechanical fix attempt")
            agent = run_agent(
                RESOLVE_MODEL,
                prep_prompt(pr, result.failing),
                workspace,
                TIMEOUT_PREP_S,
                transcript_path=transcript,
            )
            print("::endgroup::")
            after = live_head(repo, head_ref)
            if after and after != result.new_base_sha:
                result = PrepResult(
                    OUTCOME_UPDATED,
                    new_base_sha=after,
                    pushed_sha=after,
                    ci_state="rechecking",
                    detail=f"pushed a mechanical fix for: {', '.join(result.failing[:3])}",
                )
            elif not agent.completed:
                print(f"prep agent aborted: {agent.abort_reason}")

        emit_outputs(
            outcome=result.outcome,
            new_base_sha=result.new_base_sha,
            pushed_sha=result.pushed_sha,
            ci_state=result.ci_state,
            detail=result.detail,
            reaims="0",
        )
        print(f"prep: {result.outcome} — {result.detail}")
        # NEVER fails the run. A prep that could not tidy the branch must not
        # cost the review: a branch left behind still reviews correctly, and
        # a conflict is the author's to resolve.
        return 0

    if phase == "review":
        # Classify BEFORE the model starts. §11's routing is pure file-list
        # arithmetic, so the harness can settle it deterministically — and it
        # has to, because whether registering sub-agents makes any sense is
        # decided by the answer. A scope that routes to ONE agent gets none
        # registered: `Task` then has nothing to dispatch to, and the rule
        # holds by construction rather than by the model choosing to follow a
        # paragraph. It did not follow the comparable "fetch once" paragraph.
        files = pr_files(repo, pr)
        scope = classify_scope(files, diff_lines(repo, pr))
        # `dispatch_set`, not SCOPE_AGENTS: the table is only §2a's Wave 1 row.
        # §1b adds `reachability` on full/mixed, and §2a's mixed-partition rule
        # adds a `ci-config` or `conformance` specialist when the PR also
        # carries those files. Registering the table alone left the parent
        # instructed to dispatch agents that did not exist.
        fan_out = dispatch_set(scope, files)
        solo = solo_scope(scope, files)
        print(
            f"scope={scope} agents={len(fan_out)}" + (f" solo={solo}" if solo else "")
        )

        # Everything determinate, before the model starts. The pack is the
        # reviewer's first turn; red-green needs nothing the model produces,
        # so it runs alongside the model stages and is joined before render.
        diff_text = _sh(["gh", "pr", "diff", str(pr)], cwd=repo).stdout or ""
        routing = load_routing()
        pack = build_pack(
            repo=pathlib.Path(repo), diff=diff_text, scope=scope, routing=routing
        )
        findings_path = pathlib.Path(workspace) / FINDINGS_RELPATH
        findings_path.parent.mkdir(parents=True, exist_ok=True)
        if findings_path.exists():
            findings_path.unlink()
        base_ref = os.environ.get("BASE_SHA") or "origin/main"
        redgreen_job = RedGreenJob(
            repo=pathlib.Path(repo),
            base_ref=base_ref,
            files=pack.files,
            workdir=pathlib.Path(workspace) / ".sdk-loop" / "redgreen-base",
        )

        # Everything a specialist needs beyond its brief, built once per
        # specialist: the shared judgement contract, the pack, and the rules
        # for these paths — full text where the diff contains what a rule is
        # about, by name otherwise. The first cutover gave sub-agents a brief
        # and nothing else: no contract, no diff, no rules.
        corpus = load_corpus()
        budget = full_text_budget()
        changed_paths = [f.path for f in pack.files]

        def context_for(specialist: str) -> str:
            rules = render_rules(
                select_rules(
                    corpus,
                    specialist=specialist,
                    changed_paths=changed_paths,
                    diff=diff_text,
                    budget_chars=budget,
                )
            )
            return render_pack(pack, specialist, rules_section=rules)

        playbook_text = _read(PLAYBOOK_REVIEW)
        subagent_context = {
            name: "\n\n---\n\n".join(
                p
                for p in (
                    playbook_text,
                    SUBAGENT_RETURN_NOTE,
                    context_for(name),
                )
                if p
            )
            for name in fan_out
        }

        print(f"::group::review round {round_no} — agent transcript")
        result = run_agent(
            REVIEW_MODEL,
            review_prompt(
                pr,
                round_no,
                state.live,
                ledger,
                os.environ.get("PRIOR_SHA", ""),
                scope=scope,
                solo=solo,
                agents=fan_out,
                pack=context_for(solo) if solo else render_pack(pack, "reviewer"),
                output_path=str(findings_path),
            ),
            workspace,
            TIMEOUT_REVIEW_S,
            transcript_path=transcript,
            subagents=not solo and bool(fan_out),
            subagent_names=fan_out,
            subagent_context=subagent_context,
        )
        print("::endgroup::")

        # The live path. The model wrote JSON and posted nothing; the runner
        # gates, challenges, renders and posts. If the gate fails there is no
        # comment, and the existing interpreter below reports the failure the
        # same way it always did — deliberately, not by accident.
        if result.completed and findings_path.exists():
            sev = load_severity()

            def challenge(prompt: str) -> str:
                out = run_agent(
                    RESOLVE_MODEL,
                    prompt,
                    workspace,
                    TIMEOUT_REFUTE_S,
                    transcript_path=transcript,
                )
                return out.stdout if out.completed else ""

            live = deliver(
                payload_text=findings_path.read_text(encoding="utf-8"),
                pack=pack,
                sev=sev,
                by_design=load_by_design(),
                challenge=challenge,
                challenge_brief=pathlib.Path(REFUTE_BRIEF).read_text(encoding="utf-8"),
                # RESOLVE_MODEL is a different family from REVIEW_MODEL, so the
                # strong form of the challenge is available on this lane.
                challenge_mode=CROSS_FAMILY,
                diff=diff_text,
                redgreen_report=redgreen_job.join(timeout_s=REDGREEN_JOIN_S),
                pr=pr,
                pr_title=pr_title(repo, pr),
                reviewed_head=state.live,
                answers_trigger=os.environ.get("COMMENT_ID") or None,
                model=REVIEW_MODEL,
                run_url=os.environ.get("GHA_RUN_URL", ""),
            )
            if live.should_post:
                url = post_comment(repo, pr, live.body, _sh)
                print(
                    f"posted {live.verdict}: {len(live.kept)} findings, "
                    f"{live.dropped} dropped, challenge={live.challenged} {url}"
                )
                if not url:
                    # A failed post must not be read as success from a stale
                    # comment. Without COMMENT_ID (workflow_dispatch) the
                    # ANSWERS_TRIGGER filter is a no-op, so newest_verdict
                    # would adopt an older same-SHA verdict on the PR.
                    emit_outputs(
                        outcome=OUTCOME_FAILED,
                        detail=(
                            "the review comment did not land — refusing to "
                            "adopt an existing verdict"
                        ),
                        new_base_sha=state.live,
                        reaims="0",
                    )
                    print(
                        f"review round {round_no}: {OUTCOME_FAILED} "
                        "post produced no URL"
                    )
                    return 1
            else:
                print(f"::warning::review not posted — {live.failure}")
        elif result.completed:
            print(f"::warning::the reviewer completed but wrote no {FINDINGS_RELPATH}")

        comments = json.loads(
            _sh(
                ["gh", "api", f"repos/{repo}/issues/{pr}/comments", "--paginate"]
            ).stdout
            or "[]"
        )
        verdict_comment = newest_verdict(
            comments, answers_trigger=os.environ.get("COMMENT_ID")
        )
        outcome = interpret_review(result, verdict_comment, state.live)
        audit_verdict_contract(verdict_comment)
        counts = opencode_usage(workspace)
        usage, cost = format_usage(counts), usage_total(counts)
        usd = usage_cost_usd(counts, REVIEW_MODEL)
        print(f"tokens: {usage} · cost: {format_usd(usd)}")
        emit_outputs(
            outcome=outcome.outcome,
            verdict=outcome.verdict,
            reviewed_head=outcome.reviewed_head,
            verdict_url=outcome.verdict_url,
            detail=outcome.detail,
            reaims="0",
            new_base_sha=state.live,
            cost="" if cost is None else str(cost),
            usd="" if usd is None else f"{usd:.6f}",
            usage=usage,
            spent_total=(
                ""
                if running_total(spent_so_far, cost) is None
                else str(running_total(spent_so_far, cost))
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
            if "sdk-loop-" not in line and ".sdk-loop-rgcfg" not in line
        ).strip()
        after = live_head(repo, head_ref)
        dismissals = parse_dismissals(f"{result.stdout}\n{result.stderr}")
        outcome = interpret_resolve(result, bool(dirty), before, after, dismissals)
        # Measured here rather than inherited: `cost` and `usage` were only
        # ever assigned inside the review branch, so every resolve phase
        # reached `emit_outputs` with both names unbound and died on a
        # NameError AFTER the resolver had already pushed its fix — the work
        # landed, the round was reported as failed, and the loop stopped.
        counts = opencode_usage(workspace)
        usage, cost = format_usage(counts), usage_total(counts)
        usd = usage_cost_usd(counts, RESOLVE_MODEL)
        print(f"tokens: {usage} · cost: {format_usd(usd)}")
        for entry in outcome.dismissals:
            ledger.add(entry["id"], entry["rationale"], round_no)
        emit_outputs(
            outcome=outcome.outcome,
            pushed_sha=outcome.pushed_sha,
            reaims="0",
            new_base_sha=after,
            ledger=ledger.to_json(),
            detail=outcome.detail,
            cost="" if cost is None else str(cost),
            usd="" if usd is None else f"{usd:.6f}",
            usage=usage,
            spent_total=(
                ""
                if running_total(spent_so_far, cost) is None
                else str(running_total(spent_so_far, cost))
            ),
        )
        print(f"resolve round {round_no}: {outcome.outcome}")
        return 0 if outcome.outcome != OUTCOME_FAILED else 1

    print(f"unknown phase {phase!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
