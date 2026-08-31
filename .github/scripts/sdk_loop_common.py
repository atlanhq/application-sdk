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
import time
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
#: `grok-4.6`, dotted — that is the alias the gateway serves. The undotted
#: form was tried and rejected: "Invalid model name passed in
#: model=xai/grok-4-6", and `/v1/models` lists `xai/grok-4.6` and no variant.
#:
#: Superseded note, kept because it recorded a real finding: two live rounds
#: died 11ms in with
#: `Error: [DecimalError] Invalid argument: [object Object]` — decimal.js
#: refusing a non-numeric argument — before the agent read anything. The
#: dot was blamed. It was the wrong culprit — swapping it merely moved the
#: failure from a crash to a 400. The real cause was an unpriced model; see
#: `opencode_config`.
#:
#: The alias also contains a slash. opencode splits `--model` on the FIRST
#: slash only, so `gateway/xai/grok-4.6` resolves to provider `gateway`, model
#: `xai/grok-4.6` — the key in the config's `models` map is the full alias.
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


#: The playbook's Phase 2 agents, registered so opencode's Task tool can
#: dispatch them. §2a says "dispatch agents via the Agent tool" — Claude Code's
#: name for it; opencode calls the same mechanism `Task`, and it runs them in
#: parallel just the same. Without registering them the primary agent has
#: nothing to delegate TO, and Phase 2's whole fan-out silently collapses into
#: one agent doing everything: a review that still produces a verdict, just a
#: worse one, with nothing in the output to say so.
#:
#: The prompts are the EXISTING files, loaded by reference. Nothing is copied,
#: so the agents stay owned by `.mothership/pr-review/agents/` and keep the
#: reference rules #3530 gave them.
#: Audited against both playbooks on 2026-08-31: `Agent tool` (ORCHESTRATION
#: lines 556 and 918) is the ONLY harness-specific capability either one names.
#: No Skills, no MCP, no glean, no WebFetch, no TodoWrite — everything else is
#: bash, gh, git and file reads, all of which opencode has. So subagent
#: registration was the whole gap, and the resolve playbook needs nothing: it
#: has no agents directory and dispatches none.
#:
#: `adversarial.md` is deliberately absent from this list — Wave 2 is skipped
#: in this lane (see the review prompt), so registering it would advertise a
#: capability the phase is told not to use.
PHASE2_AGENTS = (
    "correctness",
    "quality",
    "structure",
    "reachability",
    "conformance",
    "ci-config",
    "toolkit-review",
)


