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


def build_prompt(pr_number: str, gha_run_url: str, max_rounds: int) -> str:
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

GITHUB_TOKEN is pre-injected by the sandbox. Use `gh` for all GitHub operations.

Drive PR #{pr_number} to MERGE-READY: green required CI + zero @sdk-review
findings (nits included, unless proven false with a recorded rationale) +
verdict READY_TO_MERGE. You are the only writer — the reviewer runs in its own
separate sandbox; you trigger it with `@sdk-review` and consume its comment.
Do NOT `gh pr merge` — stop at merge-ready and hand back to a human. Expect each
push to reset the reviewer labels/status (reset-on-push) — that is normal; key
the loop off findings + CI, not labels. Stop after MAX_ROUNDS rounds, or if a
dismissed finding is re-raised, and report. At the very end print the
`=== SDK RESOLVE SUMMARY ===` block from ORCHESTRATION Phase 4 verbatim."""


def build_payload(
    pr_number: str, gha_run_url: str, max_rounds: int, run_date: str
) -> dict[str, Any]:
    return {
        "mode": "direct",
        "stream": True,
        "source": "github-comment",
        "source_id": f"sdk-resolve-{pr_number}-{run_date}",
        "repositories": ["atlanhq/application-sdk"],
        "base_branch": "main",
        "snapshot": "_base",
        "prompt": build_prompt(pr_number, gha_run_url, max_rounds),
        "max_timeout_seconds": STREAM_TIMEOUT_SECONDS,
        "idle_timeout_seconds": 1800,
        "metadata": {
            "pr_number": pr_number,
            "max_rounds": max_rounds,
            "run_date": run_date,
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


def render_step_summary(st: SSEState, pr_number: str, gha_run_url: str) -> str:
    """Build the Markdown written to GITHUB_STEP_SUMMARY — always renders."""
    summary = mine_summary(st)
    ok = st.completed and st.status == "completed"
    merge_ready = summary.get("merge_ready", "").lower() == "yes"
    if not ok:
        outcome = "❌ run failed"
    elif merge_ready:
        outcome = "✅ merge-ready (human merges)"
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
    if st.errored:
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
        return 1, "::error::Stream ended without a 'complete' event"
    if st.status != "completed":
        return 1, f"::error::Sandbox final status={st.status} (expected 'completed')"
    return 0, f"SDK Resolve completed (cost={st.cost})."


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
    print(f"Dispatching SDK Resolve: pr={pr_number} max_rounds={max_rounds}")

    if not check_health(base_url):
        print("::error::Cannot reach mothership after retries")
        return 1

    payload = build_payload(pr_number, gha_run_url, max_rounds, run_date)
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

    write_step_summary(render_step_summary(st, pr_number, gha_run_url))
    code, message = decide_exit(st)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
