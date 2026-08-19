#!/usr/bin/env python3
"""Dispatch ONE conformance-remediation unit directly to a rover — the e2e proof.

This is the Stage-2 verification from FND-18, runnable **before any of the three
PRs merge**: the rover's playbook, identity rules, tools doc and the /remediate
skill are read from *this checkout* and injected as ``session_files``, so the
whole lane can be exercised from a branch. The mothership orchestrator is
deliberately not in this path — this proves the unit itself (rover + playbook +
models + gates + PR delivery); the orchestrator PR proves the queueing around it.

Flow mirrors ``mothership_sandbox_dispatch.py`` (health check → POST
/api/sandbox/execute → parse SSE → exit on final status), with one addition
borrowed from ``sdk_evolution_dispatch.py``: the stream's text is buffered and
the rover's ``=== REMEDIATION SUMMARY ===`` block + ``RESULT:`` line are mined
into ``GITHUB_STEP_SUMMARY``, so the run's outcome — N of M cleared, the PR URL,
which model served which lane, cost — is readable without opening logs.

Environment:
    MOTHERSHIP_URL       base URL (e.g. https://mothership.atlan.dev)
    HARNESS_TOKEN        bearer for /api/sandbox/execute
    TARGET_REPO          e.g. atlanhq/atlan-netsuite-app
    RULE_ID              exactly one rule, e.g. L011
    SUITE_VERSION        pinned conformance version (default 0.20.1)
    GHA_RUN_URL          this workflow run's URL
    GITHUB_STEP_SUMMARY  step-summary path (optional outside CI)
    DRY_RUN              '1' -> print the payload, skip the POST
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any

HEALTH_RETRIES = 5
HEALTH_BACKOFF_SECONDS = 5
# The rover's ceiling is 5400s; the stream timeout sits above it so the sandbox's
# own timeout — which produces a structured error event — always fires first.
STREAM_TIMEOUT_SECONDS = 5700
SANDBOX_MAX_TIMEOUT_SECONDS = 5400
SANDBOX_IDLE_TIMEOUT_SECONDS = 1800

# The production pair shipped by sdk-review.yml. small_fast_model must be
# explicit: mothership computes `fast = small_fast_model or model`, so pinning
# `model` alone would put the background lane on the main model.
MAIN_MODEL = "kimi-k3"
SUBAGENT_MODEL = "gpt-5.6-luna"
AI_GATEWAY_KEY_NAME = "sdk_review"
DEFAULT_SUITE_VERSION = "0.20.1"

# Blessed session-file prefixes (mothership sandbox_api). Anything else is
# rejected by the sandbox API, silently from the rover's point of view.
SESSION_PREFIX = "/workspace/.mothership/session/"
SKILL_PREFIX = "/home/sandbox/.claude/skills/"

RULE_ID_RE = re.compile(r"^[A-Z]\d{3}$")

# Over a long stream the raw text would grow unbounded; the summary block sits at
# the very end, so keeping the tail always preserves it.
BUFFER_CAP_BYTES = 65536

SUMMARY_START = "=== REMEDIATION SUMMARY ==="
SUMMARY_END = "=== END REMEDIATION SUMMARY ==="
RESULT_RE = re.compile(r"^RESULT:\s*(?P<kind>[a-z-]+)\s*:?\s*(?P<detail>.*)$", re.M)


def _read(rel: str) -> str:
    """A playbook file from this checkout — the branch under test, not main."""
    path = Path(rel)
    if not path.is_file():
        raise FileNotFoundError(
            f"{rel} not found — this script must run from an application-sdk "
            "checkout that carries the conformance-remediation playbook"
        )
    return path.read_text(encoding="utf-8")


def build_prompt(
    *, repo: str, rule_id: str, suite_version: str, gha_run_url: str
) -> str:
    series = rule_id[:1].upper()
    return f"""You are the conformance-remediation rover (e2e verification run).

Repository cloned at: /workspace/{repo.split("/")[-1]}
Working directory: cd /workspace/{repo.split("/")[-1]}

Read and follow, in this order:
  /workspace/.mothership/session/REMEDIATION.md   (your playbook — MANDATORY)
  /workspace/.mothership/session/CLAUDE.md        (identity + hard limits)
  /workspace/.mothership/session/tools.md         (tools + commands)
  /workspace/.mothership/session/PRIOR_DECISIONS.json

