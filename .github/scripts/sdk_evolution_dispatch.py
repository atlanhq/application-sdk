#!/usr/bin/env python3
"""Dispatch an SDK Evolution run to mothership's Rover Direct API and stream it.

One dispatcher for both tiers of the pipeline (see
`.mothership/sdk-evolution/ORCHESTRATION.md`):

  * daily   — fast, high-confidence pass over the whole SDK (Mon–Sat)
  * weekly  — the Sunday superset: design/ADR PRs, /audit-consumers, toolkit

The inline SSE-parsing shell that used to live in sdk-evolution-cron.yml is
gone; this tested script owns it per docs/standards/ci.md. Beyond streaming, it
writes a **GITHUB_STEP_SUMMARY** (found / killed / PRs / cost + Linear parent
link) — the missing observability that got the cron disabled. It parses the
`=== SDK EVOLUTION SUMMARY ===` block the agent emits (ORCHESTRATION Stage 7).

Flow: resolve tier → health-check mothership (retries) → build prompt + payload
→ POST /api/sandbox/execute and stream SSE → render the step summary → exit
non-zero on a sandbox error / incomplete stream / non-'completed' final status.

Environment:
    MOTHERSHIP_URL      base URL (e.g. https://mothership.atlan.dev)
    HARNESS_TOKEN       bearer for /api/sandbox/execute
    TIER                daily | weekly | auto (auto → weekly on Sunday UTC)
    CONSUMER_PR_CAP     weekly only: max consumer migration PRs per run
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
from typing import Any

HEALTH_RETRIES = 5
HEALTH_BACKOFF_SECONDS = 5
STREAM_TIMEOUT_SECONDS = 7200
DEFAULT_CONSUMER_PR_CAP = 5

SUMMARY_START = "=== SDK EVOLUTION SUMMARY ==="
SUMMARY_END = "=== END SUMMARY ==="
# The count/label rows we render into the GitHub step summary, in order.
SUMMARY_METRICS = (
    "discovered",
    "killed_prefilter",
    "killed_verify",
    "killed_feasibility",
    "fix_prs",
    "design_prs",
    "consumer_prs",
    "handed_for_review",
)


def resolve_tier(tier: str, run_date: str) -> str:
    """Resolve 'auto' to a concrete tier: weekly on Sunday (UTC), else daily.

    `run_date` is an ISO `YYYY-MM-DD` string; a malformed date falls back to
    daily (the cheap, safe default).
    """
    tier = (tier or "auto").strip().lower()
    if tier in ("daily", "weekly"):
        return tier
    try:
        # weekday(): Mon=0 … Sun=6
        is_sunday = time.strptime(run_date, "%Y-%m-%d").tm_wday == 6
    except (ValueError, TypeError):
        return "daily"
    return "weekly" if is_sunday else "daily"


def build_prompt(
    tier: str, run_date: str, gha_run_url: str, consumer_pr_cap: int
) -> str:
    weekly_note = (
        f"\nThis is a WEEKLY run — also do the deep + cross-repo stages "
        f"(ARCH/TEMPORAL/CONSUMERS/TOOLKIT/DX). CONSUMER_PR_CAP={consumer_pr_cap} "
        f"caps consumer migration PRs; rotate the remainder to next week.\n"
        if tier == "weekly"
        else "\nThis is a DAILY run — fast, high-confidence pass only. Do NOT open "
        "design debates; note weekly DESIGN candidates and move on.\n"
    )
    # CONSUMER_PR_CAP only applies to the weekly consumer audit — daily ignores
    # it, so keep it out of the daily prompt header.
    cap_line = f"\n  CONSUMER_PR_CAP:  {consumer_pr_cap}" if tier == "weekly" else ""
    return f"""You are running the SDK Evolution pipeline in a Cloudflare sandbox.

Repository cloned at: /workspace/application-sdk (on main).
Working directory: cd /workspace/application-sdk

Read and follow the orchestration EXACTLY:
  .mothership/sdk-evolution/ORCHESTRATION.md
  .mothership/sdk-evolution/CLAUDE.md
  .mothership/sdk-evolution/tools.md
  .mothership/sdk-evolution/references/check-registry.md

Run metadata (use these verbatim):
  TIER:             {tier}
  RUN_DATE:         {run_date}
  GHA_RUN_URL:      {gha_run_url}{cap_line}
{weekly_note}
GITHUB_TOKEN is pre-injected by the sandbox. Use `gh` for all GitHub operations.
Linear proxy access: use the $PROXY_BASE and $PROXY_JWT env vars (see tools.md).

Cover all three surfaces (application_sdk/, packages/conformance/,
contract-toolkit/). Honour the check-registry DO-NOT-re-report list — never
re-raise anything ruff / conformance CI / codeql / trivy already gates.

