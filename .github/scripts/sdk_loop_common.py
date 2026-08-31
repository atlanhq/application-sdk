#!/usr/bin/env python3
"""Shared vocabulary and primitives for the `@sdk-loop` lane.

`@sdk-loop` drives an open PR to merge-ready by alternating two phases —
review, then resolve only when the review found something — inside ONE workflow
run, one job per phase, up to `MAX_ROUNDS` pairs. It is additive: `@sdk-review`
and `@sdk-resolve` are untouched and keep working exactly as they do today.

Three things live here because both the fence and the phase runner need them
and they must not drift apart:

1. **The marker vocabulary.** `sdk_review_approve.py`, the dedupe, the
   reconcile and the downgrade/dismiss workflows all find their work by
   parsing HTML comment markers out of PR comments. The loop emits the
   IDENTICAL set, which is why none of them needed a change. Inventing a
   second format here would have forced a port of each.

2. **Head fencing.** Every phase re-reads the live head before acting. Work
   computed against a head that has since moved is discarded, never pushed —
   see `head_state()`.

3. **Agent invocation.** `opencode run` against the LiteLLM gateway, with the
   crucial detail that its exit code means nothing (below).

Environment shared by the whole lane:
    LITELLM_API_KEY     gateway bearer; the ONLY credential the agent gets
    LITELLM_BASE_URL    optional; defaults to https://llmproxy.atlan.dev
    GH_TOKEN            App installation token, scoped per phase
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Any, Sequence

# --------------------------------------------------------------------------
# Lane constants
# --------------------------------------------------------------------------

#: Round pairs (review + resolve) per run. Matches `@sdk-resolve`'s MAX_ROUNDS
#: so the two lanes give a PR the same number of chances. Skipped jobs cost
#: nothing, so this bounds only the worst case.
MAX_ROUNDS = 8

#: Review runs on the judgment model. There is deliberately NO ladder and no
#: adversarial second pass: the resolve phase opens by contesting the findings
#: it was handed (`.mothership/pr-resolve/ORCHESTRATION.md` §3d, "Fix every
#: finding (or prove it false)"), so a separate adversarial reviewer would be
#: paying twice for one job.
REVIEW_MODEL = "kimi-k3"

#: Resolve runs on the mechanical model — the same role split connector-pulse
#: uses, where luna is first-line and kimi is escalation.
RESOLVE_MODEL = "gpt-5.6-luna"

DEFAULT_GATEWAY = "https://llmproxy.atlan.dev"

#: Every model this lane may reach. opencode is pinned to exactly these so a
#: typo'd alias fails closed at config time instead of as a paid 400 mid-run.
ALLOWED_MODELS = (REVIEW_MODEL, RESOLVE_MODEL)

PLAYBOOK_REVIEW = ".mothership/pr-review/ORCHESTRATION.md"
PLAYBOOK_RESOLVE = ".mothership/pr-resolve/ORCHESTRATION.md"

TRIGGER = "@sdk-loop"

# --------------------------------------------------------------------------
# Marker vocabulary — shared with the existing lane, byte for byte
# --------------------------------------------------------------------------

MARK_VERDICT_BLOCK = "<!-- SDK_REVIEW -->"
MARK_STARTED = "<!-- SDK_REVIEW_STARTED -->"

VERDICT_RE = re.compile(r"<!--\s*VERDICT:\s*([A-Z_]+)\s*-->")
REVIEWED_HEAD_RE = re.compile(r"<!--\s*REVIEWED_HEAD:\s*([0-9a-f]{7,40})\s*-->")
ANSWERS_TRIGGER_RE = re.compile(r"<!--\s*ANSWERS_TRIGGER:\s*(\d+)\s*-->")

VERDICTS_CLEAN = ("READY_TO_MERGE",)
VERDICTS_ACTIONABLE = ("NEEDS_FIXES",)
#: Verdicts the loop cannot act on by fixing code. It stops and says why
#: rather than burning rounds on something a resolve phase cannot move.
VERDICTS_TERMINAL = ("BLOCKED", "NEEDS_HUMAN", "NEEDS_REBASE")

ALL_VERDICTS = VERDICTS_CLEAN + VERDICTS_ACTIONABLE + VERDICTS_TERMINAL


def parse_verdict(body: str) -> str | None:
    """Extract the verdict from a comment body, or None if it carries none."""
    match = VERDICT_RE.search(body or "")
    if match is None:
        return None
    verdict = match.group(1)
    return verdict if verdict in ALL_VERDICTS else None


def parse_reviewed_head(body: str) -> str | None:
    """Extract the sha a verdict describes."""
    match = REVIEWED_HEAD_RE.search(body or "")
    return match.group(1) if match else None


def parse_answers_trigger(body: str) -> str | None:
    """Extract the comment id a verdict answers."""
    match = ANSWERS_TRIGGER_RE.search(body or "")
    return match.group(1) if match else None


def is_verdict_comment(body: str) -> bool:
    return MARK_VERDICT_BLOCK in (body or "")


# --------------------------------------------------------------------------
# Head fencing
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class HeadState:
    """Where the branch is, relative to where this round expected it."""

    live: str
    baseline: str
    #: Shas this run pushed itself. A head equal to one of these is our own
    #: progress, not somebody else's commit.
    ours: tuple[str, ...] = ()

    @property
    def unchanged(self) -> bool:
        return self.live == self.baseline

    @property
    def moved_by_us(self) -> bool:
        return not self.unchanged and self.live in self.ours

    @property
    def moved_by_other(self) -> bool:
        """Someone outside this run advanced the branch.

        This is the only condition that discards in-flight work. It is NOT a
        failure and NOT a stop: the loop re-aims at the new head and re-enters
        the review phase (never resolve — nothing is fixed against a review of
        a different sha).
        """
        return not self.unchanged and not self.moved_by_us


def head_state(live: str, baseline: str, ours: Sequence[str] = ()) -> HeadState:
    return HeadState(live=live, baseline=baseline, ours=tuple(ours))


# --------------------------------------------------------------------------
# Dismissal ledger — what keeps disagreement converging without a human
# --------------------------------------------------------------------------


@dataclass
class DismissalLedger:
    """Findings the resolve phase contested, carried forward between rounds.

    The resolver may dismiss a finding as a false positive with a rationale
    (`.mothership/pr-resolve/ORCHESTRATION.md` §3d). Left alone, the next
    review re-raises it, the resolver refuses to argue twice, and the run stops
    for a human — which is exactly the outcome this lane exists to avoid. Now
    that `READY_TO_MERGE` needs an EMPTY `### Findings`, a re-raised dismissal
    would also withhold the approval forever.

    So dismissals are durable across rounds: the ledger is handed to the next
    review phase, which must either accept a dismissal or escalate it with a
    new reason — it may not silently re-list the same finding. The loop
    converges on its own; a human is needed only when the reviewer escalates.
    """

    entries: list[dict[str, str]] = field(default_factory=list)

    def add(self, finding_id: str, rationale: str, round_no: int) -> None:
        self.entries.append(
            {
                "id": finding_id,
                "rationale": rationale,
                "round": str(round_no),
            }
        )

    def ids(self) -> set[str]:
        return {e["id"] for e in self.entries}

    def as_prompt_section(self) -> str:
        """Render for injection into the next review phase's prompt."""
        if not self.entries:
            return ""
        lines = [
            "## Findings already dismissed in this run",
            "",
            "The resolver contested each of these with the rationale shown. Do NOT",
            "re-list one unchanged. Either accept the dismissal, or escalate it with",
            "a NEW reason that engages the rationale — never repeat the original",
            "wording, which would wedge the loop.",
            "",
        ]
        for entry in self.entries:
            lines.append(
                f"- **{entry['id']}** (round {entry['round']}): {entry['rationale']}"
            )
        return "\n".join(lines) + "\n"

    def to_json(self) -> str:
        return json.dumps({"entries": self.entries}, indent=2, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str | None) -> DismissalLedger:
        if not raw or not raw.strip():
            return cls()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        entries = payload.get("entries")
        if not isinstance(entries, list):
            return cls()
        clean = [
            e
            for e in entries
            if isinstance(e, dict) and isinstance(e.get("id"), str) and e["id"]
        ]
        return cls(entries=clean)


