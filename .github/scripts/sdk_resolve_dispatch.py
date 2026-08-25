#!/usr/bin/env python3
"""Dispatch an SDK Resolve run to mothership's Rover Direct API and stream it.

The resolver is the WRITE counterpart to @sdk-review: it drives an open PR to
merge-ready by fixing CI + every reviewer finding (nits included, unless proven
false), looping review->fix->push until zero findings + green CI + READY_TO_MERGE
— then STOPS (a human merges). It runs in its OWN mothership sandbox, separate
from the read-only reviewer sandbox; all its logic lives in
`.mothership/pr-resolve/ORCHESTRATION.md`.

Dispatch + SSE parsing + GITHUB_STEP_SUMMARY rendering live here (tested) rather
than in inline workflow shell, per docs/standards/ci.md. Parses the
`=== SDK RESOLVE SUMMARY ===` block the resolver emits (ORCHESTRATION Phase 4).

One re-dispatch is allowed when the sandbox dies on a hard error a second
attempt could survive — see MAX_DISPATCH_ATTEMPTS and retry_decision(). Two
classes qualify, and they differ on which model attempt 2 runs: a model/provider
fault swaps the model (FND-641), while a fault in mothership's own sandbox-api
wrapper keeps it (FND-764), because nothing about the model failed there. The
retry fires only after the out-of-band poll has come back empty, which is the
single point where the sandbox is provably dead and nothing further will land;
every other stream ending stays fail-fast.

That poll establishes "nothing was DELIVERED", not "nothing was PUSHED" — it
only looks for the Phase-4 hand-off, so a resolver that pushed in earlier rounds
and died before Phase 4 leaves it empty too. Each retry class therefore has to
carry its own evidence that no first resolver exists: the model-fault class
because the sandbox died on its own first turn, and the sandbox-api class
because it additionally requires an empty `cost_usd` (see `billed_nothing`).

That is a strictly narrower rule than the review lane's, on purpose (FND-647).
The reviewer also retries a fault it cannot prove killed the sandbox, because
two reviewers only ever cost a duplicate comment that FND-636's dedupe
collapses. A resolver PUSHES COMMITS: two live on one branch is what
`sdk_resolve_push_guard.py` exists to prevent and nothing can undo afterwards.
So on this lane an unproven death is never re-dispatched — a dropped RPC buys
the LONG out-of-band poll instead (see `oob_poll_budget`), which is a recovery
that cannot double-run anything. `retry_decision` has to say that explicitly:
the fault otherwise reads as a no-op run to `resolved_nothing` and would be
re-dispatched on the strength of it.

Environment:
    MOTHERSHIP_URL      base URL (e.g. https://mothership.atlan.dev)
    HARNESS_TOKEN       bearer for /api/sandbox/execute
    PR_NUMBER           the open PR to drive to merge-ready
    MAX_ROUNDS          max @sdk-review rounds before stopping (default 8)
    REVIEWERS           comma-separated GitHub handles to request + tag at the
                        end; fed by vars.SDK_RESOLVE_REVIEWERS. Optional, no
                        default — unset means no reviewer is requested and the
                        run logs a warning.
    REQUESTER           login that invoked @sdk-resolve (also tagged)
    GHA_RUN_URL         this workflow run's URL
    RUN_DATE            ISO date (computed if absent)
    GITHUB_STEP_SUMMARY path GitHub Actions gives us to render the run summary
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from datetime import datetime, timezone
from typing import Any, NamedTuple

HEALTH_RETRIES = 5
HEALTH_BACKOFF_SECONDS = 5
STREAM_TIMEOUT_SECONDS = 7200
# Per-read socket idle watchdog: if no bytes arrive for this long the stream is
# considered stalled and the runner is freed, instead of blocking the whole 2h.
# Set just above mothership's own idle_timeout_seconds (1800) so we only fire
# once mothership has itself given up on an idle session.
READ_IDLE_TIMEOUT_SECONDS = 1900
# Tail-cap for the mined buffers: over a 2h stream the raw/response text would
# otherwise grow unbounded. The Phase 4 summary block sits at the very end, so
# keeping the last N bytes always preserves it.
BUFFER_CAP_BYTES = 65536
DEFAULT_MAX_ROUNDS = 8
# Models this lane runs on, mirroring the review lane. The main lane is pinned
# to Grok 4.6 by operator request, replacing kimi-k3 (chosen on cost per TASK,
# not per token: index 57.2, ~$0.85/task vs claude-opus-5 at 60.5, ~$2.40).
# Both proxy sides have to allow it or every dispatch dies on turn one, and the
# code says which: 400 means the pinned name is not one the
# llmproxy.atlan.dev catalog recognises (its own message is "Invalid model
# name passed in model=..."), 403 means the gateway key does not allowlist it.
# This lane sends the `sdk_review` alias, so its spend is billed to the
# shared SDK-lane key LITELLM_KEY_SDK_REVIEW, and resolve runs therefore
# share the review lane's budget.
#
# Mothership itself does not validate the name at all: `_validate_model_ids`
# in `harness/api/models/sandbox.py` only rejects blank/whitespace/control-char
# values, by design ("No allow-list of names: new models must work without a
# code change"). So a typo in this constant is never caught at dispatch - it
# boots a real sandbox, bills for it, and only dies mid-run when the container
# itself calls the proxy. That is exactly what happened for FND-660: this
# constant carried the OpenRouter-style `x-ai/` prefix instead of this proxy's
# `xai/`, so any dispatch that reached the sandbox would have paid for one
# and failed on its first proxy call.
#
# KNOWN RISK, carried deliberately: xAI does NOT prompt-cache on the Anthropic
# `/v1/messages` route Claude Code uses (verified in the LiteLLM ledger when
# mothership pinned its PR reviewer to xai/grok-4.5 in Jul 2026, and the reason
# it reverted), so a multi-turn agentic lane re-bills its full context every
# turn — and resolve runs up to DEFAULT_MAX_ROUNDS rounds per PR. That risk was
# measured on grok-4.5, not 4.6: a manual `/v1/messages` probe of
# `xai/grok-4.6` on 2026-08-20 returned a non-zero `cache_read_input_tokens`,
# so the no-caching claim is unverified for 4.6 and the per-run cost should be
# re-measured before it is treated as settled.
#
# The fast lane stays on gpt-5.6-luna. Reverting is a one-liner.
MAIN_MODEL = "xai/grok-4.6"
FAST_MODEL = "gpt-5.6-luna"

# --- Re-dispatch when the sandbox dies on a hard error ----------------------
# One retry, on a DIFFERENT main model. Mothership's intra-group provider
# fallback already exists and already fires on a 429 — but every provider in a
# model's group serves the SAME model, so a same-model re-dispatch just re-hits
# the same model-level fault (observed while this lane was on kimi-k3: the 429
# fell through to Moonshot AI, which returned 400 "the message at position 21
# with role 'assistant' must not be empty" — same model, same bug). Swapping the
# model is the whole point of the retry; without it the second sandbox boot
# fails identically.
MAX_DISPATCH_ATTEMPTS = 2
# Attempt 2's main model. This is mothership's own DEFAULT_CLAUDE_MODEL, named
# explicitly rather than by omitting `model` from the payload: an explicit
# constant lets a test assert the two attempts actually differ, and keeps the
# retry from silently following a mothership config change. `small_fast_model`
# and CLAUDE_CODE_SUBAGENT_MODEL stay pinned to FAST_MODEL on the retry —
# mothership's model_routing_env does `fast = small_fast_model or model`, so
# changing only `model` must not be allowed to drag the background lane along.
RETRY_MAIN_MODEL = "claude-opus-5"
# Retry only a fault a different model can plausibly survive. This is an
# allowlist, not a denylist: a wrong retry burns a second sandbox boot — up to
# an hour of the job's 130-min budget plus a real bill — so an unrecognised
# cause stays fail-fast. A recognised code decides on its own; the message is
# consulted ONLY when the code carries no information at all.
RETRYABLE_ERR_CODES = frozenset(
    {
        "400",
        "429",
        "500",
        "502",
        "503",
        "504",
        "api_error",
        "overloaded_error",
        "provider_error",
        "rate_limit_error",
        "sandbox_error",
    }
)
# Message substrings that identify a model/provider fault. These are a fallback
# for one specific case: a `complete` event flattens an absent error code to
# `none`, and a standalone `error` event defaults it to `unknown`, while both
# still forward the provider's own text. They must NEVER override a code that
# does carry information — "401 upstream auth failed" mentions upstream and is
# nonetheless a permanent fault that would fail identically on any model.
RETRYABLE_ERR_PATTERNS = (
    "must not be empty",  # the empty-assistant-turn fault (first seen on kimi-k3)
    "rate-limited",
    "rate limited",
    "overloaded",
    "temporarily unavailable",
    "upstream",
)
UNINFORMATIVE_ERR_CODES = frozenset({"", "none", "unknown"})
# FND-647. A fault in mothership's own RPC to the sandbox, not in the model —
# observed as `code=unknown` + "ReadableStream received over RPC disconnected
# prematurely." on run 32309963717 (PR #3276), which threw away a healthy $0.69
# run. Read only when the code carries no information, exactly like the
# model-fault patterns above.
#
# On THIS lane the class never authorises a re-dispatch: the drop is in the pipe,
# so it is no evidence the resolver died, and a second resolver pushing to one
# branch is unrecoverable. What it selects is the long out-of-band poll (the
# resolver may be many minutes from its Phase 4 hand-off) plus a refusal that
# says so. The review lane keeps its own copy of this tuple and DOES retry on
# it; the tuples are deliberately per-lane, because the entry conditions are.
RETRYABLE_TRANSPORT_PATTERNS = ("disconnected prematurely",)
# Causes that fail identically on any model — never spend a second sandbox:
#   elicitation   the sandbox wants interactive input, which GHA can never give.
#   stream_error  OUR urlopen died, not the sandbox. The resolver is probably
#                 still alive and working, so a re-dispatch would double-run it
#                 — the exact thing the hook point below is chosen to avoid.
# Auth/permission and prompt faults are excluded by the allowlist above rather
# than named here: they will fail the same way on any model, forever.
NEVER_RETRY_ERR_CODES = frozenset({"elicitation", "stream_error"})
# FND-764. mothership's own sandbox-api codespace: `code=internal` carries a
# message stamped with the wrapper that reported it (`_err_msg(e,
# "sandbox-api/_execute_sync")` in its `sandbox_api.py`). Every observed
# instance is plumbing on mothership's side of the API, never the model:
#
#   [sandbox-api/_execute_sync] generator didn't stop after throw()     (x8)
#   [sandbox-api/_execute_sync] Failed to clone repository: ...         (x1)
#                               Command timeout after 60000ms
#
# Those two do NOT share a cause — the clone timeout is legible and plainly
# transient, while `generator didn't stop after throw()` is mothership's
# `langfuse_lifecycle_span` swallowing the real exception and yielding a second
# time (FND-765), so the actual fault is destroyed before it reaches us. What
# they share is what licenses the retry: nothing was delivered and nothing was
# billed. Both arrive with an empty `cost_usd` while other failures on this lane
# report real spend ($0.48 / $2.26 / $6.58 / $28.34), so no model produced a
# token, and all 9 affected PRs were checked for a resolver push or Phase-4
# hand-off — there were none.
#
# That empty `cost_usd` is CHECKED, not merely observed — see `billed_nothing`.
# It has to be, because it is the whole safety argument on a lane that pushes
# commits, and it does not follow from anything else the classifier sees. The
# masked shape is `langfuse_lifecycle_span` destroying the real exception, so
# the wrapper fault can originate anywhere inside `_execute_sync` — including
# AFTER a run did work — and such an occurrence would be byte-identical to the
# 9 pre-first-token ones. The out-of-band poll does not cover the gap either:
# it runs for OOB_POLL_SECONDS_HARD_ERROR and looks only for the Phase-4
# hand-off, so a resolver that pushed in rounds 1..N without reaching Phase 4
# leaves it empty. "The poll came back empty" is not "nothing was pushed".
#
# This conjunct is the ONE place this class deliberately diverges from the
# review lane's copy, for the same reason RETRYABLE_TRANSPORT_PATTERNS diverges:
# the entry conditions are per-lane. A second reviewer costs a duplicate comment
# that FND-636's dedupe collapses; a second resolver pushes to a branch and
# cannot be undone.
#
# Matched on the code AND the message, not the code alone: `internal` is a
# generic label and a future non-plumbing use of it must not inherit a retry
# it was never argued for. The message test keeps this an allowlist instead of
# letting the class rot into a denylist. It is a substring rather than a prefix
# test because mothership does not consistently deliver the message bare: the
# transport class on this same lane exists because the provider text arrived
# wrapped in a nested JSON blob, and `[sandbox-api/` is specific enough that
# matching it anywhere in the message keeps the allowlist just as tight.
SANDBOX_API_ERR_CODE = "internal"
SANDBOX_API_ERR_MARKER = "[sandbox-api/"
# Long enough to survive the clone-timeout shape, which is ~150 chars and buries
# its actual cause ("Command timeout after 60000ms") at the very end, behind the
# JSON envelope. That is the ONE variant of this class whose message carries a
# recoverable cause, so truncating it at the 80 the other classes use would
# discard the only thing worth reading in the line a human triages from.
SANDBOX_API_ERR_PREVIEW_CHARS = 200
# Dispatch-level HTTP statuses live in their own `http_` codespace, kept apart
# from the stream's provider codes on purpose: a provider 400 carried inside the
# stream is worth a different model, while a 400 on the POST itself is a
# malformed payload that fails identically on any model. Only 429 and 5xx mean
# "the far side was transiently unable to accept the request".
RETRYABLE_DISPATCH_HTTP_CODES = frozenset(
    {"http_429", "http_500", "http_502", "http_503", "http_504"}
)
# The job caps at 130 min (`timeout-minutes: 130` in sdk-resolve.yml) and the VPN
# steps eat a few of those before dispatch starts, so budget this step at 110 min
# from its own start. A retry is only worth booting with enough of that left to
# finish at least one review->fix round; below the floor, fail fast rather than
# burn a sandbox the runner will kill mid-flight. Cancelling the job does NOT
# stop the sandbox, so the retry's own `max_timeout_seconds` is clamped to what
# remains — otherwise a killed runner leaves it billing for up to 2h unattended.
DISPATCH_BUDGET_SECONDS = 6600
RETRY_MIN_REMAINING_SECONDS = 1800

# --- Out-of-band hand-off backstop -----------------------------------------
# The resolver runs in its OWN mothership sandbox; our SSE stream only observes
# it. That stream can be cut (proxy/VPN) minutes — even tens of minutes — before
# the resolver actually finishes and posts its Phase-4 SDK_RESOLVE_SUMMARY
# hand-off comment (observed: a stream dropped ~30 min before the resolver
# reached READY_TO_MERGE and handed off). So when our stream ends unhappily,
# poll the PR for that comment before reporting failure — its presence, newer
# than this run's start, is out-of-band proof the run completed.
RESOLVE_SUMMARY_MARKER = "<!-- SDK_RESOLVE_SUMMARY -->"
OOB_POLL_INTERVAL_SECONDS = 30
# Transport drop (stream cut, sandbox presumed still working): poll long enough
# to catch a late hand-off. Kept well under the job's 130-min timeout.
OOB_POLL_SECONDS_STREAM_DROP = 2700
# Abnormal sandbox termination (status=error / error event): the sandbox is dead
# and will post nothing more — a couple of quick checks only catch a summary that
# landed just before it died. NOT used for a transport fault, which reports a
# terminal error without the sandbox being dead (FND-647): those take the
# stream-drop budget above.
OOB_POLL_SECONDS_HARD_ERROR = 120
# Clock-skew margin subtracted from this run's start when matching a summary's
# created_at, so a hand-off posted right at the boundary isn't missed. Small
# enough that a prior run's (minutes-older) summary is never mistaken for ours.
OOB_SINCE_SKEW_SECONDS = 120

SUMMARY_START = "=== SDK RESOLVE SUMMARY ==="
SUMMARY_END = "=== END SUMMARY ==="
# Summary rows only a run that reached Phase 4 can carry. Any one of them is
# proof of work; their absence is proof the resolver never got there.
TERMINAL_SUMMARY_KEYS = ("final_verdict", "merge_ready", "stopped_reason")
# Rows rendered into the GitHub step summary, in order.
SUMMARY_ROWS = (
    "rounds",
    "findings_fixed",
    "findings_dismissed",
    "ci",
    "final_verdict",
    "merge_ready",
    "stopped_reason",
)


def build_prompt(
    pr_number: str, gha_run_url: str, max_rounds: int, reviewers: str, requester: str
) -> str:
    # Reviewers to request + the requester who triggered the run — tagged at the
    # end so a human takes the merge from a green, review-requested PR.
    tag_list = ", ".join(f"@{h}" for h in _reviewer_handles(reviewers, requester))
    # REVIEWERS comes from a repo variable and may be unset. Build the
    # reviewer-request instruction from the cleaned list so an empty value
    # can't produce an argument-less `gh pr edit --add-reviewer`, which fails
    # and burns a round. Unset does NOT silently skip the hand-off: the
    # resolver is told to state that no reviewer list is configured, and the
    # dispatch step logs a ::warning:: as well. (CODEOWNERS auto-request only
    # fires when a PR is opened, so it is not a fallback for a PR this
    # resolver has been iterating on.)
    request_list = ",".join(_reviewer_handles(reviewers, ""))
    review_request = (
        f"run `gh pr edit {pr_number} --add-reviewer {request_list}` "
        '(ignore "can\'t request from the author" errors) and '
        if request_list
        else "state in the report that NO reviewer list is configured "
        "(vars.SDK_RESOLVE_REVIEWERS is unset) so a human assigns one, then "
    )
    return f"""You are running the SDK Resolver in a Cloudflare sandbox.