def _agent_prompt(name: str) -> str:
    """The playbook's agent brief, read from this runner's own checkout.

    Deliberately NOT wrapped in a try/except. A missing brief means the review
    would run a domain agent with no instructions and still emit a verdict —
    the single most dangerous failure mode in a review lane, because nothing
    downstream distinguishes it from a clean pass.
    """
    path = os.path.join(".mothership", "pr-review", "agents", f"{name}.md")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def review_subagents(model: str) -> dict[str, Any]:
    """opencode subagent entries for the playbook's Phase 2 agents.

    Each is read-only by construction: the review lane holds a token with no
    write scope, and denying `edit` here makes that true of the agent as well
    as the credential rather than relying on either alone.
    """
    return {
        name: {
            "description": f"SDK review — {name} domain agent (Phase 2 Wave 1).",
            "mode": "subagent",
            "model": f"{PROVIDER}/{model}",
            # A dispatched sub-agent is INVISIBLE while it runs — the parent
            # prints one `• agent` line and nothing more until it returns — so
            # nothing else in this lane can bound a sub-agent that loops. Two
            # measured runs sat in one such call for 904s and 2582s.
            #
            # Sizing: the PARENT completes a whole review in ~17 model round
            # trips. A single-domain sub-agent doing more than 60 is not being
            # thorough, it is looping, and at ~13s per round trip 60 is already
            # ~13 minutes. This forces a text-only answer at the cap rather
            # than running forever, so the review degrades to a partial finding
            # set instead of losing the phase entirely.
            #
            # NOT claimed as the fix for the stall: token accounting does not
            # attribute sub-agent usage, so whether those runs burned steps or
            # hung inside one is still unknown. This bounds one of the two.
            "maxSteps": SUBAGENT_MAX_STEPS,
            # Inlined rather than `{file:./.mothership/pr-review/agents/…}`.
            # The template's resolution against a DOT-directory is unverified
            # here, and the same path returns 0 matches through the agent's own
            # Glob — so a template that silently resolved to nothing would hand
            # the sub-agent no instructions at all and look exactly like a slow
            # review. Reading it in Python removes the question: the file is
            # either present or the phase fails loudly with a traceback.
            "prompt": _agent_prompt(name),
            # Enumerated in full, mirroring the primary agent, because a
            # PARTIAL block here was not equivalent: `external_directory` and
            # `doom_loop` ask unless listed, headless opencode has nobody to
            # answer an ask, and an unlisted tool IS an ask. The primary
            # carries the full list and works; the sub-agents carried only
            # four keys and stalled. That is suggestive, NOT a diagnosis —
            # a blocking ask cannot explain the #3478 sub-agent that returned
            # a complete verdict after 15 minutes on this same partial block.
            # Aligned because the asymmetry is indefensible either way, not
            # because it is known to be the stall.
            #
            # `edit` stays denied so read-only holds in the agent as well as
            # in its token; the outbound channels stay denied for the same
            # injection reason. Last matching rule wins, so these override
            # the wildcard.
            "permission": {
                "*": "allow",
                "read": "allow",
                "glob": "allow",
                "grep": "allow",
                "bash": "allow",
                "skill": "allow",
                "lsp": "allow",
                "question": "allow",
                "external_directory": "allow",
                "doom_loop": "allow",
                "edit": "deny",
                "webfetch": "deny",
                "websearch": "deny",
            },
        }
        for name in PHASE2_AGENTS
    }


def opencode_config(model: str, with_subagents: bool = False) -> dict[str, Any]:
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
                # Cost is DECLARED rather than left empty. This is a
                # HYPOTHESIS under test, not a diagnosis, for the crash that
                # killed the first three live runs:
                #
                #   Error: [DecimalError] Invalid argument: [object Object]
                #
                # What is PROVEN: it dies 11ms in, before any network call —
                # the undotted alias got as far as a real 400 from the
                # gateway, so the crash is local to opencode's model
                # resolution. `[object Object]` reaching decimal.js means an
                # OBJECT was passed where a number was wanted, and an
                # unpopulated cost table is the obvious candidate for that
                # object. connector-pulse's aliases resolve in opencode's
                # bundled registry; a gateway-only alias like `xai/grok-4.6`
                # does not, which fits.
                #
                # What is NOT proven: that cost is the object in question. If
                # this run still crashes, the next suspects are opencode
                # parsing `4.6` out of the id as a version, and the nested
                # slash in `gateway/xai/grok-4.6`.
                #
                # Zeroes rather than real prices: this lane bills through the
                # gateway key and reads spend from /key/info, so opencode's own
                # accounting is unused. Declaring it only stops it guessing.
                "models": {
                    name: {
                        "cost": {
                            "input": 0,
                            "output": 0,
                            "cache_read": 0,
                            "cache_write": 0,
                        }
                    }
                    for name in ALLOWED_MODELS
                },
            }
        },
        "model": f"{PROVIDER}/{model}",
        # Headless runs have nobody to answer an "ask", and opencode treats an
        # unanswered ask as a rejection — a connector-pulse run starved exactly
        # that way when its skill reads were auto-rejected. So everything a
        # phase legitimately does is allowed outright.
        **({"agent": review_subagents(model)} if with_subagents else {}),
        # Every permission the playbooks can reach, in one place.
        #
        # Headless opencode has nobody to answer an "ask", so it auto-REJECTS
        # one — and an unlisted tool is an ask. A live round died after
        # checkout with "The user rejected permission to use this specific
        # tool call", naming no tool. `task` was the one that mattered: the
        # subagents were registered but the primary agent was never allowed to
        # DISPATCH them, so Phase 2 could not fan out even once registered.
        #
        # Enumerated rather than left to defaults because the defaults are not
        # uniform: `external_directory` and `doom_loop` ask unless listed, and
        # the review playbook legitimately does both — it reads /tmp scratch
        # files and re-runs similar greps across a large diff.
        #
        # `webfetch` and `websearch` stay DENIED, and listing everything else
        # is what makes that denial deliberate rather than incidental. The
        # agent reads untrusted PR content, so an injected prompt must not
        # reach an outbound channel it picks itself. Last matching rule wins,
        # so both override the wildcard.
        "permission": {
            "*": "allow",
            "read": "allow",
            "edit": "allow",
            "glob": "allow",
            "grep": "allow",
            "bash": "allow",
            "task": "allow",
            "skill": "allow",
            "lsp": "allow",
            "question": "allow",
            "external_directory": "allow",
            "doom_loop": "allow",
            "webfetch": "deny",
            "websearch": "deny",
        },
    }


