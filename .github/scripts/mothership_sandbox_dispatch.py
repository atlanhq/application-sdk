#!/usr/bin/env python3
"""Dispatch a vuln-triage run to mothership's Rover Direct API and stream it.

Replaces the inline SSE-parsing shell (a `while`/`case` state machine) in
vuln-triage-cron.yml — that conditional logic now lives here (tested) per
docs/standards/ci.md. The HTTP itself is irreducible I/O; the event handling
and exit decision are pure and unit-tested.

Flow: check mothership reachability (retries) → build the run prompt + payload
→ POST /api/sandbox/execute and stream Server-Sent Events → exit non-zero on a
sandbox error / incomplete stream / non-'completed' final status.

Environment:
    MOTHERSHIP_URL   base URL (e.g. https://mothership.atlan.dev)
    HARNESS_TOKEN    bearer for /api/sandbox/execute
    TICKET           Linear ticket to triage
    SEVERITY         severity bucket of the ticket
    GHA_RUN_URL      this workflow run's URL
    RUN_DATE         ISO date (computed if absent)
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

HEALTH_RETRIES = 5
HEALTH_BACKOFF_SECONDS = 5
STREAM_TIMEOUT_SECONDS = 7200


def build_prompt(ticket: str, severity: str, run_date: str, gha_run_url: str) -> str:
    return f"""You are running the vuln-triage rover in a Cloudflare sandbox.

Repository cloned at: /workspace/application-sdk (on main).
Working directory: cd /workspace/application-sdk

Read and follow the orchestration:
  .mothership/vuln-triage/ORCHESTRATION.md
  .mothership/vuln-triage/CLAUDE.md
  .mothership/vuln-triage/tools.md

Run metadata (use these verbatim):
  TICKET:       {ticket}        # the Linear ticket to triage
  SEVERITY:     {severity}      # severity bucket of this ticket
  RUN_DATE:     {run_date}
  GHA_RUN_URL:  {gha_run_url}

GITHUB_TOKEN is pre-injected by the sandbox. Use `gh` for all GitHub operations.

Triage every CVE listed on ticket {ticket}. For each, classify per
ORCHESTRATION.md, then enact the SLA flow:
  - Critical/High -> open an allowlist PR (touching ONLY
    .security/base-allowlist.json, label "vuln-auto-merge") with expires =
    detection + SLA (CRITICAL 7d, HIGH 30d). For Case 1 also open a bump PR
    (touching ONLY the dep manifests, same label).
  - Medium/Low -> detail on the ticket with its SLA; never allowlist.
Both PR shapes auto-merge via vuln-auto-merge.yml once CI is green -- never
merge or push to main yourself. Keep the two PR shapes separate (the GHA
path-allowlist refuses anything mixed). Include a link to GHA_RUN_URL in the
ticket summary. Tag Vaibhav or Chris only when human action is genuinely
required (a bump needing source edits, or a base-image rebuild).

Linear / GPT proxy access: use the $PROXY_BASE and $PROXY_JWT env vars
injected by mothership (see tools.md)."""


def build_payload(
    ticket: str, severity: str, run_date: str, gha_run_url: str
) -> dict[str, Any]:
    return {
        "mode": "direct",
        "stream": True,
        "source": "github-cron",
        "source_id": f"vuln-triage-{ticket}-{run_date}",
        "repositories": ["atlanhq/application-sdk"],
        "base_branch": "main",
        "snapshot": "_base",
        "prompt": build_prompt(ticket, severity, run_date, gha_run_url),
        "max_timeout_seconds": 7200,
        "idle_timeout_seconds": 1800,
        "metadata": {"ticket": ticket, "severity": severity, "run_date": run_date},
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
    if st.event == "started":
        return f"[started]   session={_jget(data, 'session_id')} sandbox={_jget(data, 'sandbox_id')}"
    if st.event == "action":
        name = _jget(data, "action_name")
        return f"[action]    {name}" if name else None
    if st.event == "thought":
        return None
    if st.event == "response":
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
    return 0, f"Vuln triage completed successfully (cost={st.cost})."


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


def main() -> int:
    base_url = os.environ["MOTHERSHIP_URL"].rstrip("/")
    token = os.environ["HARNESS_TOKEN"]
    ticket = os.environ["TICKET"]
    severity = os.environ.get("SEVERITY", "")
    gha_run_url = os.environ.get("GHA_RUN_URL", "")
    run_date = os.environ.get("RUN_DATE") or time.strftime("%Y-%m-%d", time.gmtime())

    if not check_health(base_url):
        print("::error::Cannot reach mothership after retries")
        return 1

    payload = build_payload(ticket, severity, run_date, gha_run_url)
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
        return 1

    code, message = decide_exit(st)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