Run metadata (use verbatim; do NOT re-derive):
  REPO:                {repo}
  RULE_ID:             {rule_id}
  SERIES:              {series}
  TIER:                block
  DELIVERY:            one_pr_per_rule
  BASE_REF:            main
  SUITE_VERSION:       {suite_version}
  APPLY_UNVERIFIABLE:  false
  RUN_ID:              e2e-{rule_id.lower()}
  GHA_RUN_URL:         {gha_run_url}

Remediate EXACTLY the one rule {rule_id} in {repo}. Do not widen scope to a
second rule you happen to notice — note it and move on.

GITHUB_TOKEN is pre-injected. Use `gh` for all GitHub operations. Never
force-push, never push to main, never merge, and never edit tests/, .github/ or
conformance/ in the app repo.

Delegate the per-file edit drafting and the refutation pass to Task sub-agents
(they run on the cheaper model); keep the serial judgment for yourself.

You are the judge: decide, record, continue. Never request interactive input —
there is no human attached to this run. When there is genuinely no evidence
either way, abstain and report rule-review.

Finish with the two report blocks and exactly one RESULT line, as Stage 8
specifies. Include main_model and subagent_model in the summary — this run
exists to prove the model split, so report what actually served each lane."""


def build_payload(
    *, repo: str, rule_id: str, suite_version: str, gha_run_url: str
) -> dict[str, Any]:
    return {
        "mode": "direct",
        # From a GitHub runner the call crosses nginx (~60s upstream read
        # timeout), so SSE keeps the connection alive — same reason every other
        # lane in this repo streams.
        "stream": True,
        "source": "github-e2e",
        "source_id": f"conformance-remediation-e2e-{repo.split('/')[-1]}-{rule_id}",
        "repositories": [repo],
        "base_branch": "main",
        "snapshot": "_base",
        "ai_gateway_key_name": AI_GATEWAY_KEY_NAME,
        "model": MAIN_MODEL,
        "small_fast_model": SUBAGENT_MODEL,
        "env_vars": {"CLAUDE_CODE_SUBAGENT_MODEL": SUBAGENT_MODEL},
        "session_files": {
            f"{SESSION_PREFIX}REMEDIATION.md": _read(
                ".mothership/conformance-remediation/ORCHESTRATION.md"
            ),
            f"{SESSION_PREFIX}CLAUDE.md": _read(
                ".mothership/conformance-remediation/CLAUDE.md"
            ),
            f"{SESSION_PREFIX}tools.md": _read(
                ".mothership/conformance-remediation/tools.md"
            ),
            f"{SESSION_PREFIX}PRIOR_DECISIONS.json": "[]",
            f"{SKILL_PREFIX}remediate/SKILL.md": _read(
                ".claude/skills/remediate/SKILL.md"
            ),
        },
        "prompt": build_prompt(
            repo=repo,
            rule_id=rule_id,
            suite_version=suite_version,
            gha_run_url=gha_run_url,
        ),
        "max_timeout_seconds": SANDBOX_MAX_TIMEOUT_SECONDS,
        "idle_timeout_seconds": SANDBOX_IDLE_TIMEOUT_SECONDS,
        "metadata": {"rule": rule_id, "repo": repo, "suite_version": suite_version},
    }


class StreamState:
    """Outcome of the SSE stream plus a tail buffer for summary mining."""

    def __init__(self) -> None:
        self.event = ""
        self.got_event = False
        self.completed = False
        self.errored = False
        self.status = ""
        self.cost = ""
        self.err_code = ""
        self.err_msg = ""
        self.buffer = ""

    def append_text(self, text: str) -> None:
        if not text:
            return
        self.buffer += text
        if len(self.buffer) > BUFFER_CAP_BYTES:
            self.buffer = self.buffer[-BUFFER_CAP_BYTES:]


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


def process_line(line: str, st: StreamState) -> str | None:
    """One raw SSE line -> optional log line. Mirrors mothership_sandbox_dispatch."""
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
    if st.event in ("thought", "response"):
        # Buffered, not printed: this is where the REMEDIATION SUMMARY block and
        # the RESULT line arrive.
        st.append_text(_jget(data, "text") or _jget(data, "content"))
        return None
    if st.event == "elicitation":
        st.errored = True
        st.err_code = "elicitation"
        st.err_msg = "Sandbox requested interactive input; the playbook forbids this"
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


def process_stream(lines: Iterable[str]) -> StreamState:
    st = StreamState()
    for raw in lines:
        msg = process_line(raw.rstrip("\n"), st)
        if msg:
            print(msg, flush=True)
    return st


def mine_summary(text: str) -> tuple[dict[str, str], str, str]:
    """Extract the summary block fields and the final RESULT line from the tail.

    Uses the LAST occurrence of each marker: the playbook itself (quoted into the
    rover's context) contains template copies, and mining those would report the
    template instead of the run.
    """
    fields: dict[str, str] = {}
    s = text.rfind(SUMMARY_START)
    if s != -1:
        e = text.find(SUMMARY_END, s)
        if e != -1:
            for line in text[s + len(SUMMARY_START) : e].splitlines():
                line = line.strip()
                if ":" in line and not line.startswith("#"):
                    key, _, value = line.partition(":")
                    fields[key.strip()] = value.strip()

    kind, detail = "", ""
    matches = list(RESULT_RE.finditer(text))
    if matches:
        kind = matches[-1].group("kind").strip()
        detail = matches[-1].group("detail").strip()
    return fields, kind, detail


def render_step_summary(
    st: StreamState, fields: dict[str, str], kind: str, detail: str
) -> str:
    rows = [
        "## Conformance remediation — e2e run",
        "",
        f"**RESULT:** `{kind or 'not reported'}` {detail}",
        "",
        "| field | value |",
        "|---|---|",
    ]
    for key in (
        "repo",
        "rule",
        "tier",
        "suite_version",
        "findings_before",
        "findings_after",
        "cleared",
        "reverted",
        "refuted",
        "pr_url",
        "main_model",
        "subagent_model",
    ):
        if key in fields:
            rows.append(f"| {key} | {fields[key]} |")
    rows.append(f"| sandbox status | {st.status or 'unknown'} |")
    if st.cost:
        rows.append(f"| cost_usd | {st.cost} |")
    if st.err_msg:
        rows.append(f"| error | {st.err_code}: {st.err_msg} |")
    return "\n".join(rows) + "\n"


def decide_exit(st: StreamState, kind: str) -> tuple[int, str]:
    """Exit non-zero unless the sandbox completed AND the rover reported a state.

    The two are independent failures: a sandbox can complete while the rover
    never printed a RESULT (we do not know what it did), and a rover can print a
    RESULT right before the sandbox dies (the delivery may not have happened).
    Both must hold before this run is called green.
    """
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
    if not kind:
        return 1, "::error::Sandbox completed but the rover reported no RESULT line"
    if kind == "error":
        return 1, "::error::Rover reported RESULT: error"
    return 0, f"E2E run finished: RESULT={kind}"


def check_health(base_url: str) -> bool:
    for attempt in range(1, HEALTH_RETRIES + 1):
        try:
            with urllib.request.urlopen(f"{base_url}/health", timeout=10) as resp:
                if (getattr(resp, "status", None) or resp.getcode()) == 200:
                    print(f"Mothership reachable (attempt {attempt})")
                    return True
        except (urllib.error.URLError, OSError) as e:
            print(f"Mothership unreachable ({e}), retry {attempt}/{HEALTH_RETRIES}")
        if attempt < HEALTH_RETRIES:
            time.sleep(HEALTH_BACKOFF_SECONDS)
    return False


def main() -> int:
    repo = os.environ.get("TARGET_REPO", "")
    rule_id = os.environ.get("RULE_ID", "").strip().upper()
    suite_version = os.environ.get("SUITE_VERSION") or DEFAULT_SUITE_VERSION
    gha_run_url = os.environ.get("GHA_RUN_URL", "")

    if not repo or "/" not in repo:
        print(f"::error::TARGET_REPO must be owner/name, got {repo!r}")
        return 1
    if not RULE_ID_RE.match(rule_id):
        print(f"::error::RULE_ID must be one rule like L011, got {rule_id!r}")
        return 1

    payload = build_payload(
        repo=repo, rule_id=rule_id, suite_version=suite_version, gha_run_url=gha_run_url
    )

    if os.environ.get("DRY_RUN") == "1":
        preview = dict(payload)
        preview["session_files"] = {
            k: f"<{len(v)} chars>" for k, v in payload["session_files"].items()
        }
        preview["prompt"] = f"<{len(payload['prompt'])} chars>"
        print(json.dumps(preview, indent=2))
        return 0

    base_url = os.environ.get("MOTHERSHIP_URL", "").rstrip("/")
    token = os.environ.get("HARNESS_TOKEN", "")
    if not base_url or not token:
        print("::error::MOTHERSHIP_URL and HARNESS_TOKEN are required")
        return 1
    if not check_health(base_url):
        print("::error::Cannot reach mothership after retries")
        return 1

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

    fields, kind, detail = mine_summary(st.buffer)
    summary = render_step_summary(st, fields, kind, detail)
    print(summary)
    step_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as f:
            f.write(summary)

    code, message = decide_exit(st, kind)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