# --------------------------------------------------------------------------
# Agent invocation
# --------------------------------------------------------------------------


def gateway_base() -> str:
    return (os.environ.get("LITELLM_BASE_URL") or DEFAULT_GATEWAY).rstrip("/")


def opencode_config(model: str) -> dict[str, Any]:
    """`opencode.json` pinning the only provider and models this lane may use.

    Shell and network stay ALLOWED here, unlike the conformance fast lane which
    denies both. That is not an oversight: both playbooks are built on `gh` and
    `git` (the review playbook calls `gh pr` and `gh api` throughout; the
    resolve playbook pushes), so a shell-less agent could not run them at all.
    The containment is moved instead — the review phase is handed a token with
    no write scope, so the read-only contract is a property of the credential
    rather than a promise in a prompt.
    """
    if model not in ALLOWED_MODELS:
        raise ValueError(f"model {model!r} is not one of {ALLOWED_MODELS}")
    base = gateway_base()
    return {
        "$schema": "https://opencode.ai/config.json",
        "provider": {
            "llmproxy": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Atlan AI Gateway",
                "options": {
                    "baseURL": f"{base}/v1",
                    "apiKey": "{env:LITELLM_API_KEY}",
                },
                "models": {name: {} for name in ALLOWED_MODELS},
            }
        },
        "model": f"llmproxy/{model}",
        # Headless runs have nobody to answer an "ask", and opencode treats an
        # unanswered ask as a rejection — a connector-pulse run starved exactly
        # that way when its skill reads were auto-rejected. So everything a
        # phase legitimately does is allowed outright.
        "permission": {
            "read": "allow",
            "glob": "allow",
            "grep": "allow",
            "edit": "allow",
            "bash": "allow",
            "webfetch": "deny",
        },
    }