Repository cloned at: /workspace/application-sdk.
Working directory: cd /workspace/application-sdk

Read and follow the orchestration EXACTLY:
  .mothership/pr-resolve/ORCHESTRATION.md
  .mothership/pr-resolve/CLAUDE.md

Run metadata (use these verbatim):
  PR_NUMBER:    {pr_number}
  MAX_ROUNDS:   {max_rounds}
  GHA_RUN_URL:  {gha_run_url}
  REVIEWERS:    {reviewers}          # GitHub handles to request as reviewers
  REQUESTER:    {requester}          # who invoked @sdk-resolve
  TAG_LIST:     {tag_list}           # @-mention these at the end

GITHUB_TOKEN is pre-injected by the sandbox. Use `gh` for all GitHub operations.

Drive PR #{pr_number} to MERGE-READY: green required CI + zero @sdk-review
findings (nits included, unless proven false with a recorded rationale) +
verdict READY_TO_MERGE. You are the only writer — the reviewer runs in its own
separate sandbox; you trigger it with `@sdk-review` (post as-is; the reviewer
workflow now accepts the sandbox bot identity) and consume its comment.
Do NOT `gh pr merge` — stop at merge-ready and hand back to a human. When you
finish (merge-ready OR NEEDS_HUMAN), REQUEST HUMAN REVIEW: {review_request}post
the final report @-mentioning {tag_list} so they know it's their turn. Expect each push to reset the reviewer labels/status
(reset-on-push) — that is normal; key the loop off findings + CI, not labels.
Stop after MAX_ROUNDS rounds, or if a dismissed finding is re-raised, and
report. At the very end print the `=== SDK RESOLVE SUMMARY ===` block from
ORCHESTRATION Phase 4 verbatim."""


def _reviewer_handles(reviewers: str, requester: str) -> list[str]:
    """Ordered, de-duplicated GitHub handles: configured reviewers + requester."""
    handles: list[str] = []
    for h in [*reviewers.split(","), requester]:
        h = h.strip().lstrip("@")
        if h and h not in handles:
            handles.append(h)
    return handles


def attempt_model(attempt: int) -> str:
    """Default main model for this attempt — the swap a model-fault retry needs.

    Only a default, and only ever right for attempt 1: `retry_decision()` names
    the model for the attempt it authorises, because the retry classes differ on
    exactly this axis. A same-model re-dispatch (mothership's own sandbox-api
    wrapper faulted, FND-764) must NOT be dragged onto RETRY_MAIN_MODEL —
    nothing about the model failed.
    """
    return MAIN_MODEL if attempt <= 1 else RETRY_MAIN_MODEL


def build_payload(
    pr_number: str,
    gha_run_url: str,
    max_rounds: int,
    run_date: str,
    reviewers: str,
    requester: str,
    *,
    model: str,
    attempt: int = 1,
    max_timeout_seconds: int = STREAM_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    # `attempt` > 1 is a re-dispatch after the previous sandbox died on a hard
    # error: same prompt (the resolver re-reads live PR state in Phase 0, so the
    # second sandbox resumes wherever the first stopped), on whichever main
    # model the caller's RetryPlan named — a model fault swaps it, a fault in
    # mothership's own wrapper keeps it. The source_id is suffixed so the two
    # runs are distinguishable on the mothership side rather than colliding on
    # one id.
    suffix = "" if attempt <= 1 else f"-retry{attempt - 1}"
    return {
        "mode": "direct",
        "stream": True,
        "source": "github-comment",
        "source_id": f"sdk-resolve-{pr_number}-{run_date}{suffix}",
        "repositories": ["atlanhq/application-sdk"],
        "base_branch": "main",
        "snapshot": "_base",
        "ai_gateway_key_name": "sdk_review",
        # Model pinning, same three lanes as sdk-review.yml (PR #2985). Without
        # these the lane inherits mothership's DEFAULT_CLAUDE_MODEL
        # (claude-opus-5) and `_base` leaves CLAUDE_CODE_SUBAGENT_MODEL on
        # claude-sonnet-5, so Task/Explore legwork bills Claude rates whatever
        # the main lane runs. `small_fast_model` must be pinned explicitly:
        # mothership's model_routing_env does `fast = small_fast_model or model`,
        # so pinning `model` alone would put the background lane on MAIN_MODEL.
        # `model` is required from the caller because it is NOT a function of
        # the attempt number — a sandbox-api plumbing fault retries on the SAME
        # model where a model fault swaps it (see RetryPlan). Deliberately no
        # `or attempt_model(attempt)` fallback: that would let a caller that
        # forgot the argument silently get the swap back.
        "model": model,
        "small_fast_model": FAST_MODEL,
        "env_vars": {"CLAUDE_CODE_SUBAGENT_MODEL": FAST_MODEL},
        "prompt": build_prompt(
            pr_number, gha_run_url, max_rounds, reviewers, requester
        ),
        "max_timeout_seconds": max_timeout_seconds,
        "idle_timeout_seconds": 1800,
        "metadata": {
            "pr_number": pr_number,
            "max_rounds": max_rounds,
            "run_date": run_date,
            "reviewers": reviewers,
            "requester": requester,
            "attempt": attempt,
        },
    }


class SSEState:
    """Accumulates the outcome of the Rover Direct SSE stream."""

    def __init__(self) -> None:
        self.event = ""
        self.got_event = False
        self.completed = False
        self.errored = False
        self.status = ""
        self.cost = ""
        self.err_code = ""
        self.err_msg = ""
        # Two buffers mined for the Phase 4 summary block (mothership's surface
        # for the block isn't guaranteed — verify on first real dispatch):
        #  - response_text: `response` event text, concatenated with NO
        #    separator so delta fragments reassemble intact.
        #  - raw_data: every `data:` payload verbatim, one per line, so a block
        #    arriving as raw stdout lines is still recoverable.
        self.response_text = ""
        self.raw_data = ""


def _jget(data: str, *keys: str, default: str = "") -> str:
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return default
    for k in keys:
        if isinstance(obj, dict) and k in obj:
            obj = obj[k]
        else:
            return default
    return str(obj) if obj is not None else default


def _response_text(data: str) -> str:
    """Best-effort extraction of human-readable text from a `response` event."""
    try:
        obj = json.loads(data)
    except json.JSONDecodeError:
        return data
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        for key in ("text", "content", "message", "response", "delta", "output"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        return ""
    return ""


def process_line(line: str, st: SSEState) -> str | None:
    """Apply one raw SSE line to the state. Returns a log line (or None)."""
    if line == "":
        st.event = ""
        return None
    if line.startswith(":"):
        return None
    if line.startswith("event: "):
        st.event = line[len("event: ") :]
        st.got_event = True
        return None
    if line.startswith("id: "):
        return None
    if not line.startswith("data: "):
        return None

    data = line[len("data: ") :]
    # Event-agnostic capture so the Phase 4 block survives whichever event
    # carries it. Tail-capped so a 2h stream can't grow the buffer unbounded.
    st.raw_data = (st.raw_data + data + "\n")[-BUFFER_CAP_BYTES:]
    if st.event == "started":
        return f"[started]   session={_jget(data, 'session_id')} sandbox={_jget(data, 'sandbox_id')}"
    if st.event == "action":
        name = _jget(data, "action_name")
        return f"[action]    {name}" if name else None
    if st.event == "thought":
        return None
    if st.event == "response":
        # No separator — responses may stream as deltas. Tail-capped like raw_data.
        st.response_text = (st.response_text + _response_text(data))[-BUFFER_CAP_BYTES:]
        return "[response]  (agent posted a response)"
    if st.event == "elicitation":
        st.errored = True
        st.err_code = "elicitation"
        st.err_msg = "Sandbox requires interactive input; cannot answer from GHA"
        return "[elicit]    sandbox requested user input — treating as error"
    if st.event == "error":
        st.errored = True
        st.err_code = _jget(data, "code", default="unknown")
        st.err_msg = _jget(data, "message")
        return f"[error]     code={st.err_code} message={st.err_msg}"
    if st.event == "complete":
        st.completed = True
        st.status = _jget(data, "status", default="unknown")
        st.cost = _jget(data, "cost_usd")
        if st.status == "error":
            # A `complete` carrying status=error is as terminal as a standalone
            # `error` event, so flag it the same way — otherwise decide_exit and
            # the step summary parse the reason and then throw it away, leaving
            # the log with only "final status=error" and no code/message.
            # Gated on there actually being detail: with an empty error object
            # the bare `status=...` line downstream says more than `code=none`.
            code = _jget(data, "error", "code")
            st.err_code = code or "none"
            st.err_msg = _jget(data, "error", "message")
            st.errored = bool(code or st.err_msg)
        return f"[complete]  status={st.status} cost_usd={st.cost}"
    return None


def process_stream(lines: Iterable[str]) -> SSEState:
    st = SSEState()
    for raw in lines:
        msg = process_line(raw.rstrip("\n"), st)
        if msg:
            print(msg)
    return st


def parse_summary(text: str) -> dict[str, str]:
    """Extract the Phase 4 `key: value` block if present (tolerates \\n escaping)."""
    normalized = text.replace("\\n", "\n")
    start = normalized.find(SUMMARY_START)
    if start == -1:
        return {}
    end = normalized.find(SUMMARY_END, start)
    body = normalized[start + len(SUMMARY_START) : end if end != -1 else None]
    out: dict[str, str] = {}
    for line in body.splitlines():
        m = re.match(r"\s*([a-z_]+)\s*:\s*(.+?)\s*$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def mine_summary(st: SSEState) -> dict[str, str]:
    """Find the Phase 4 block in either buffer (response text, then raw)."""
    return parse_summary(st.response_text) or parse_summary(st.raw_data)


def run_completed(st: SSEState) -> bool:
    """True when the resolver actually finished its work.

    Proof of work is the Phase 4 `=== SDK RESOLVE SUMMARY ===` block, which the
    resolver emits as its very last action (ORCHESTRATION Phase 4) — never the
    transport `complete` sentinel. The sentinel reports only that the sandbox
    stopped, and a sandbox can stop having done nothing at all: two dispatches
    ended `status=completed` after 3-5 min having posted no `@sdk-review`
    trigger, no status comment and no report, and pushed nothing, yet both runs
    went green because the sentinel alone decided the exit code. Conversely,
    mothership sometimes ends a fully successful stream cleanly (EOF) with no
    sentinel at all. So the summary decides in both directions; the sentinel
    decides only whether the sandbox is still alive, which is
    `sandbox_terminated_abnormally`'s job.
    """
    if st.errored:
        return False
    if st.completed and st.status != "completed":
        # A terminal status the sandbox itself calls a failure. `process_line`
        # leaves `errored` False for a `complete` carrying status=error with an
        # empty error object, so the check above does not cover this — and
        # without it a run that streamed its Phase 4 summary and *then* died
        # would render as merge-ready in the step summary while `decide_exit`
        # returned 1. Terminal status and proof of work are both consulted, so
        # the two never diverge.
        return False
    summary = mine_summary(st)
    return any(k in summary for k in TERMINAL_SUMMARY_KEYS)


def resolved_nothing(st: SSEState) -> bool:
    """True for a no-op run: the sandbox reported success but did no work.

    Distinct from every other failure shape in that nothing went wrong at the
    transport or provider layer — the model simply ended its turn before
    working through the ORCHESTRATION. Nothing was pushed and nothing was
    posted, so re-dispatching is both safe and the only way forward.
    """
    return st.completed and st.status == "completed" and not run_completed(st)


def _rounds_completed(summary: dict[str, str]) -> int | None:
    """Parsed `rounds` count from the summary block, or None if absent/malformed."""
    try:
        return int(summary["rounds"].strip())
    except (KeyError, ValueError, AttributeError):
        return None


class RetryPlan(NamedTuple):
    """Whether to re-dispatch, why, and which main model attempt N+1 runs.

    `model` is empty when `retry` is False. It cannot be derived from the
    attempt number alone: the retry classes differ on exactly that axis — a
    dead sandbox needs a DIFFERENT model, a sandbox-api plumbing fault needs
    the SAME one. Ported from the review lane, which already had to make this
    distinction (FND-645 / FND-647).
    """

    retry: bool
    reason: str
    model: str = ""


class Attempt(NamedTuple):
    """One dispatch of the run: which model it ran, and how it ended."""

    number: int
    model: str
    state: SSEState


def _md_cell(text: str) -> str:
    """Flatten arbitrary error text into one Markdown table cell."""
    # Truncate BEFORE escaping: the other order can cut mid-escape and leave a
    # trailing backslash that swallows the cell delimiter.
    return " ".join(text.split())[:200].replace("|", "\\|")


def total_cost(attempts: Sequence[Attempt]) -> str:
    """Summed `cost_usd` across attempts, or "" when none reported one.

    Mothership's cost telemetry is already unreliable (runs that streamed
    hundreds of responses have reported an empty or zero cost), so an
    unparseable value is skipped rather than treated as zero — and the caller
    renders the per-attempt breakdown alongside this total so a retry ladder
    cannot hide its spend behind one number.
    """
    total = 0.0
    seen = False
    for a in attempts:
        try:
            total += float(a.state.cost)
        except (TypeError, ValueError):
            continue
        seen = True
    if not seen:
        return ""
    return f"{total:.4f}".rstrip("0").rstrip(".")


def render_attempt_trail(attempts: Sequence[Attempt]) -> list[str]:
    """Per-attempt model/status/cost rows; empty unless a retry actually ran."""
    if len(attempts) < 2:
        return []
    lines = [
        "",
        "### Attempts",
        "",
        "| # | Model | Status | Cost (USD) | Error |",
        "|---|---|---|---|---|",
    ]
    missing_cost = False
    for a in attempts:
        st = a.state
        status = st.status or ("error" if st.errored else "no `complete` event")
        err = f"`{st.err_code}` {st.err_msg}".strip() if st.errored else ""
        if not st.cost:
            missing_cost = True
        lines.append(
            f"| {a.number} | `{a.model}` | {status} | {st.cost or 'n/a'} | "
            f"{_md_cell(err)} |"
        )
    if missing_cost:
        lines += [
            "",
            "> One or more attempts reported no `cost_usd` — the total above "
            "is a lower bound.",
        ]
    return lines


def render_step_summary(
    st: SSEState,
    pr_number: str,
    gha_run_url: str,
    oob_url: str | None = None,
    attempts: Sequence[Attempt] = (),
) -> str:
    """Build the Markdown written to GITHUB_STEP_SUMMARY — always renders.

    `oob_url`, when set, is the resolver's out-of-band Phase-4 hand-off comment
    found by polling after our stream was cut: the run completed even though the
    stream reported failure, so render it as a recovered success.

    `attempts` carries every dispatch this run made. With a single attempt it
    changes nothing; with a retry it adds the per-attempt cost trail and makes
    the headline cost the sum, so the retry's spend is never invisible.
    """
    summary = mine_summary(st)
    ok = run_completed(st) or oob_url is not None
    merge_ready = summary.get("merge_ready", "").lower() == "yes"
    # Backstop for the "exited before the review returned" failure mode: a run
    # that completes not-merge-ready having finished ZERO review rounds never
    # actually ran the review->fix loop (e.g. it posted @sdk-review then ended
    # its turn before the reply landed). That is a resolver defect, not a genuine
    # hand-to-human — flag it distinctly so it isn't mistaken for normal triage.
    exited_early = ok and not merge_ready and _rounds_completed(summary) == 0
    if oob_url:
        outcome = (
            "✅ completed out-of-band — our stream dropped, but the resolver's "
            "Phase-4 hand-off comment was found on the PR"
        )
    elif resolved_nothing(st):
        outcome = (
            "❌ no-op run — the sandbox reported success but did no work (no "
            "Phase-4 summary, nothing pushed); safe to re-run `@sdk-resolve`"
        )
    elif not ok:
        outcome = "❌ run failed"
    elif merge_ready:
        outcome = "✅ merge-ready (human merges)"
    elif exited_early:
        outcome = (
            "⚠️ exited before completing a review round — the resolver did not run "
            "the review→fix loop; safe to re-run `@sdk-resolve`"
        )
    else:
        outcome = "⚠️ stopped short — needs a human"
    cost = total_cost(attempts) if len(attempts) > 1 else st.cost
    cost_line = f"**Cost:** {cost or 'n/a'} USD"
    if len(attempts) > 1:
        cost_line += f" across {len(attempts)} attempts"
    lines = [
        f"# SDK Resolve — PR #{pr_number}",
        "",
        f"**Outcome:** {outcome}  ",
        f"{cost_line}  ",
    ]
    if gha_run_url:
        lines.append(f"**Run:** [logs + cost]({gha_run_url})  ")
    if oob_url:
        lines.append(
            f"**Out-of-band hand-off:** [resolver summary comment]({oob_url})  "
        )
    elif st.errored:
        lines.append(f"**Error:** `{st.err_code}` {st.err_msg}  ")
    if summary:
        lines += ["", "| Metric | Value |", "|---|---|"]
        for key in SUMMARY_ROWS:
            if key in summary:
                lines.append(f"| {key.replace('_', ' ')} | {summary[key]} |")
    else:
        lines += [
            "",
            "> No summary block was emitted by the run — see the workflow log "
            "for phase output.",
        ]
    lines += render_attempt_trail(attempts)
    return "\n".join(lines) + "\n"


def write_step_summary(content: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as e:  # never let summary-writing sink the run
        print(f"::warning::Could not write step summary: {e}")


def decide_exit(st: SSEState) -> tuple[int, str]:
    """Map the final stream state to (exit_code, message)."""
    if st.errored:
        return 1, f"::error::Sandbox error: code={st.err_code} message={st.err_msg}"
    if not st.got_event:
        return (
            1,
            "::error::Stream ended without a single SSE event — likely VPN/network/proxy issue",
        )
    if not st.completed:
        # No transport `complete` sentinel. If the resolver still emitted its
        # Phase 4 summary block, it finished its work — the missing sentinel is a
        # transport artifact (clean EOF), not a failed run. Without that evidence
        # the stream truncated mid-work, which stays a failure.
        if run_completed(st):
            return (
                0,
                "::warning::Stream ended without a 'complete' event, but the "
                "resolver emitted its Phase 4 summary — treating the run as "
                "completed.",
            )
        if st.response_text:
            # The agent streamed real work but the stream was cut before Phase 4
            # (no summary, no terminal `complete`/`error`). This is a mid-run
            # stream drop — typically a server/proxy connection cap on a
            # long-lived response — not a resolver-logic bug. Name it so.
            return (
                1,
                "::error::Stream ended mid-run without a 'complete' event — the "
                "resolver was working but the stream was cut before its Phase 4 "
                "summary (likely a server/proxy stream-duration cap on the "
                "mothership connection, not a resolver bug). Re-trigger the "
                "resolver to retry.",
            )
        return 1, "::error::Stream ended without a 'complete' event"
    if st.status != "completed":
        return 1, f"::error::Sandbox final status={st.status} (expected 'completed')"
    if resolved_nothing(st):
        # The sentinel says the sandbox exited happily; the missing Phase 4
        # summary says it never did the work. Fail, so the retry ladder below
        # gets a chance on a different model instead of the run going green
        # over a resolve that pushed nothing and posted nothing.
        return (
            1,
            "::error::Sandbox reported status=completed but emitted no Phase 4 "
            "summary — the resolver ended its turn without working through the "
            "ORCHESTRATION (no @sdk-review trigger, no report, nothing pushed). "
            "Treating as a failed run.",
        )
    return 0, f"SDK Resolve completed (cost={st.cost})."


def _parse_iso8601_epoch(value: str) -> float | None:
    """GitHub `created_at` (e.g. 2026-07-17T06:34:00Z) → POSIX seconds, or None."""
    try:
        return (
            datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
            .replace(tzinfo=timezone.utc)
            .timestamp()
        )
    except (ValueError, TypeError):
        return None


def find_oob_summary(comments: Iterable[Any], since_epoch: float) -> str | None:
    """URL of the newest SDK_RESOLVE_SUMMARY comment created at/after since_epoch.

    The marker is written only by the resolver's Phase 4, so marker + a
    timestamp newer than this run's start uniquely identifies *our* hand-off.
    """
    best_url: str | None = None
    best_epoch = since_epoch
    for c in comments:
        if not isinstance(c, dict):
            continue
        if RESOLVE_SUMMARY_MARKER not in (c.get("body") or ""):
            continue
        created = _parse_iso8601_epoch(c.get("created_at", ""))
        if created is None or created < since_epoch:
            continue
        if best_url is None or created >= best_epoch:
            best_url, best_epoch = c.get("html_url") or "", created
    return best_url


def _fetch_pr_comments(
    pr_number: str, token: str, opener: Callable[..., Any] = urllib.request.urlopen
) -> list[Any]:
    """Newest-first page of the PR's issue comments (where SDK_RESOLVE_SUMMARY lives)."""
    url = (
        "https://api.github.com/repos/atlanhq/application-sdk/"
        f"issues/{pr_number}/comments?per_page=100&sort=created&direction=desc"
    )
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with opener(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return data if isinstance(data, list) else []


def sandbox_terminated_abnormally(st: SSEState) -> bool:
    """True when the sandbox itself died, as opposed to our stream being cut.

    The distinction drives two decisions: how long to wait for an out-of-band
    hand-off, and whether re-dispatching is safe. A dead sandbox will post
    nothing more; a live one behind a cut stream is still working, and
    re-dispatching against it would double-run the resolver.

    A no-op run (`resolved_nothing`) belongs on the dead side: the sentinel
    proves the sandbox has stopped, so it will post nothing more and a
    re-dispatch cannot double-run it.
    """
    if st.errored:
        return True
    if not st.completed:
        return False  # no sentinel — our stream was cut, the sandbox may live on
    return st.status != "completed" or resolved_nothing(st)


def is_transport_fault(st: SSEState) -> bool:
    """True when the reported fault is the RPC to the sandbox, not the model.

    Same code discipline as `is_retryable_fault`: an informative code decides on
    its own and never falls through to the patterns, which exist for the
    flattened `none`/`unknown`/empty case. `http_` codes are excluded outright —
    a status on the POST itself means no sandbox was ever started, so there is no
    RPC to a sandbox that could have dropped, and that codespace already decides
    for itself.
    """
    if st.err_code.startswith("http_") or st.err_code not in UNINFORMATIVE_ERR_CODES:
        return False
    return any(p in st.err_msg.lower() for p in RETRYABLE_TRANSPORT_PATTERNS)


def oob_poll_budget(st: SSEState) -> int:
    """Seconds to look for an out-of-band summary, given how the stream ended."""
    if not st.got_event:
        return 0  # never saw an event → sandbox likely never started; nothing to await
    if is_transport_fault(st):
        # FND-647. mothership reported a dropped RPC, which `errored` then makes
        # look like a dead sandbox to the check below — so this ranks above it.
        # The pipe dropping is no evidence the resolver stopped, and it can be
        # many minutes from its Phase 4 hand-off, so give it the full window.
        # Waiting is the ONLY recovery available here: a re-dispatch is off the
        # table (see `retry_decision`), so the 120s this used to get is exactly
        # how a healthy run gets thrown away (run 32309963717, $0.69).
        return OOB_POLL_SECONDS_STREAM_DROP
    if sandbox_terminated_abnormally(st):
        # Sandbox terminated abnormally; only catch a summary posted just before.
        return OOB_POLL_SECONDS_HARD_ERROR
    # Our own stream was cut with no terminal event; the sandbox is likely
    # still working.
    return OOB_POLL_SECONDS_STREAM_DROP


def _squash(text: str) -> str:
    r"""Collapse whitespace runs so a multi-line fault fits one log line."""
    return re.sub(r"\s+", " ", text).strip()


def billed_nothing(st: SSEState) -> bool:
    """True when the run reported no spend, i.e. no model ever produced a token.

    This is the evidence that licenses a re-dispatch on THIS lane, so it is
    checked rather than assumed. All 9 observed FND-764 occurrences reported an
    empty `cost_usd` while other failures on this lane reported real spend
    ($0.48 / $2.26 / $6.58 / $28.34).

    Deliberately fails OPEN on a cost it cannot read. `total_cost` above
    documents mothership's cost telemetry as unreliable, and that unreliability
    is false-EMPTY — runs that streamed hundreds of responses have reported no
    cost. So an unreadable value can only ever return this to the behaviour
    that shipped without the guard, never to something stricter than the
    evidence supports.
    """
    try:
        return float(st.cost) == 0.0
    except (TypeError, ValueError):
        return True  # absent or unparseable — no positive evidence of spend


def is_sandbox_api_fault(st: SSEState) -> bool:
    """True when mothership's sandbox-api reported a fault in its own plumbing.

    Self-deciding on the code, like every other classifier here: `internal` is
    informative, so this never consults RETRYABLE_ERR_PATTERNS. The message
    prefix is part of the discriminator rather than a fallback — see
    SANDBOX_API_ERR_CODE for why the code alone is not enough.

    The empty-cost conjunct is what makes this safe ON THIS LANE, and it is why
    this predicate is NOT byte-identical to the review lane's copy — see
    SANDBOX_API_ERR_CODE.
    """
    if st.err_code != SANDBOX_API_ERR_CODE:
        return False
    if not billed_nothing(st):
        return False
    return SANDBOX_API_ERR_MARKER in st.err_msg


def is_retryable_fault(st: SSEState) -> bool:
    """True when the cause looks like a model/provider fault, not a fixed one.

    The code decides whenever it carries information: a recognised retryable one
    retries, and anything else — 401, 403, a prompt fault — does not. The message
    patterns are consulted only for a code of `none`/`unknown`/empty, which is
    the flattened-code case they exist for. Letting them speak for a known code
    would classify "401 upstream auth failed" as retryable and buy a second
    sandbox that fails identically.
    """
    if st.err_code.startswith("http_"):
        # Self-deciding codespace: a dispatch-level HTTP status never falls
        # through to the message-pattern ladder below.
        return st.err_code in RETRYABLE_DISPATCH_HTTP_CODES
    if st.err_code in NEVER_RETRY_ERR_CODES:
        return False
    if resolved_nothing(st):
        # A no-op run carries no error code at all, so the code ladder below
        # cannot see it. The fault is the model ending its turn early, which is
        # precisely the kind a different model can plausibly survive.
        return True
    if st.err_code in RETRYABLE_ERR_CODES:
        return True
    if st.err_code not in UNINFORMATIVE_ERR_CODES:
        return False
    return any(p in st.err_msg.lower() for p in RETRYABLE_ERR_PATTERNS)


def _retry_class(st: SSEState, attempt: int, current_model: str) -> RetryPlan:
    """Which retry class this ending falls into, if any, and on which model.

    `current_model` is the model THIS attempt actually ran, threaded down from
    `main()`. The same-model classes must not re-derive it from `attempt`:
    `attempt_model` answers "what attempt N would have run by default", which
    stops being the same answer the moment a same-model retry has happened.

    Called only once the out-of-band poll has come back empty, so "posted
    nothing" is established for every branch, and "the sandbox is dead" for the
    abnormal-termination branch — with one exception, which is why the transport
    branch below exists: a dropped RPC reports a terminal fault without proving
    the resolver stopped, and on this lane an unproven death is never retried.
    """
    if not sandbox_terminated_abnormally(st):
        return RetryPlan(
            False,
            "the stream ended without a hard error — the sandbox may still be "
            "running, and a re-dispatch would double-run the resolver",
        )
    if is_transport_fault(st):
        # FND-647, and it must be ranked ABOVE `is_retryable_fault` rather than
        # left to fall through it. An error event leaves `run_completed` False,
        # so a clean `complete` plus a dropped RPC is indistinguishable from a
        # no-op run — and FND-644's `resolved_nothing` branch answers True on
        # that basis, buying a second resolver against a first of UNKNOWN
        # liveness. That is the double-push `sdk_resolve_push_guard.py` exists to
        # prevent, and no dedupe can undo it after the fact. (Before FND-644 the
        # same fault fell out the other side, refused with "a different model
        # would fail the same way" — true, and beside the point: the model never
        # failed. Run 32309963717 was lost that way.)
        #
        # This lane therefore has NO transport retry class at all, by decision
        # rather than by default: what the fault buys instead is the long
        # out-of-band poll above, which recovers the run without ever risking a
        # second resolver. If that poll came back empty, the run is spent.
        return RetryPlan(
            False,
            f"code={st.err_code or 'none'} reports a dropped RPC to the sandbox, "
            "not a dead one — the resolver may still be pushing, and two "
            "resolvers on one branch cannot be undone, so this lane waits for "
            "the out-of-band hand-off instead of re-dispatching",
        )
    if is_sandbox_api_fault(st):
        # FND-764. Ranked below the transport branch and above the model one,
        # because it is neither: mothership reported a fault in its OWN wrapper
        # around the run, so nothing about the model failed and the swap axis is
        # the wrong one — attempt 2 re-runs the same model on a fresh sandbox.
        #
        # Unlike the transport case just above, this one is safe on this lane.
        # The distinction is where the fault is: a dropped RPC leaves a sandbox
        # of unknown liveness behind the broken pipe, while a sandbox-api fault
        # is mothership saying its own execution wrapper never delivered a run
        # at all — empty `cost_usd`, no first token, and (checked across all 9
        # occurrences) not one resolver push or Phase-4 hand-off. There is no
        # first resolver for a second to race.
        same = current_model
        return RetryPlan(
            True,
            f"code={st.err_code} is a fault in mothership's own sandbox-api "
            f"wrapper ({_squash(st.err_msg)[:SANDBOX_API_ERR_PREVIEW_CHARS]}), "
            f"not in the model or the "
            f"resolver — re-dispatching on the same model ({same}) "
            f"(attempt {attempt + 1} of {MAX_DISPATCH_ATTEMPTS})",
            same,
        )
    if not is_retryable_fault(st):
        return RetryPlan(
            False,
            f"code={st.err_code or 'none'} is not a known model/provider fault, "
            "so a different model would fail the same way",
        )
    cause = (
        "the sandbox reported success but emitted no Phase 4 summary (no-op run)"
        if resolved_nothing(st)
        else f"code={st.err_code or 'none'} looks like a model/provider fault"
    )
    return RetryPlan(
        True,
        f"{cause} — re-dispatching on {RETRY_MAIN_MODEL} (attempt {attempt + 1} "
        f"of {MAX_DISPATCH_ATTEMPTS})",
        RETRY_MAIN_MODEL,
    )


def retry_decision(
    st: SSEState, attempt: int, seconds_left: float, current_model: str
) -> RetryPlan:
    """Whether to re-dispatch after this attempt, on what, and why either way.

    The reason is logged verbatim so a run that did NOT retry says why — the
    part that is otherwise invisible. The attempt cap and the wall-clock floor
    bound EVERY class; only the classification in `_retry_class` differs.

    `current_model` is required rather than defaulted: a caller that omitted it
    would silently reintroduce the attempt-number derivation this signature
    exists to remove. See `_retry_class`.
    """
    if attempt >= MAX_DISPATCH_ATTEMPTS:
        return RetryPlan(False, f"already used all {MAX_DISPATCH_ATTEMPTS} attempts")
    plan = _retry_class(st, attempt, current_model)
    if not plan.retry:
        return plan
    if seconds_left < RETRY_MIN_REMAINING_SECONDS:
        return RetryPlan(
            False,
            f"only {int(seconds_left)}s of the job budget remain, below the "
            f"{RETRY_MIN_REMAINING_SECONDS}s a retry needs to finish a round",
        )
    return plan


def poll_for_oob_summary(
    pr_number: str,
    token: str,
    since_epoch: float,
    budget_seconds: int,
    *,
    fetch: Callable[[str, str], list[Any]] = _fetch_pr_comments,
    sleeper: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.time,
) -> str | None:
    """Poll the PR for the Phase-4 hand-off comment until found or budget elapses."""
    if budget_seconds <= 0 or not token:
        return None
    deadline = now() + budget_seconds
    while True:
        try:
            comments = fetch(pr_number, token)
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as e:
            print(f"::warning::PR-comment poll failed (will retry): {e}")
            comments = []
        url = find_oob_summary(comments, since_epoch)
        if url:
            return url
        if now() >= deadline:
            return None
        sleeper(OOB_POLL_INTERVAL_SECONDS)


def check_health(
    base_url: str,
    opener: Callable[..., Any] = urllib.request.urlopen,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    for attempt in range(1, HEALTH_RETRIES + 1):
        try:
            with opener(f"{base_url}/health", timeout=10) as resp:
                status = getattr(resp, "status", None)
                if status is None:
                    status = resp.getcode()
                if status == 200:
                    print(f"Mothership reachable (attempt {attempt})")
                    return True
        except (urllib.error.URLError, OSError) as e:
            print(f"Mothership unreachable ({e}), retry {attempt}/{HEALTH_RETRIES}")
        else:
            print(f"Mothership non-200, retry {attempt}/{HEALTH_RETRIES}")
        if attempt < HEALTH_RETRIES:
            sleeper(HEALTH_BACKOFF_SECONDS)
    return False


def _max_rounds() -> int:
    try:
        return int(os.environ.get("MAX_ROUNDS", ""))
    except (ValueError, TypeError):
        return DEFAULT_MAX_ROUNDS


def dispatch_once(
    base_url: str,
    token: str,
    payload: dict[str, Any],
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> SSEState:
    """POST one execution and drain its SSE stream into an SSEState."""
    req = urllib.request.Request(
        f"{base_url}/api/sandbox/execute",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    try:
        # timeout is the per-read socket idle watchdog (not a whole-request cap):
        # a silent stall frees the runner in ~READ_IDLE_TIMEOUT_SECONDS instead
        # of blocking the full 2h. TimeoutError/OSError surface here too.
        with opener(req, timeout=READ_IDLE_TIMEOUT_SECONDS) as resp:
            return process_stream(raw.decode("utf-8", "replace") for raw in resp)
    except urllib.error.HTTPError as e:
        # Ordering matters: HTTPError is itself a URLError subclass, so this
        # clause MUST come before the URLError/TimeoutError/OSError one below
        # or every dispatch-level HTTP status would be swallowed into
        # `stream_error` and treated as "our connection died mid-stream" - the
        # exact class this codebase refuses to retry (run 32347771368 got a
        # 504 on the POST and logged "code=stream_error is not a known
        # model/provider fault": no retry, no out-of-band poll, zero work done).
        #
        # An HTTP status on the POST means the far side actually answered and
        # no sandbox was ever started, unlike a mid-stream drop - so there is
        # nothing to double-run, which is why this is retryable-eligible where
        # `stream_error` is not.
        try:
            # Narrow on purpose: a body read can fail on a half-open socket
            # (OSError), a premature close (http.client.HTTPException), or a
            # response whose `fp` was already consumed/absent (AttributeError,
            # ValueError). None of those may be allowed to mask the HTTP status
            # we came here to record.
            body = e.read().decode("utf-8", "replace")
        except (OSError, http.client.HTTPException, AttributeError, ValueError):
            body = str(e.reason)
        body = body[:500]
        print(f"::error::Sandbox dispatch rejected with HTTP {e.code}: {body}")
        st = SSEState()
        st.errored = True
        st.err_code = f"http_{e.code}"
        st.err_msg = f"HTTP {e.code} on dispatch POST: {body}"
        return st
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"::error::Sandbox dispatch stream error/stall: {e}")
        st = SSEState()
        st.errored = True
        # NOT retryable: this is OUR connection dying, not the sandbox. See
        # NEVER_RETRY_ERR_CODES — the resolver is probably still working.
        st.err_code = "stream_error"
        st.err_msg = str(e)
        return st


def main() -> int:
    missing = [
        v
        for v in ("MOTHERSHIP_URL", "HARNESS_TOKEN", "PR_NUMBER")
        if not os.environ.get(v)
    ]
    if missing:
        print(
            f"::error::Missing required environment variable(s): {', '.join(missing)}"
        )
        return 1
    base_url = os.environ["MOTHERSHIP_URL"].rstrip("/")
    token = os.environ["HARNESS_TOKEN"]
    pr_number = os.environ["PR_NUMBER"]
    gha_run_url = os.environ.get("GHA_RUN_URL", "")
    run_date = os.environ.get("RUN_DATE") or time.strftime("%Y-%m-%d", time.gmtime())
    max_rounds = _max_rounds()
    # No hardcoded handles: the list comes from the repo variable
    # vars.SDK_RESOLVE_REVIEWERS via the workflow. Unset is tolerated but never
    # silent — without it the run ends with no reviewer requested, which is the
    # kind of thing that only gets noticed weeks later.
    reviewers = os.environ.get("REVIEWERS", "")
    if not _reviewer_handles(reviewers, ""):
        print(
            "::warning::SDK_RESOLVE_REVIEWERS is unset — no reviewer will be "
            "requested when this PR reaches merge-ready. Set the repo variable "
            "to a comma-separated list of GitHub handles."
        )
    requester = os.environ.get("REQUESTER", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    # Lower bound for matching *this* run's hand-off comment; captured before the
    # sandbox starts so a prior run's (older) summary can't be mistaken for ours.
    run_start_epoch = time.time()
    print(f"Dispatching SDK Resolve: pr={pr_number} max_rounds={max_rounds}")

    if not check_health(base_url):
        print("::error::Cannot reach mothership after retries")
        return 1

    attempts: list[Attempt] = []
    oob_url: str | None = None
    attempt = 1
    # Named by the previous attempt's RetryPlan; empty on the first pass. The
    # model is NOT a function of the attempt number — see RetryPlan.
    next_model = ""
    while True:
        model = next_model or attempt_model(attempt)
        # Clamp the sandbox's own cap to what is left of this step's budget.
        # Cancelling the job does not stop the sandbox, so an unclamped retry
        # started late would keep billing for up to 2h after the runner dies.
        seconds_left = DISPATCH_BUDGET_SECONDS - (time.time() - run_start_epoch)
        payload = build_payload(
            pr_number,
            gha_run_url,
            max_rounds,
            run_date,
            reviewers,
            requester,
            attempt=attempt,
            model=model,
            max_timeout_seconds=max(1, min(STREAM_TIMEOUT_SECONDS, int(seconds_left))),
        )
        print(f"[attempt {attempt}/{MAX_DISPATCH_ATTEMPTS}] dispatching on {model}")
        st = dispatch_once(base_url, token, payload)
        attempts.append(Attempt(attempt, model, st))
        code, message = decide_exit(st)
        # Per-attempt cost, logged separately so a retry ladder cannot hide its
        # spend behind a single total.
        print(
            f"[attempt {attempt}/{MAX_DISPATCH_ATTEMPTS}] model={model} "
            f"status={st.status or 'none'} cost_usd={st.cost or 'n/a'} "
            f"code={st.err_code or 'none'}"
        )
        if code == 0:
            break

        # Transport backstop: the resolver may have finished out-of-band after our
        # stream was cut. Look for its Phase-4 hand-off comment before failing.
        # Clamped to what is left of the step's budget: a 45-min stream-drop poll
        # started late (most likely on a retry, which begins with attempt 1's time
        # already spent) would otherwise let the runner's 130-min timeout kill the
        # job before the step summary is ever written.
        seconds_left = DISPATCH_BUDGET_SECONDS - (time.time() - run_start_epoch)
        budget = min(oob_poll_budget(st), max(0, int(seconds_left)))
        if github_token and budget > 0:
            print(
                f"::warning::Stream ended unhappily; polling PR #{pr_number} for up "
                f"to {budget}s for the resolver's out-of-band hand-off before "
                "reporting failure."
            )
            oob_url = poll_for_oob_summary(
                pr_number,
                github_token,
                run_start_epoch - OOB_SINCE_SKEW_SECONDS,
                budget,
            )
        if oob_url:
            code = 0
            message = (
                "::warning::Our SSE stream ended unhappily, but the resolver posted "
                f"its Phase-4 hand-off out-of-band ({oob_url}) — the run completed. "
                "Treating as success."
            )
            break

        # Only here is the sandbox provably dead with nothing further to land:
        # the hard-error poll came back empty. Retrying earlier would double-run
        # a resolver that is still working, or one that finished just before it
        # died. Phase 0 re-reads live PR state, so a second sandbox resumes
        # rather than restarts.
        plan = retry_decision(
            st,
            attempt,
            DISPATCH_BUDGET_SECONDS - (time.time() - run_start_epoch),
            model,
        )
        if not plan.retry:
            print(f"::warning::Not re-dispatching: {plan.reason}")
            break
        print(f"::warning::Re-dispatching on a fresh sandbox: {plan.reason}")
        next_model = plan.model
        attempt += 1

    if code != 0 and len(attempts) > 1:
        message += (
            f" (retried on {attempts[-1].model} after {attempts[0].model} "
            "failed; both attempts failed)"
        )

    write_step_summary(
        render_step_summary(st, pr_number, gha_run_url, oob_url, attempts)
    )
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
