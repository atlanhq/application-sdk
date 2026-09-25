"""One lens run on one PR: admit → scope → (free checks) → review → verify → post.

The round rules that make the loop converge are here, in code:

- **Admission.** lens runs only when asked. An unchanged head is never
  re-reviewed (humans included) unless forced; past `max_rounds`, lens
  stops and says so. New commits are always reviewed below that cap.
- **Incremental.** Round N>1 reviews only `reviewed_head..head` when the new
  head strictly descends from the old one and neither model nor config
  changed; anything else is a full review (open-code-review's fail-closed
  checkpoint).
- **Frozen findings.** Earlier findings are passed back as "do not repeat";
  a new finding in a later round must be at least `later_round_min_severity`.
  Fingerprint dedupe makes a restatement of an old finding a no-op.
- **Free resolution.** A finding whose quoted code no longer exists at the
  head is resolved without a model call. Only findings whose code still
  exists in a file the new commits touched get one verify call.
- **Merge rule.** Blocking = open critical/high findings. Medium/low never block.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import holistic, prompts
from .agent import BundleResult, review_bundle
from .bundle import group
from .config import Config
from .diff import parse_unified_diff, snippet_in_text
from .findings import BLOCKING, SEVERITIES, Finding, PRState, merge_new
from .github import GitHub, GitHubError
from .index import build_index
from .llm import BudgetExhausted, Client, Ledger, LLMError
from .rules import RuleSet
from .select import DEFAULT_EXCLUDE, select_files
from .tools import Workspace, parse_args

SUMMARY_MARKER = "<!-- lens-summary -->"


@dataclass
class RunResult:
    action: str  # reviewed | skipped
    reason: str = ""
    mode: str = ""  # full | incremental
    state: PRState | None = None
    new_findings: list[Finding] = field(default_factory=list)
    unplaced: list[Finding] = field(default_factory=list)
    resolved_free: list[str] = field(default_factory=list)
    resolved_verified: list[str] = field(default_factory=list)
    skipped_files: list[tuple[str, str]] = field(default_factory=list)
    bundles: list[BundleResult] = field(default_factory=list)
    ledger: Ledger | None = None
    incomplete: list[str] = field(
        default_factory=list
    )  # why part of the review did not happen

    @property
    def failed(self) -> bool:
        """A model/transport failure, as opposed to a deliberate budget stop."""
        return any(b.stop == "llm_error" for b in self.bundles)


def _sev_at_least(sev: str, floor: str) -> bool:
    return (
        SEVERITIES.index(sev) <= SEVERITIES.index(floor)
        if floor in SEVERITIES
        else True
    )


def find_state(gh: GitHub, number: int) -> tuple[PRState | None, str]:
    for c in gh.issue_comments(number):
        body = c.get("body") or ""
        if SUMMARY_MARKER in body and (c.get("user") or {}).get("login") in {
            "github-actions[bot]"
        }:
            return PRState.decode(body), body
    return None, ""


def run(
    *,
    gh: GitHub,
    number: int,
    root: Path,
    cfg: Config,
    rules: RuleSet,
    client_factory: Any,
    force: bool = False,
    post: bool = True,
) -> RunResult:
    pr = gh.pr(number)
    head = pr["head"]["sha"]
    base = pr["base"]["sha"]
    state, _ = find_state(gh, number)
    state = state or PRState()

    # ---- admission (0 model calls) --------------------------------------
    same_reviewer = state.model == cfg.model and state.config_hash == cfg.raw_hash
    if state.reviewed_head == head and same_reviewer and not force:
        return RunResult(
            "skipped",
            f"head {head[:8]} already reviewed; comment `@lens force` to re-run",
            state=state,
        )
    if state.round >= cfg.max_rounds and not force:
        return RunResult(
            "skipped",
            f"round cap ({cfg.max_rounds}) reached; comment `@lens force` to review anyway",
            state=state,
        )
    # No "N clean rounds, stop" rule: lens runs only when a human asks, and a
    # request on an unchanged head is already refused above. New commits are
    # always reviewed, however many earlier rounds came back clean.

    ledger = (
        Ledger.from_dict(state.ledger, cfg.cap_usd_per_pr)
        if state.ledger
        else Ledger(cfg.cap_usd_per_pr)
    )
    if ledger.remaining <= 0.01:
        return RunResult(
            "skipped",
            f"PR budget of ${cfg.cap_usd_per_pr:.2f} spent",
            state=state,
            ledger=ledger,
        )

    incremental = (
        bool(state.reviewed_head)
        and same_reviewer
        and gh.compare_status(state.reviewed_head, head) == "ahead"
    )
    mode = "incremental" if incremental else "full"
    range_base = state.reviewed_head if incremental else base
    diff_text = gh.diff(range_base, head)
    all_files = parse_unified_diff(diff_text)
    full_files = (
        all_files if not incremental else parse_unified_diff(gh.diff(base, head))
    )

    round_no = state.round + 1
    res = RunResult("reviewed", mode=mode, state=state, ledger=ledger)

    # ---- head text for changed files (data only; never executed) -----------
    head_text: dict[str, str] = {}
    for fd in full_files[: cfg.max_changed_files]:
        if fd.status == "deleted":
            head_text[fd.path] = ""
        elif not fd.is_binary:
            head_text[fd.path] = gh.file_at(fd.path, head) or ""

    # ---- free resolution: the quoted code is gone ------------------------
    touched = {fd.path for fd in all_files}
    for f in state.open_findings():
        text = head_text.get(f.path)
        if text is None and f.path not in touched:
            continue  # file untouched since: finding still stands
        if not text or not snippet_in_text(text, f.evidence):
            f.status = "fixed"
            res.resolved_free.append(f.id)

    # ---- scope -----------------------------------------------------------
    sel = select_files(all_files, exclude=tuple(DEFAULT_EXCLUDE) + tuple(cfg.exclude))
    res.skipped_files = sel.skipped
    bundles = group(sel.reviewed)
    if len(bundles) > cfg.max_bundles:
        # Largest-change bundles first; the rest are named in the summary, not silently dropped.
        bundles.sort(key=lambda b: -b.changed_lines)
        for b in bundles[cfg.max_bundles :]:
            res.skipped_files.extend((p, "bundle cap") for p in b.paths)
        bundles = bundles[: cfg.max_bundles]

    idx = build_index(root, overrides=head_text)
    ws = Workspace(
        root=root,
        head_text=head_text,
        diffs={fd.path: fd for fd in full_files},
        index=idx,
    )
    round_cap = ledger.remaining * (cfg.first_round_share if round_no == 1 else 1.0)
    round_ledger = Ledger(cap_usd=round_cap)
    client: Client = client_factory(round_ledger)
    pr_meta = {"title": pr.get("title"), "body": pr.get("body")}

    # ---- verify still-open findings in touched files (1 call) -------------
    if cfg.verify and round_no > 1:
        res.resolved_verified = _verify(
            client, ws, [f for f in state.open_findings() if f.path in touched]
        )

    # ---- approach check: once per PR, FIRST --------------------------------
    # It runs before the line review so every bundle reviews against the PR's
    # intent. It is stored in state and reused on every later invocation — a
    # re-review never pays to re-understand the PR (only `@lens force` redoes it).
    if cfg.approach and (not state.approach or force) and sel.reviewed:
        ac = holistic.check(
            client, ws, full_files if incremental else sel.reviewed, pr_meta
        )
        if ac.ran:
            state.approach = holistic.to_state(ac, head)
    pr_meta["understanding"] = holistic.understanding_from_state(state.approach or {})

    # ---- review ----------------------------------------------------------
    confirmed = [f for f in state.findings if f.status == "open"]

    def one(b):  # noqa: ANN001, ANN202
        return review_bundle(
            client, ws, b, rules, pr_meta, confirmed, cfg.limits, reflect=cfg.reflect
        )

    with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:
        res.bundles = list(pool.map(one, bundles))

    fresh: list[Finding] = []
    for br in res.bundles:
        fresh.extend(br.findings)
        res.unplaced.extend(br.unplaced)
    if round_no > 1:
        fresh = [
            f for f in fresh if _sev_at_least(f.severity, cfg.later_round_min_severity)
        ]
        res.unplaced = [
            f
            for f in res.unplaced
            if _sev_at_least(f.severity, cfg.later_round_min_severity)
        ]
    res.new_findings = merge_new(state, fresh, round_no=round_no)

    # ---- state -----------------------------------------------------------
    ledger.spent_usd += round_ledger.spent_usd
    ledger.calls += round_ledger.calls
    ledger.input_tokens += round_ledger.input_tokens
    ledger.cached_tokens += round_ledger.cached_tokens
    ledger.output_tokens += round_ledger.output_tokens
    for k, v in round_ledger.by_stage.items():
        stage = k.split(":", 1)[0]
        ledger.by_stage[stage] = ledger.by_stage.get(stage, 0.0) + v
    for b in res.bundles:
        if b.stop in ("llm_error", "budget"):
            res.incomplete.append(f"{b.label}: {b.stop} — {b.error[:200]}")
    # Spend is always booked. But a round in which part of the review did not
    # happen must never be recorded as a review of this head: that would make
    # the unchanged-head rule skip it and count it as a dry round — a broken
    # alias or a spent key turning into a permanent, silent "all clear".
    if not res.incomplete:
        state.reviewed_head, state.reviewed_base = head, base
        state.model, state.config_hash = cfg.model, cfg.raw_hash
        state.round = round_no
        state.dry_rounds = state.dry_rounds + 1 if not res.new_findings else 0
    state.ledger = ledger.to_dict()
    state.history.append(
        {
            "round": round_no,
            "head": head[:12],
            "mode": mode,
            "new": len(res.new_findings),
            "resolved": len(res.resolved_free) + len(res.resolved_verified),
            "usd": round(round_ledger.spent_usd, 4),
            "calls": round_ledger.calls,
        }
    )

    if post:
        publish(gh, number, head, res)
    return res


def _verify(client: Client, ws: Workspace, open_: list[Finding]) -> list[str]:
    if not open_:
        return []
    items = []
    for f in open_[:20]:
        text = ws.text(f.path) or ""
        lines = text.splitlines()
        sym = ws.index.enclosing(f.path, f.line) if f.line else None
        lo, hi = (
            (sym.start, sym.end)
            if sym and sym.end - sym.start < 120
            else (max(f.line - 15, 1), f.line + 15)
        )
        code = "\n".join(
            f"{i:>5} {lines[i - 1]}" for i in range(lo, min(hi, len(lines)) + 1)
        )
        items.append(
            f'<finding id="{f.id}" path="{f.path}">\n{f.title}. {f.body}\n<code_now>\n{code}\n</code_now>\n</finding>'
        )
    messages = [
        {"role": "system", "content": prompts.VERIFY_SYSTEM},
        {"role": "user", "content": "\n".join(items)},
    ]
    try:
        comp = client.complete(
            "verify",
            messages,
            max_tokens=900,
            tools=prompts.VERIFY_TOOLS,
            tool_choice="required",
            cache_key="lens-verify",
        )
    except (BudgetExhausted, LLMError):
        return []
    fixed: list[str] = []
    by_id = {f.id: f for f in open_}
    for tc in comp.tool_calls:
        for it in (
            parse_args((tc.get("function") or {}).get("arguments") or "").get("items")
            or []
        ):
            f = by_id.get(str(it.get("id")))
            if f and it.get("status") == "fixed":
                f.status = "fixed"
                fixed.append(f.id)
    return fixed


# ---- rendering ---------------------------------------------------------------

_SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}


def inline_body(f: Finding) -> str:
    out = f"{_SEV_ICON[f.severity]} **{f.severity} · {f.category}** — {f.title}\n\n{f.body}"
    if f.suggestion and f.end_line:
        out += f"\n\n```suggestion\n{f.suggestion.rstrip()}\n```"
    return out + f"\n\n<sub>lens {f.id}</sub>"


def render_summary(res: RunResult) -> str:
    st = res.state or PRState()
    blocking = st.open_findings(BLOCKING)
    others = [f for f in st.open_findings() if f.severity not in BLOCKING]
    if res.incomplete:
        verdict = (
            "⚠️ **Review incomplete** — part of this change was not reviewed; this is not an all-clear. "
            "The next push (or `@lens`) retries it.\n\n"
            + "\n".join(f"- {r}" for r in res.incomplete)
        )
    elif blocking:
        verdict = "❌ **Changes requested** — open blocking findings"
    else:
        verdict = "✅ **No blocking findings**"
    lines = [
        SUMMARY_MARKER,
        f"### lens review · round {st.round} ({res.mode})",
        verdict,
        "",
    ]
    ap = st.approach or {}
    if ap.get("problem"):
        # Shown so an author sees at once if the reviewer misread the intent.
        lines.append(
            f"**lens reads this PR as** — {ap['problem']} *How:* {ap.get('approach', '')}\n"
        )
    if ap.get("verdict") == "concerns":
        lines.append(
            "**Approach check** — worth a second look (advisory, does not block):"
        )
        for c in ap.get("concerns", []):
            alt = f" *Instead:* {c['alternative']}" if c.get("alternative") else ""
            lines.append(f"- **{c.get('title', '')}** — {c.get('why', '')}{alt}")
        lines.append("")
    elif ap.get("verdict") == "sound":
        lines.append("**Approach check** — sound.\n")
    if blocking or others:
        lines.append("| id | severity | where | finding |\n|---|---|---|---|")
        for f in blocking + others:
            where = f"`{f.path}:{f.line}`" if f.line else f"`{f.path}`"
            lines.append(f"| {f.id} | {f.severity} | {where} | {f.title} |")
        lines.append("")
    fixed = [f for f in st.findings if f.status == "fixed"]
    if fixed:
        lines.append(f"Resolved: {', '.join(f.id for f in fixed)}")
    if res.unplaced:
        lines.append(
            "\n<details><summary>Findings that could not be anchored to a diff line</summary>\n"
        )
        for f in res.unplaced:
            lines.append(f"- **{f.severity}** `{f.path}` — {f.title}: {f.body}")
        lines.append("\n</details>")
    if res.skipped_files:
        lines.append(
            f"\n<details><summary>{len(res.skipped_files)} file(s) not reviewed</summary>\n"
        )
        lines.extend(f"- `{p}` — {why}" for p, why in res.skipped_files[:50])
        lines.append("\n</details>")
    led = st.ledger
    hit = (
        (led.get("cached_tokens", 0) / led["input_tokens"])
        if led.get("input_tokens")
        else 0.0
    )
    lines.append(
        f"\n<sub>round {st.round} · ${led.get('spent_usd', 0):.3f} of ${led.get('cap_usd', 0):.2f} · "
        f"{led.get('calls', 0)} model calls · {hit:.0%} prompt cache hits · {st.model}</sub>"
    )
    lines.append(st.encode())
    return "\n".join(lines)


def _inline(f: Finding) -> dict[str, Any]:
    c: dict[str, Any] = {
        "path": f.path,
        "line": f.end_line or f.line,
        "side": "RIGHT",
        "body": inline_body(f),
    }
    if f.end_line and f.end_line != f.line:
        c["start_line"], c["start_side"] = f.line, "RIGHT"
    return c


def publish(gh: GitHub, number: int, head: str, res: RunResult) -> None:
    """Inline comments first, then the summary — so a comment GitHub refuses
    (a 422 on a line it does not consider part of the diff) is reported in
    the summary instead of being lost, and one bad line never sinks the rest."""
    body = "lens found issues on this change — see the summary comment."
    todo = list(res.new_findings)
    for i in range(0, len(todo), 50):
        batch = todo[i : i + 50]
        try:
            gh.review(number, head, body, [_inline(f) for f in batch])
        except GitHubError:
            for f in batch:
                try:
                    gh.review(number, head, body, [_inline(f)])
                except GitHubError:
                    res.unplaced.append(f)
    gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))


def to_json(res: RunResult) -> str:
    st = res.state
    return json.dumps(
        {
            "action": res.action,
            "incomplete": res.incomplete,
            "reason": res.reason,
            "mode": res.mode,
            "new": [f.__dict__ for f in res.new_findings],
            "unplaced": [f.__dict__ for f in res.unplaced],
            "resolved_free": res.resolved_free,
            "resolved_verified": res.resolved_verified,
            "skipped_files": res.skipped_files,
            "bundles": [
                {
                    "label": b.label,
                    "turns": b.turns,
                    "tools": b.tool_calls,
                    "stop": b.stop,
                    "error": b.error,
                    "found": len(b.findings),
                    "unplaced": len(b.unplaced),
                    "reflector_removed": len(b.removed_by_reflector),
                }
                for b in res.bundles
            ],
            "ledger": st.ledger if st else {},
        },
        indent=2,
        default=str,
    )