# --------------------------------------------------------------------------
# Cost
# --------------------------------------------------------------------------


def parse_opencode_usage(text: str) -> dict[str, int]:
    """Token counts from `opencode stats`, which is the authoritative source.

    Preferred over the gateway's /key/info spend for two reasons. It is
    attributable to THIS phase rather than to a key several lanes share, and
    it reports cache_read/cache_write — the one measurement that decides
    whether the fixed ~90KB playbook prefix is being re-paid for on every one
    of a phase's ~24 turns, which is where this lane's cost actually lives.

    Dollars are deliberately NOT taken from here: the model entries declare
    zero cost (see `opencode_config`), so opencode's own total is $0.00 by
    construction. Tokens are unaffected by that and are exact.
    """
    fields = {
        "input": r"Input\s+([\d,]+)",
        "output": r"Output\s+([\d,]+)",
        "cache_read": r"Cache Read\s+([\d,]+)",
        "cache_write": r"Cache Write\s+([\d,]+)",
    }
    out: dict[str, int] = {}
    for key, pattern in fields.items():
        match = re.search(pattern, text or "")
        if match:
            out[key] = int(match.group(1).replace(",", ""))
    return out


def opencode_usage(cwd: str, runner: Any = subprocess.run) -> dict[str, int]:
    """Run `opencode stats` for today and parse it. Never raises."""
    try:
        proc = runner(
            ["opencode", "stats", "--days", "1"],
            cwd=cwd,
            env=agent_env(),
            capture_output=True,
            text=True,
            timeout=60,
        )
    except Exception:
        return {}
    return parse_opencode_usage(proc.stdout or "")


def usage_total(usage: dict[str, int]) -> int | None:
    """Billable tokens for a phase: input + output.

    Cache reads are already counted inside `input` by the provider, so adding
    them again would double-count the very quantity we want to watch shrink.
    """
    if not usage:
        return None
    return usage.get("input", 0) + usage.get("output", 0)


def format_tokens(total: int | None) -> str:
    return "unavailable" if total is None else f"{total:,}"


def format_usage(usage: dict[str, int]) -> str:
    """One-line token summary, with the cache signal made explicit."""
    if not usage:
        return "tokens unavailable"
    parts = [f"in {usage.get('input', 0):,}", f"out {usage.get('output', 0):,}"]
    read, write = usage.get("cache_read", 0), usage.get("cache_write", 0)
    if read or write:
        parts.append(f"cache r/w {read:,}/{write:,}")
    else:
        # Worth flagging rather than omitting: no cache activity at all means
        # the fixed playbook prefix is being re-sent every turn.
        parts.append("cache MISS (no reuse)")
    return " · ".join(parts)


#: Consecutive re-aims before the loop gives up. A re-aim is not progress —
#: it discards the round's work and starts over — so it needs a budget of its
#: own. Without one, a branch that keeps moving (or a harness that keeps
#: MISREADING the head as moved) silently eats the whole round cap: a live run
#: burned three rounds on the identical mismatch, each reporting `reaim` and
#: advancing as though something had changed.
#: Hard cap on a sub-agent's agentic iterations. See `review_subagents`.
SUBAGENT_MAX_STEPS = 60