#: Env the agent process inherits. An allowlist, not the runner's whole
#: environment: every other GitHub Actions secret stays out of a process that
#: is reading untrusted PR content.
AGENT_ENV_PASSTHROUGH = (
    "PATH",
    "HOME",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "LITELLM_API_KEY",
    "LITELLM_BASE_URL",
    "GH_TOKEN",
    "GITHUB_REPOSITORY",
)


def agent_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in AGENT_ENV_PASSTHROUGH if k in os.environ}
    env.update(extra or {})
    return env


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def looks_authenticated(self) -> bool:
        """False when the transcript carries a gateway auth/model rejection.

        `opencode` exits 0 even on fatal errors — verified live in
        connector-pulse on an auth failure. An exit code therefore proves
        nothing, and every caller must decide success from observable effects
        (a verdict was posted, a tree changed). This is only a fast negative:
        it catches the failure that otherwise looks like "the model had no
        findings", which is the most dangerous false success in a review lane.
        """
        haystack = f"{self.stdout}\n{self.stderr}".lower()
        return not any(
            marker in haystack
            for marker in (
                "invalid model name",
                "authenticationerror",
                "invalid api key",
                "401 unauthorized",
                "403 forbidden",
                "insufficient_quota",
            )
        )


def run_agent(
    model: str,
    prompt: str,
    cwd: str,
    timeout_s: int,
    runner: Any = subprocess.run,
) -> AgentResult:
    """Invoke one agent phase. NEVER treat the exit code as success.

    The prompt is passed as an argv element, not through a shell, so untrusted
    PR content in it can never be interpreted as a command.
    """
    if shutil.which("opencode") is None:
        raise RuntimeError("opencode is not installed on this runner")
    config_path = os.path.join(cwd, "opencode.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(opencode_config(model), handle, indent=2)
    try:
        completed = runner(
            ["opencode", "run", "--model", f"llmproxy/{model}", prompt],
            cwd=cwd,
            env=agent_env(),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    finally:
        # Never leave the pinned config in a tree the resolve phase might commit.
        try:
            os.unlink(config_path)
        except FileNotFoundError:
            pass
    return AgentResult(
        exit_code=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


# --------------------------------------------------------------------------
# GitHub Actions plumbing
# --------------------------------------------------------------------------


def emit_outputs(**values: str) -> None:
    """Write step outputs. No-op off-runner so the scripts stay unit-testable."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            if "\n" in value:
                handle.write(f"{key}<<__SDKLOOP__\n{value}\n__SDKLOOP__\n")
            else:
                handle.write(f"{key}={value}\n")
