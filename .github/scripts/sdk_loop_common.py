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
    LITELLM_BASE_URL    gateway base URL. Required, and deliberately has no
                        default — the endpoint is supplied as a secret, never
                        written into this repo.
    GH_TOKEN            App installation token, scoped per phase
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Sequence

# --------------------------------------------------------------------------
# Lane constants
# --------------------------------------------------------------------------

#: Round pairs (review + resolve) per run. Matches `@sdk-resolve`'s MAX_ROUNDS
#: so the two lanes give a PR the same number of chances. Skipped jobs cost
#: nothing, so this bounds only the worst case.
MAX_ROUNDS = 8

#: Review runs on the same model the existing lanes review with — their
#: `MAIN_MODEL`, so all three lanes agree on what "a review" means and a
#: finding difference between them is about the harness, not the model.
#:
#: There is deliberately NO ladder and no adversarial second pass: the resolve
#: phase opens by contesting the findings it was handed
#: (`.mothership/pr-resolve/ORCHESTRATION.md` §3d, "Fix every finding (or prove
#: it false)"), so a separate adversarial reviewer would pay twice for one job.
#:
#: The alias contains a slash. opencode splits `--model` on the FIRST slash
#: only, so `gateway/xai/grok-4.6` resolves to provider `gateway`, model
#: `xai/grok-4.6` — and the key in the config's `models` map has to be the
#: full alias, slash included.
REVIEW_MODEL = "xai/grok-4.6"

#: Resolve runs on the mechanical model — the same role split connector-pulse
#: uses for its mechanical lane.
RESOLVE_MODEL = "gpt-5.6-luna"

#: The provider key inside `opencode.json`. A local alias only — the real
#: endpoint arrives at runtime from LITELLM_BASE_URL and is never in this repo.
PROVIDER = "gateway"

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
    """The AI gateway base URL, from the environment. No default, on purpose.

    The endpoint is configuration, not source: it is supplied as a secret and
    must not be written into this repository. Failing closed also beats
    guessing — a missing value would otherwise send the phase somewhere
    unintended, and the first sign would be a confusing auth error mid-run.
    """
    base = (os.environ.get("LITELLM_BASE_URL") or "").strip()
    if not base:
        raise RuntimeError(
            "LITELLM_BASE_URL is not set. The AI gateway endpoint is supplied "
            "as a secret; there is no built-in default."
        )
    return base.rstrip("/")


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
            PROVIDER: {
                "npm": "@ai-sdk/openai-compatible",
                "name": "Atlan AI Gateway",
                "options": {
                    "baseURL": f"{base}/v1",
                    "apiKey": "{env:LITELLM_API_KEY}",
                },
                "models": {name: {} for name in ALLOWED_MODELS},
            }
        },
        "model": f"{PROVIDER}/{model}",
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


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


def parse_key_spend(payload: dict[str, Any]) -> float | None:
    """Cumulative USD on the key, from a LiteLLM ``/key/info`` body."""
    info = payload.get("info") or {}
    spend = info.get("spend")
    return float(spend) if isinstance(spend, (int, float)) else None


def gateway_spend() -> float | None:
    """Cumulative USD spend on this gateway key, or None if unreadable.

    None means exactly that — endpoint disabled, key lacks permission, gateway
    unreachable. Callers must report "unavailable" rather than substitute a
    zero: a run that silently claims $0.00 is worse than one that admits it
    could not measure.
    """
    key = os.environ.get("LITELLM_API_KEY", "")
    if not key:
        return None
    request = urllib.request.Request(f"{gateway_base()}/key/info")
    request.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return parse_key_spend(json.loads(response.read()))
    except Exception:
        return None


def spend_delta(before: float | None, after: float | None) -> float | None:
    """Cost of a phase, as the movement in key spend across it.

    The key is the billing unit, not the run — so this is only the phase's own
    cost when nothing else billed to the same key during the window. This lane
    runs PRs fully in parallel and shares its key with other automation, so
    treat the figure as an UPPER BOUND on the phase and say so wherever it is
    shown. Attributing precisely would need per-request tagging the gateway
    does not currently give us.

    A negative delta is not possible from our own usage; it means the counter
    reset or rolled over, so the measurement is discarded rather than reported
    as a saving.
    """
    if before is None or after is None:
        return None
    delta = after - before
    return delta if delta >= 0 else None