#: Kill an agent that has printed nothing for this long. Sized off measurement,
#: not taste: across observed runs the longest gap between two output lines in a
#: HEALTHY phase was well under a minute, while a stalled one produced 43
#: minutes of nothing. Five minutes sits far above the former and far below the
#: latter, so it cannot fire on a slow-but-working phase.
IDLE_TIMEOUT_S = 5 * 60

MAX_CONSECUTIVE_REAIMS = 2


def reaim_exhausted(consecutive: int) -> bool:
    """Whether a further re-aim should stop the run instead of costing a round.

    Consecutive, not cumulative: a branch legitimately edited twice over a long
    drive is normal, whereas two back to back with no completed round between
    them means the loop is not converging on any one commit.
    """
    return consecutive >= MAX_CONSECUTIVE_REAIMS


#: Default ceiling on what one loop may spend, in TOKENS.
#:
#: Tokens rather than dollars, for three reasons found the hard way. The
#: gateway's /key/info returns 403 for this key — reading account-wide spend
#: needs an admin-scoped key, and a review lane has no business holding one. A
#: spend delta on a shared key was only ever an upper bound anyway, since other
#: lanes bill to it concurrently. And `opencode stats` already reports tokens
#: per phase, exactly.
#:
#: NOT YET A WORKING GUARD, and the number says so. Two complete runs have now
#: reported `in 260 · out 10` and `in 230 · out 5` — for a 20-minute and a
#: 45-minute review respectively. `opencode stats` aggregates its own store and
#: demonstrably does not attribute what a dispatched sub-agent spends, which on
#: those runs was substantially all of it.
#:
#: So the ceiling is left far out of reach ON PURPOSE. Tightening it to a
#: plausible-looking figure would produce a guard that reads as working, fires
#: against a number covering a few percent of real usage, and stops healthy
#: runs at random. Until a run reports a total consistent with its wall-clock,
#: treat the `tokens:` line as diagnostics and the round cap as the real bound.
DEFAULT_MAX_TOKENS = 20_000_000


def token_budget() -> int:
    raw = (os.environ.get("SDK_LOOP_MAX_TOKENS") or "").strip()
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value > 0 else DEFAULT_MAX_TOKENS


def token_budget_exceeded(spent: int | None, budget: int) -> bool:
    """Whether the NEXT phase should be refused.

    Checked before a phase starts, never mid-flight: a half-finished review is
    spend with nothing to show for it, so the loop stops on a round boundary
    where the work so far still counts.

    An unmeasured total (None) never blocks — a failed stats read is not
    evidence of overspend, and the round cap still bounds the worst case.
    """
    if spent is None:
        return False
    return spent >= budget


#: A ripgrep config that makes hidden paths searchable.
#:
#: opencode's Glob and Grep are ripgrep-backed, and ripgrep skips dot-paths.
#: The whole playbook lives under `.mothership/`, so on the first complete run
#: the reviewer got 0 matches TWICE: once globbing its own agent definitions,
#: and once grepping the reference rules for prior art on the finding it was
#: about to raise. It then raised that finding without the rules that exist to
#: inform it — no error, no mention in the verdict.
#:
#: ripgrep reads flags from the file named by RIPGREP_CONFIG_PATH, so this is
#: a structural fix rather than a prompt asking the model to remember.
RG_CONFIG_NAME = ".sdk-loop-rgcfg"


