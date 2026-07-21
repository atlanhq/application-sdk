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

Environment:
    MOTHERSHIP_URL      base URL (e.g. https://mothership.atlan.dev)
    HARNESS_TOKEN       bearer for /api/sandbox/execute
    PR_NUMBER           the open PR to drive to merge-ready
    MAX_ROUNDS          max @sdk-review rounds before stopping (default 8)
    REVIEWERS           comma-separated GitHub handles to request + tag at the end
    REQUESTER           login that invoked @sdk-resolve (also tagged)
    GHA_RUN_URL         this workflow run's URL
    RUN_DATE            ISO date (computed if absent)
    GITHUB_STEP_SUMMARY path GitHub Actions gives us to render the run summary
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

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
# landed just before it died.
OOB_POLL_SECONDS_HARD_ERROR = 120
# Clock-skew margin subtracted from this run's start when matching a summary's
# created_at, so a hand-off posted right at the boundary isn't missed. Small
# enough that a prior run's (minutes-older) summary is never mistaken for ours.
OOB_SINCE_SKEW_SECONDS = 120

SUMMARY_START = "=== SDK RESOLVE SUMMARY ==="
SUMMARY_END = "=== END SUMMARY ==="
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
finish (merge-ready OR NEEDS_HUMAN), REQUEST HUMAN REVIEW: run
`gh pr edit {pr_number} --add-reviewer {reviewers}` (ignore "can't request from
the author" errors) and post the final report @-mentioning {tag_list} so they
know it's their turn. Expect each push to reset the reviewer labels/status
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


def build_payload(
    pr_number: str,
    gha_run_url: str,
    max_rounds: int,
    run_date: str,
    reviewers: str,
    requester: str,
) -> dict[str, Any]:
    return {
        "mode": "direct",
        "stream": True,
        "source": "github-comment",
        "source_id": f"sdk-resolve-{pr_number}-{run_date}",
        "repositories": ["atlanhq/application-sdk"],
        "base_branch": "main",
        "snapshot": "_base",
        "prompt": build_prompt(
            pr_number, gha_run_url, max_rounds, reviewers, requester
        ),
        "max_timeout_seconds": STREAM_TIMEOUT_SECONDS,
        "idle_timeout_seconds": 1800,
        "metadata": {
            "pr_number": pr_number,
            "max_rounds": max_rounds,
            "run_date": run_date,
            "reviewers": reviewers,
            "requester": requester,
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
            st.err_code = _jget(data, "error", "code", default="none")
            st.err_msg = _jget(data, "error", "message")
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

    The transport `complete` event is the normal success signal, but mothership
    sometimes ends the resolve stream cleanly (EOF) without it. The resolver
    emits its Phase 4 `=== SDK RESOLVE SUMMARY ===` block as its very last action
    (ORCHESTRATION Phase 4), so a mined summary carrying a terminal key is
    end-of-run evidence independent of the sentinel — treat that as completed. A
    stream truncated mid-work carries no summary block and is still a failure, so
    this never masks a genuinely incomplete run.
    """
    if st.errored:
        return False
    if st.completed:
        return st.status == "completed"
    summary = mine_summary(st)
    return any(k in summary for k in ("final_verdict", "merge_ready", "stopped_reason"))


def _rounds_completed(summary: dict[str, str]) -> int | None:
    """Parsed `rounds` count from the summary block, or None if absent/malformed."""
    try:
        return int(summary["rounds"].strip())
    except (KeyError, ValueError, AttributeError):
        return None


def render_step_summary(
    st: SSEState, pr_number: str, gha_run_url: str, oob_url: str | None = None
) -> str:
    """Build the Markdown written to GITHUB_STEP_SUMMARY — always renders.

    `oob_url`, when set, is the resolver's out-of-band Phase-4 hand-off comment
    found by polling after our stream was cut: the run completed even though the
    stream reported failure, so render it as a recovered success.
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
    lines = [
        f"# SDK Resolve — PR #{pr_number}",
        "",
        f"**Outcome:** {outcome}  ",
        f"**Cost:** {st.cost or 'n/a'} USD  ",
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


def oob_poll_budget(st: SSEState) -> int:
    """Seconds to look for an out-of-band summary, given how the stream ended."""
    if not st.got_event:
        return 0  # never saw an event → sandbox likely never started; nothing to await
    if st.errored or (st.completed and st.status != "completed"):
        # Sandbox terminated abnormally; only catch a summary posted just before.
        return OOB_POLL_SECONDS_HARD_ERROR
    return OOB_POLL_SECONDS_STREAM_DROP  # transport drop; sandbox likely still working


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
    reviewers = os.environ.get("REVIEWERS", "cmgrote,vaibhavatlan")
    requester = os.environ.get("REQUESTER", "")
    github_token = os.environ.get("GITHUB_TOKEN", "")
    # Lower bound for matching *this* run's hand-off comment; captured before the
    # sandbox starts so a prior run's (older) summary can't be mistaken for ours.
    run_start_epoch = time.time()
    print(f"Dispatching SDK Resolve: pr={pr_number} max_rounds={max_rounds}")

    if not check_health(base_url):
        print("::error::Cannot reach mothership after retries")
        return 1

    payload = build_payload(
        pr_number, gha_run_url, max_rounds, run_date, reviewers, requester
    )
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
        with urllib.request.urlopen(req, timeout=READ_IDLE_TIMEOUT_SECONDS) as resp:
            st = process_stream(raw.decode("utf-8", "replace") for raw in resp)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"::error::Sandbox dispatch stream error/stall: {e}")
        st = SSEState()
        st.errored = True
        st.err_code = "stream_error"
        st.err_msg = str(e)

    code, message = decide_exit(st)
    oob_url: str | None = None
    if code != 0:
        # Transport backstop: the resolver may have finished out-of-band after our
        # stream was cut. Look for its Phase-4 hand-off comment before failing.
        budget = oob_poll_budget(st)
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

    write_step_summary(render_step_summary(st, pr_number, gha_run_url, oob_url))
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