Create a Linear parent ticket and put a link to GHA_RUN_URL at the top of its
description and in the Stage 7 summary. Hand every surviving PR to a SINGLE
@sdk-review pass (NOT auto-complete). At the very end, print the
`=== SDK EVOLUTION SUMMARY ===` block from ORCHESTRATION Stage 7 verbatim so the
workflow can surface it."""


def build_payload(
    tier: str, run_date: str, gha_run_url: str, consumer_pr_cap: int
) -> dict[str, Any]:
    return {
        "mode": "direct",
        "stream": True,
        "source": "github-cron",
        "source_id": f"sdk-evolution-{tier}-{run_date}",
        "repositories": ["atlanhq/application-sdk"],
        "base_branch": "main",
        "snapshot": "_base",
        "prompt": build_prompt(tier, run_date, gha_run_url, consumer_pr_cap),
        "max_timeout_seconds": STREAM_TIMEOUT_SECONDS,
        "idle_timeout_seconds": 1800,
        "metadata": {
            "tier": tier,
            "run_date": run_date,
            "consumer_pr_cap": consumer_pr_cap,
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
        # Two buffers mined for the Stage 7 summary block, because we can't
        # assume how mothership surfaces it (verified on first manual dispatch):
        #  - response_text: text extracted from `response` events, concatenated
        #    with NO separator so delta-streamed fragments reassemble intact.
        #  - raw_data: every `data:` payload verbatim, one per line, so a block
        #    that arrives as raw stdout lines (not wrapped in a response) is
        #    still recoverable. parse_summary is run over both.
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
    """Best-effort extraction of human-readable text from a `response` event.

    The event payload may be a JSON object (with a text-bearing field) or a raw
    string; we want the words either way so the Stage 7 summary block survives.
    """
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
    # Event-agnostic capture: keep every payload verbatim so the Stage 7 block
    # is recoverable no matter which event carries it.
    st.raw_data += data + "\n"
    if st.event == "started":
        return f"[started]   session={_jget(data, 'session_id')} sandbox={_jget(data, 'sandbox_id')}"
    if st.event == "action":
        name = _jget(data, "action_name")
        return f"[action]    {name}" if name else None
    if st.event == "thought":
        return None
    if st.event == "response":
        # No separator — response events may stream as deltas; a separator
        # would split a summary line like "discovered: 12" across fragments.
        st.response_text += _response_text(data)
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
    """Extract the `key: value` pairs from the Stage 7 summary block, if present.

    Tolerates JSON-escaped newlines (``\\n``) in captured response text.
    Returns an empty dict when no well-formed block is found.
    """
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
    """Find the Stage 7 block in either buffer (response text, then raw)."""
    return parse_summary(st.response_text) or parse_summary(st.raw_data)


def render_step_summary(
    st: SSEState, tier: str, run_date: str, gha_run_url: str
) -> str:
    """Build the Markdown written to GITHUB_STEP_SUMMARY — always renders."""
    summary = mine_summary(st)
    outcome = (
        "✅ completed" if (st.completed and st.status == "completed") else "❌ failed"
    )
    lines = [
        f"# SDK Evolution — {tier} — {run_date}",
        "",
        f"**Outcome:** {outcome}  ",
        f"**Cost:** {st.cost or 'n/a'} USD  ",
    ]
    parent = summary.get("parent_ticket")
    if parent:
        lines.append(f"**Linear parent:** {parent}  ")
    if gha_run_url:
        lines.append(f"**Run:** [logs + cost]({gha_run_url})  ")
    if st.errored:
        lines.append(f"**Error:** `{st.err_code}` {st.err_msg}  ")

    if summary:
        lines += ["", "| Metric | Count |", "|---|---|"]
        for key in SUMMARY_METRICS:
            if key in summary:
                lines.append(f"| {key.replace('_', ' ')} | {summary[key]} |")
    else:
        lines += [
            "",
            "> No summary block was emitted by the run — see the workflow log "
            "for stage output.",
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
        return 1, "::error::Stream ended without a 'complete' event"
    if st.status != "completed":
        return 1, f"::error::Sandbox final status={st.status} (expected 'completed')"
    return 0, f"SDK Evolution completed successfully (cost={st.cost})."


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


def _consumer_pr_cap() -> int:
    raw = os.environ.get("CONSUMER_PR_CAP", "")
    try:
        return int(raw)
    except (ValueError, TypeError):
        return DEFAULT_CONSUMER_PR_CAP


def main() -> int:
    missing = [v for v in ("MOTHERSHIP_URL", "HARNESS_TOKEN") if not os.environ.get(v)]
    if missing:
        print(
            f"::error::Missing required environment variable(s): {', '.join(missing)}"
        )
        return 1
    base_url = os.environ["MOTHERSHIP_URL"].rstrip("/")
    token = os.environ["HARNESS_TOKEN"]
    gha_run_url = os.environ.get("GHA_RUN_URL", "")
    run_date = os.environ.get("RUN_DATE") or time.strftime("%Y-%m-%d", time.gmtime())
    tier = resolve_tier(os.environ.get("TIER", "auto"), run_date)
    consumer_pr_cap = _consumer_pr_cap()
    print(f"Dispatching SDK Evolution: tier={tier} run_date={run_date}")

    if not check_health(base_url):
        print("::error::Cannot reach mothership after retries")
        return 1

    payload = build_payload(tier, run_date, gha_run_url, consumer_pr_cap)
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
        with urllib.request.urlopen(req, timeout=STREAM_TIMEOUT_SECONDS) as resp:
            st = process_stream(raw.decode("utf-8", "replace") for raw in resp)
    except urllib.error.URLError as e:
        print(f"::error::Sandbox dispatch HTTP error: {e}")
        st = SSEState()
        st.errored = True
        st.err_code = "http_error"
        st.err_msg = str(e)

    write_step_summary(render_step_summary(st, tier, run_date, gha_run_url))
    code, message = decide_exit(st)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