def write_rg_config(cwd: str) -> str:
    """Write the ripgrep config and return its path."""
    path = os.path.join(cwd, RG_CONFIG_NAME)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("--hidden\n")
    return path


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
    "RIPGREP_CONFIG_PATH",
    "GH_TOKEN",
    "GITHUB_REPOSITORY",
    # The playbook's own variables. Their absence was VISIBLE in a live
    # transcript — the agent's very first shell block printed `REPO env:
    # unset`, `PR_NUMBER env: unset`, `GHA_RUN_URL env: unset` and four more,
    # then spent turns re-deriving each from `git` and `gh` before it could
    # start reviewing. The phase script has all of them; it simply was not
    # handing them on, so every run paid to rediscover its own context.
    "REPO",
    "PR_NUMBER",
    "HEAD_SHA",
    "HEAD_REF",
    "BASE_SHA",
    "GHA_RUN_URL",
    "COMMENTER",
    "COMMENT_ID",
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
    #: The idle watchdog killed this agent. Carried as a FIELD rather than
    #: left as a line in the transcript: `completed` is what the phase
    #: interpreter reads, and a killed agent that reports completed lets the
    #: phase go on to accept whatever verdict comment happens to be newest —
    #: including a prior @sdk-review on the same sha, adopted as its own.
    stalled: bool = False

    @property
    def abort_reason(self) -> str:
        """The agent's own fatal error, if it printed one."""
        if self.stalled:
            return (
                "agent produced no output for the idle timeout and was killed "
                "as stalled — no verdict from this phase is trustworthy"
            )
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


def _opencode_log_dir() -> str:
    return os.path.join(
        os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"),
        "opencode",
        "log",
    )


def _newest_log(since: float) -> str | None:
    """The log opencode opened for THIS run, or None if it has not yet."""
    directory = _opencode_log_dir()
    try:
        candidates = [
            os.path.join(directory, f)
            for f in os.listdir(directory)
            if f.endswith(".log")
        ]
        fresh = [c for c in candidates if os.path.getmtime(c) >= since]
        return max(fresh, key=os.path.getmtime) if fresh else None
    except OSError:
        return None


def _follow_opencode_log(
    stop: threading.Event, emit: Any, since: float, deadline: list[float] | None = None
) -> None:
    """Stream opencode's internal log to the job log WHILE the agent runs.

    This is the only live window into a dispatched sub-agent. opencode's stdout
    prints `• Agent` when one starts and `✓ Agent` when it ends, and nothing in
    between — so a sub-agent that ran 43 minutes and a sub-agent that died
    instantly produce identical output until the very end. Its internal log,
    by contrast, records the stream lifecycle per session id: `stream`,
    `stream error`, retry backoff, tool dispatch. That is what tells a stall
    apart from slow work, and waiting for the artifact means waiting for the
    phase to end before anyone can see why it will not.

    Emitted with an `[oc]` prefix and deliberately NOT appended to the parsed
    transcript. The caller greps that transcript for verdict markers and
    `SDK_LOOP_DISMISSED:` lines; mixing a second stream into it risks changing
    what those parsers see. Routing to the job log only keeps diagnostics and
    control strictly separate.
    """
    handle = None
    try:
        while not stop.is_set():
            if handle is None:
                path = _newest_log(since)
                if path is None:
                    stop.wait(2)
                    continue
                handle = open(path, encoding="utf-8", errors="replace")
            chunk = handle.readline()
            if not chunk:
                stop.wait(1)
                continue
            # This line is ACTIVITY, not just diagnostics. A dispatched
            # sub-agent emits nothing on the parent's stdout for its whole
            # life — the healthy one measured here was silent there for 904s —
            # so an idle watchdog fed only by stdout would kill a working
            # review at the timeout. The internal log is the only progress
            # signal during a dispatch, which makes refreshing the deadline
            # from it the difference between "fires on a stall" and "fires on
            # every sub-agent".
            if deadline is not None:
                deadline[0] = time.monotonic()
            emit(f"[oc] {chunk.rstrip()}")
    except OSError:
        return
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass


def _save_opencode_log(transcript_path: str) -> None:
    """Copy opencode's own log next to the transcript, for the artifact.

    The transcript is only what opencode prints to stdout, and that says
    NOTHING about what happens inside a dispatched sub-agent — the measured
    stall showed one `• agent` line and then 43 minutes of silence, which is
    unactionable. opencode's internal log records the stream lifecycle
    (`stream`, `stream error`, retry backoff, model and session ids) for the
    sub-agent as well as the primary, so it is the only place a stall can be
    told apart from slow work.

    Best-effort throughout: this is diagnostics, and losing it must never fail
    a phase that produced a verdict.
    """
    source = _opencode_log_dir()
    target = f"{os.path.splitext(transcript_path)[0]}-opencode.log"
    try:
        newest = max(
            (os.path.join(source, f) for f in os.listdir(source) if f.endswith(".log")),
            key=os.path.getmtime,
        )
        shutil.copyfile(newest, target)
    except (OSError, ValueError):
        return