#: Default ceiling on what one loop may spend, in USD.
#:
#: Sized to let a HEALTHY loop finish and stop a runaway — not the other way
#: round. A ceiling that cuts off a converging run makes the lane look broken
#: and teaches people to raise it blindly, which is worse than no ceiling.
#:
#: From 61 stamped sdk-review runs in this repo: median $8.24 a review, mean
#: $11.59. Cost is almost uncorrelated with PR size (r=0.20) — a 12-line PR
#: cost $7.95, an 8033-line PR $2.64, a 31-line PR $27.29 — because the fixed
#: ~200KB playbook, the Phase 2 sub-agents and the per-finding Phase 4
#: verification dominate. Findings drive cost, not lines.
#:
#: The playbook puts typical convergence at 2-3 rounds, so a healthy loop is
#: ~3 reviews + 2 resolves. At the median that is ~$25 of review alone, and
#: 50 leaves room for the resolves plus a PR on the expensive tail. Eight
#: unbounded round-pairs would be ~$130+.
#:
#: The resolve half of that arithmetic is INFERRED: there are no stamped
#: @sdk-resolve costs in this repo to measure, because the lane has barely
#: run. Re-tune this from the first real loops rather than trusting it.
DEFAULT_MAX_USD = 50.0


def run_budget() -> float:
    raw = (os.environ.get("SDK_LOOP_MAX_USD") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_USD
    return value if value > 0 else DEFAULT_MAX_USD


def budget_exceeded(spent: float | None, budget: float) -> bool:
    """Whether the NEXT phase should be refused.

    Checked before a phase starts, never mid-flight: a half-finished review is
    money spent for nothing, so the loop stops on a round boundary where the
    work so far is still worth something.

    Unmeasurable spend (None) never blocks. The gateway failing to report is
    not evidence of overspend, and turning a metrics outage into a stalled lane
    would be the wrong trade — the round cap still bounds the worst case.
    """
    if spent is None:
        return False
    return spent >= budget


def format_usd(amount: float | None) -> str:
    return "unavailable" if amount is None else f"${amount:,.4f}"


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


#: opencode prints a fatal error as a line starting `Error:` and then exits 0.
#: Seen live: `Error: [DecimalError] Invalid argument: [object Object]`, 11ms
#: into round 1, after which the phase did nothing for four rounds while the
#: harness read a stale third-party verdict as its own output.
_ABORT_RE = re.compile(r"^\s*(?:\x1b\[[0-9;]*m)*Error:\s*(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class AgentResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def abort_reason(self) -> str:
        """The agent's own fatal error, if it printed one."""
        match = _ABORT_RE.search(f"{self.stdout}\n{self.stderr}")
        return match.group(1).strip() if match else ""

    @property
    def completed(self) -> bool:
        """Whether the agent ran to completion rather than aborting.

        The exit code is useless here — opencode returns 0 on fatal errors — so
        this reads the transcript for the error it prints on the way out. A
        phase that aborted must be reported as failed, never left for the
        caller to infer from whatever side effects are lying around.
        """
        return not self.abort_reason

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
    transcript_path: str | None = None,
    sink: Any = None,
) -> AgentResult:
    """Invoke one agent phase, STREAMING its output to the job log.

    Streaming is not a nicety here. A review phase can run three quarters of an
    hour, and buffering it means the job shows nothing at all until it ends —
    so a stalled phase and a working one look identical for 45 minutes, which
    is the exact failure that made the mothership lane miserable to operate.
    Every line is echoed as it arrives and also kept, because the caller has to
    parse the transcript afterwards (dismissals, gateway rejections).

    The full transcript is additionally written to *transcript_path* when given,
    so the workflow can upload it as an artifact — GitHub truncates long job
    logs, and the interesting part of a failed review is usually the middle.

    The prompt is passed as an argv element, never through a shell, so
    untrusted PR content in it cannot be interpreted as a command.
    """
    if shutil.which("opencode") is None:
        raise RuntimeError("opencode is not installed on this runner")
    emit = sink or (lambda line: print(line, flush=True))
    config_path = os.path.join(cwd, "opencode.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(opencode_config(model), handle, indent=2)

    lines: list[str] = []
    try:
        process = subprocess.Popen(
            ["opencode", "run", "--model", f"{PROVIDER}/{model}", prompt],
            cwd=cwd,
            env=agent_env(),
            stdout=subprocess.PIPE,
            # Merged so the transcript preserves the real interleaving; the
            # caller only ever reads them together anyway.
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        # A watchdog rather than a deadline inside the read loop: readline()
        # blocks, so a phase that hangs with no output would otherwise never
        # reach the check and would sit until the job timeout instead of ours.
        timer = threading.Timer(timeout_s, process.kill)
        timer.daemon = True
        timer.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                lines.append(line)
                emit(line)
            process.wait()
        finally:
            timer.cancel()
    finally:
        # Never leave the pinned config in a tree the resolve phase might commit.
        try:
            os.unlink(config_path)
        except FileNotFoundError:
            pass

    transcript = "\n".join(lines)
    if transcript_path:
        try:
            with open(transcript_path, "w", encoding="utf-8") as handle:
                handle.write(transcript)
        except OSError:
            # Losing the artifact copy must not fail a phase that otherwise
            # produced a verdict.
            pass
    return AgentResult(exit_code=process.returncode, stdout=transcript, stderr="")


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