def run_agent(
    model: str,
    prompt: str,
    cwd: str,
    timeout_s: int,
    transcript_path: str | None = None,
    sink: Any = None,
    subagents: bool = False,
    idle_timeout_s: int = IDLE_TIMEOUT_S,
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
    os.environ["RIPGREP_CONFIG_PATH"] = write_rg_config(cwd)
    config_path = os.path.join(cwd, "opencode.json")
    with open(config_path, "w", encoding="utf-8") as handle:
        json.dump(opencode_config(model, with_subagents=subagents), handle, indent=2)

    lines: list[str] = []
    # Stamped BEFORE launch so the follower can tell this run's log file from
    # one a previous phase left behind in the same runner image.
    started_at = time.time()
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
        # TWO deadlines, because they catch different deaths.
        #
        # The total one is a backstop. The one that actually matters is the
        # IDLE deadline, and it exists because of a measured failure: a review
        # dispatched its domain sub-agent at 14:32:22 and printed not one
        # further line until the total watchdog killed it at 15:15:24 — 43
        # minutes of runner time bought nothing, and the transcript ended
        # mid-air with no statement of what went wrong. A healthy phase streams
        # a line every ~20s in every run observed, so silence on this scale is
        # not slow work; it is a dead process that nobody has told.
        #
        # Killing on silence rather than on total elapsed turns that 45-minute
        # burn into a ~7-minute failure, and does so without needing to know
        # WHY the agent stalled — which is the point, since that cause is still
        # unproven.
        deadline = [time.monotonic()]
        # TWO events, not one. `stop` means "the watcher may exit"; `stalled`
        # means "the idle deadline actually fired". Conflating them made every
        # clean run append the stall marker, because the reader loop's `finally`
        # sets the stop event on the happy path too — caught by
        # test_the_agent_transcript_streams_rather_than_buffering.
        stop = threading.Event()
        stalled = threading.Event()

        def watch() -> None:
            start = time.monotonic()
            # `stop.wait` returns True once set, which is the loop's exit — so
            # the watcher never needs to poll the process, and a caller's fake
            # process object needs no extra methods to be testable.
            while not stop.wait(10):
                now = time.monotonic()
                if now - deadline[0] > idle_timeout_s:
                    stalled.set()
                    process.kill()
                    return
                if now - start > timeout_s:
                    process.kill()
                    return

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        follower = threading.Thread(
            target=_follow_opencode_log,
            args=(stop, emit, started_at, deadline),
            daemon=True,
        )
        follower.start()
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                deadline[0] = time.monotonic()
                lines.append(line)
                emit(line)
            process.wait()
        finally:
            stop.set()  # release both helper threads immediately
        if stalled.is_set():
            # Say so IN the transcript. A silent death and a killed stall look
            # identical downstream otherwise, and the caller reports whichever
            # it guesses.
            note = (
                f"[sdk-loop] no output for {idle_timeout_s}s — agent killed as "
                "stalled. The phase did not finish; this is not a clean verdict."
            )
            lines.append(note)
            emit(note)
    finally:
        # Never leave the pinned config in a tree the resolve phase might commit.
        for stray in (config_path, os.path.join(cwd, RG_CONFIG_NAME)):
            try:
                os.unlink(stray)
            except FileNotFoundError:
                pass

    if transcript_path:
        _save_opencode_log(transcript_path)

    transcript = "\n".join(lines)
    if transcript_path:
        try:
            with open(transcript_path, "w", encoding="utf-8") as handle:
                handle.write(transcript)
        except OSError:
            # Losing the artifact copy must not fail a phase that otherwise
            # produced a verdict.
            pass
    return AgentResult(
        exit_code=process.returncode,
        stdout=transcript,
        stderr="",
        stalled=stalled.is_set(),
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
