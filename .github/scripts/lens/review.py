"""One lens run on one PR: admit → scope → (free checks) → review → verify → post.

The round rules that make the loop converge are here, in code:

- **Admission.** lens runs only when asked. An unchanged head is never
  re-reviewed (humans included) unless forced; past `max_rounds`, lens
  stops and says so. New commits are always reviewed below that cap.
- **Incremental.** Round N>1 reviews only `reviewed_head..head` when the new
  head strictly descends from the old one and neither model nor config
  changed; anything else is a full review (a fail-closed checkpoint).
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
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import holistic, prompts, trace
from .agent import BundleResult, review_bundle
from .bundle import Bundle, group
from .config import Config
from .diff import FileDiff, Hunk, Line, parse_unified_diff, snippet_in_text
from .findings import BLOCKING, BODY_KEEP, SEVERITIES, Finding, PRState, merge_new
from .github import GitHub, GitHubError, bot_login
from .index import build_index
from .llm import BudgetExhausted, Client, Ledger, LLMError
from .rules import RuleSet
from .select import DEFAULT_EXCLUDE, select_files
from .tools import Workspace, parse_args
from .triage import Triage, triage

SUMMARY_MARKER = "<!-- lens-summary -->"
COMMENT_LIMIT = 64_000  # GitHub's cap is 65,536 characters; keep a margin
MAX_HISTORY = 20  # rounds kept in the state and shown in the summary


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
    triage: Triage = field(default_factory=Triage)
    retried: list[str] = field(default_factory=list)
    notes: list[str] = field(
        default_factory=list
    )  # advisory run notes (e.g. an API fallback)
    # What kind of run this is, in words, with the reason: "first review",
    # "re-review · only commits since abc1234", "re-review · full, because …", "retry · …".
    mode_label: str = ""
    run_url: str = (
        ""  # the Actions run doing this review (linked from the verdict and history)
    )
    # This run creates the sticky summary, so it is already at the bottom.
    summary_is_new: bool = False
    # Observability: per-request records from the client and per-phase wall time.
    calls: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    head: str = ""
    preflight_error: str = ""
    incomplete: list[str] = field(
        default_factory=list
    )  # why part of the review did not happen

    @property
    def failed(self) -> bool:
        """A model/transport failure, as opposed to a deliberate budget stop."""
        return bool(self.preflight_error) or any(
            b.stop in ("llm_error", "fatal") for b in self.bundles
        )


def describe_mode(
    *,
    mode: str,
    reviewed_head: str,
    pending: int,
    force: bool,
    model_changed: bool,
    config_changed: bool,
    ancestry: str,
) -> str:
    """The kind of run, in plain words, with the reason for a full re-review."""
    if mode == "retry":
        return f"retry · {pending} file(s) left unreviewed last run"
    if not reviewed_head:
        return "first review"
    if mode == "incremental":
        return f"re-review · only commits since {reviewed_head[:7]}"
    if force:
        why = "requested with force"
    elif model_changed:
        why = "the model changed since the last review"
    elif config_changed:
        why = "the lens config changed since the last review"
    elif ancestry and ancestry != "ahead":
        why = "the branch was force-pushed or rebased"
    else:
        why = "the last reviewed commit could not be compared"
    return f"re-review · full, because {why}"


def only_pr_changes(
    incremental: list[FileDiff], pr: list[FileDiff]
) -> tuple[list[FileDiff], list[str], int]:
    """The incremental diff restricted to the PR's own change.

    Both diffs number their RIGHT side against the same PR-head file, so an
    added line in `reviewed_head..head` is the author's only if the PR's
    `base...head` diff adds that line too. A file the PR does not change at
    all was changed only by a merge from the base branch and is dropped; in a
    PR file, a line the merge added becomes plain context; a hunk left with
    no change of the author's is dropped.

    Returns (kept files, dropped paths, count of added lines demoted)."""
    pr_by_path = {fd.path: fd for fd in pr}
    kept: list[FileDiff] = []
    dropped: list[str] = []
    demoted = 0
    for fd in incremental:
        own = pr_by_path.get(fd.path)
        if own is None:
            dropped.append(fd.path)
            continue
        own_added = own.added_lines
        hunks: list[Hunk] = []
        for h in fd.hunks:
            lines: list[Line] = []
            mine = False
            for ln in h.lines:
                if ln.kind == "+" and ln.new_no not in own_added:
                    lines.append(
                        Line(" ", None, ln.new_no, ln.text)
                    )  # the merge added it
                    demoted += 1
                else:
                    lines.append(ln)
                    mine = mine or ln.kind == "+"
            if mine:
                hunks.append(
                    Hunk(
                        h.old_start, h.old_len, h.new_start, h.new_len, h.header, lines
                    )
                )
        if hunks:
            kept.append(
                FileDiff(
                    path=fd.path,
                    old_path=fd.old_path,
                    status=fd.status,
                    is_binary=fd.is_binary,
                    hunks=hunks,
                )
            )
        elif fd.hunks:
            dropped.append(fd.path)
    return kept, dropped, demoted


def _sev_at_least(sev: str, floor: str) -> bool:
    return (
        SEVERITIES.index(sev) <= SEVERITIES.index(floor)
        if floor in SEVERITIES
        else True
    )


def find_state(gh: GitHub, number: int) -> tuple[PRState | None, str]:
    for c in gh.issue_comments(number):
        body = c.get("body") or ""
        # Only the App's own comment is trusted: see github.bot_login().
        if SUMMARY_MARKER in body and (c.get("user") or {}).get("login") == bot_login():
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
    run_url: str = "",
) -> RunResult:
    t_start = time.monotonic()
    pr = gh.pr(number)
    head = pr["head"]["sha"]
    base = pr["base"]["sha"]
    state, prior_summary = find_state(gh, number)
    state = state or PRState()
    with trace.group("1 · admission"):
        trace.line(
            f"PR #{number} head={head[:9]} base={base[:9]} title={trace.short(pr.get('title'), 90)!r}"
        )
        if state.reviewed_head:
            trace.line(
                f"previous state: round {state.round}, reviewed_head={state.reviewed_head[:9]}, "
                f"{len(state.open_findings())} open finding(s), ${float((state.ledger or {}).get('spent_usd', 0)):.4f} spent, "
                f"pending retry: {len(state.pending_files)} file(s)"
            )
        else:
            trace.line("no previous lens state on this PR: first review")
        trace.line(
            f"model={cfg.model} api={cfg.api} effort={cfg.reasoning_effort} config={cfg.raw_hash} force={force}"
        )

    # ---- admission (0 model calls) --------------------------------------
    same_reviewer = state.model == cfg.model and state.config_hash == cfg.raw_hash
    # A head already reviewed is skipped — unless part of it was left unreviewed by a
    # failure, in which case only those files are retried (never the whole PR again).
    retry_only = (
        state.reviewed_head == head
        and same_reviewer
        and not force
        and bool(state.pending_files)
    )
    if state.reviewed_head == head and same_reviewer and not force and not retry_only:
        trace.line(
            "decision: SKIP — this head was already reviewed by the same model and config"
        )
        return RunResult(
            "skipped",
            f"head {head[:8]} already reviewed; comment `/lens force` to re-run",
            state=state,
        )
    if state.round >= cfg.max_rounds and not force:
        trace.line(f"decision: SKIP — round cap {cfg.max_rounds} reached")
        return RunResult(
            "skipped",
            f"round cap ({cfg.max_rounds}) reached; comment `/lens force` to review anyway",
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
        trace.line(f"decision: SKIP — PR budget ${cfg.cap_usd_per_pr:.2f} spent")
        return RunResult(
            "skipped",
            f"PR budget of ${cfg.cap_usd_per_pr:.2f} spent",
            state=state,
            ledger=ledger,
        )

    ancestry = (
        gh.compare_status(state.reviewed_head, head)
        if state.reviewed_head and same_reviewer and not retry_only
        else ""
    )
    incremental = (
        not retry_only
        and bool(state.reviewed_head)
        and same_reviewer
        and ancestry == "ahead"
    )
    mode = "retry" if retry_only else ("incremental" if incremental else "full")
    mode_label = describe_mode(
        mode=mode,
        reviewed_head=state.reviewed_head,
        pending=len(state.pending_files),
        force=force,
        model_changed=bool(state.model) and state.model != cfg.model,
        config_changed=bool(state.config_hash) and state.config_hash != cfg.raw_hash,
        ancestry=ancestry,
    )
    if retry_only:
        full_files = parse_unified_diff(gh.diff(base, head))
        all_files = [fd for fd in full_files if fd.path in set(state.pending_files)]
        range_base = base
    else:
        range_base = state.reviewed_head if incremental else base
        all_files = parse_unified_diff(gh.diff(range_base, head))
        full_files = (
            all_files if not incremental else parse_unified_diff(gh.diff(base, head))
        )
        if incremental:
            # A merge of the base branch into the PR also "descends" from the last
            # reviewed head, so reviewed_head..head carries the base branch's changes
            # too. Keep only what is part of THIS PR's own change.
            all_files, merged_paths, merged_lines = only_pr_changes(
                all_files, full_files
            )
            if merged_paths or merged_lines:
                trace.line(
                    f"excluded changes that came from merging the base branch: "
                    f"{len(merged_paths)} file(s), {merged_lines} line(s)"
                )
        # Files a previous run failed to review ride along with the new commits.
        have = {fd.path for fd in all_files}
        all_files += [
            fd
            for fd in full_files
            if fd.path in set(state.pending_files) and fd.path not in have
        ]

    round_no = state.round + 1
    res = RunResult(
        "reviewed",
        mode=mode,
        state=state,
        ledger=ledger,
        mode_label=mode_label,
        run_url=run_url,
    )
    if post:
        # Pending while it runs: the checks box links straight to the live log.
        gh.set_status(
            head, "pending", f"reviewing · round {round_no} · {mode_label}", run_url
        )
    trace.line(
        f"decision: REVIEW round {round_no} — {mode_label}; range={range_base[:9]}..{head[:9]}, "
        f"{len(all_files)} file(s) in range ({len(full_files)} in the whole PR), budget left ${ledger.remaining:.4f}"
    )

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
            f.status, f.fixed_round, f.fixed_by = "fixed", round_no, "code-gone"
            res.resolved_free.append(f.id)

    if res.resolved_free:
        trace.line(
            f"resolved for free (quoted code gone): {', '.join(res.resolved_free)}"
        )

    # ---- scope -----------------------------------------------------------
    sel = select_files(all_files, exclude=tuple(DEFAULT_EXCLUDE) + tuple(cfg.exclude))
    res.skipped_files = sel.skipped

    # Mechanical change is proven and set aside before any model call (triage.py):
    # a 100-file rename or reformat costs a summary line, not 100 files of review.
    old_text: dict[str, str | None] = {}
    for fd in sel.reviewed[: cfg.max_changed_files]:
        if fd.path.endswith(".py") and fd.status == "modified":
            old_text[fd.path] = gh.file_at(fd.path, range_base)
    res.triage = triage(
        sel.reviewed, old_text, {k: (v or None) for k, v in head_text.items()}
    )

    bundles = plan_bundles(res.triage.reviewed, cfg, res)
    with trace.group("2 · scope: files, triage, bundles"):
        for fd in sel.reviewed:
            trace.line(
                f"selected  {fd.path} ({fd.status}, +{fd.additions}/-{fd.deletions})"
            )
        for path, why in sel.skipped:
            trace.line(f"skipped   {path}: {why}")
        for reason, paths in res.triage.mechanical.items():
            trace.line(
                f"mechanical ({reason}): {', '.join(paths[:10])}{' …' if len(paths) > 10 else ''}"
            )
        for dup, rep_path in res.triage.duplicate_of.items():
            trace.line(f"duplicate {dup}: same change as {rep_path} (reviewed once)")
        if res.triage.renames:
            trace.line(
                "renames: " + ", ".join(f"{a}->{b}" for a, b in res.triage.renames)
            )
        for b in bundles:
            cards = rules.render_for(b.paths).count("<rules card=")
            trace.line(
                f"bundle {b.label!r}: {len(b.files)} file(s), {b.changed_lines} changed lines, "
                f"~{b.diff_tokens()} diff tokens, {cards} rule card(s): {', '.join(b.paths)}"
            )
        if not bundles:
            trace.line("no bundles: nothing substantive to line-review")

    res.head = head
    res.timings_ms["scope"] = int((time.monotonic() - t_start) * 1000)
    t_phase = time.monotonic()
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

    # ---- preflight: zero-token checks before the FIRST request -------------
    # A wrong alias, a rejected key or a spent gateway budget stops the run here,
    # at zero requests, instead of failing every bundle's first call in parallel.
    will_call = (
        bool(res.triage.reviewed)
        or (cfg.approach and not state.approach)
        or (cfg.verify and round_no > 1 and state.open_findings())
    )
    if cfg.preflight and will_call:
        with trace.group("3 · preflight (zero tokens)"):
            reason = client.preflight(min_budget_usd=min(0.05, round_cap))
            for d in getattr(client, "diagnostics", []):
                trace.line(d)
            trace.line(
                f"result: {'STOP — ' + reason if reason else 'ok, the run may start'}"
            )
        if reason:
            # Preflight diagnostics reach the PR only when they stop the run; otherwise
            # they are informational and stay in the job log (the trace above).
            res.notes.extend(getattr(client, "diagnostics", []))
            res.preflight_error = reason
            res.incomplete.append(f"not started: {reason}")
            state.ledger = ledger.to_dict()
            if post:
                url = gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))
                if prior_summary:  # a new summary is already the bottom comment
                    gh.comment(number, verdict_brief(res, url))
                gh.set_status(head, *verdict_status(res), url)
            return res

    # ---- verify still-open findings in touched files (1 call) -------------
    if cfg.verify and round_no > 1:
        to_verify = [f for f in state.open_findings() if f.path in touched]
        with trace.group(f"4 · verify {len(to_verify)} still-open finding(s)"):
            res.resolved_verified = _verify(client, ws, to_verify)
            for f in to_verify:
                if f.id in res.resolved_verified:
                    f.fixed_round, f.fixed_by = round_no, "verified"
            trace.line(f"verified fixed: {', '.join(res.resolved_verified) or 'none'}")

    res.timings_ms["index"] = int((time.monotonic() - t_phase) * 1000)
    t_phase = time.monotonic()
    # ---- approach check: once per PR, FIRST --------------------------------
    # It runs before the line review so every bundle reviews against the PR's
    # intent. It is stored in state and reused on every later invocation — a
    # re-review never pays to re-understand the PR (only `/lens force` redoes it).
    pr_meta["mechanical"] = res.triage.summary_lines() + [
        f"rename: {a} -> {b}" for a, b in res.triage.renames
    ]
    if cfg.approach and (not state.approach or force) and sel.reviewed:
        with trace.group("5 · approach check (once per PR)"):
            ac = holistic.check(
                client, ws, full_files if incremental else sel.reviewed, pr_meta
            )
            if ac.ran:
                state.approach = holistic.to_state(ac, head)
                trace.line(f"problem: {trace.short(ac.problem, 240)}")
                trace.line(f"approach: {trace.short(ac.approach, 240)}")
                trace.line(
                    f"verdict: {ac.verdict} ({len(ac.concerns)} concern(s), {ac.lookups} lookup(s))"
                )
                for c in ac.concerns:
                    trace.line(f"concern: {trace.short(c.get('title'), 120)}")
            else:
                trace.line(f"did not complete: {ac.error or 'no verdict returned'}")
    elif state.approach:
        trace.line("approach check: reused from an earlier round (not re-run)")
    pr_meta["understanding"] = holistic.understanding_from_state(state.approach or {})

    res.timings_ms["approach"] = int((time.monotonic() - t_phase) * 1000)
    t_phase = time.monotonic()
    # ---- review ----------------------------------------------------------
    confirmed = [f for f in state.findings if f.status == "open"]

    # Each bundle may spend its share of what is left (by size of real change),
    # so one large bundle can never starve the others into an incomplete review.
    left = round_ledger.remaining
    weights = {b.label: max(b.changed_lines, 20) for b in bundles}
    total_w = sum(weights.values()) or 1

    def one(b):  # noqa: ANN001, ANN202
        return review_bundle(
            client,
            ws,
            b,
            rules,
            pr_meta,
            confirmed,
            cfg.limits,
            reflect=cfg.reflect,
            budget_usd=left * weights[b.label] / total_w,
        )

    with trace.group(f"6 · line review: {len(bundles)} bundle(s)"):
        trace.line(
            f"up to {cfg.concurrency} bundles in parallel; lines are prefixed [bundle]"
        )
        with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:
            res.bundles = list(pool.map(one, bundles))
    if getattr(client, "fell_back", ""):
        res.notes.append(client.fell_back)

    res.timings_ms["review"] = int((time.monotonic() - t_phase) * 1000)
    res.calls = list(getattr(client, "calls", []))
    fresh: list[Finding] = []
    for br in res.bundles:
        # A finding that is not on a diff line (unchanged code, or a line GitHub will not
        # take a comment on) is still a finding: it is stored, counted and re-verified like
        # any other. Only its delivery differs: the summary carries it, not an inline comment.
        fresh.extend(br.findings)
        fresh.extend(br.unplaced)
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
    res.unplaced = [f for f in res.new_findings if not f.line]
    with trace.group("7 · verdict"):
        for b in res.bundles:
            trace.line(
                f"bundle {b.label!r}: stop={b.stop} turns={b.turns}/{b.turn_budget} "
                f"findings={len(b.findings)} unplaced={len(b.unplaced)} fact-check removed={len(b.removed_by_reflector)}"
                + (f" error={trace.short(b.error, 160)}" if b.error else "")
            )
        for f in res.new_findings:
            trace.line(
                f"NEW {f.id} {f.severity:<8} {f.path}:{f.line} {trace.short(f.title, 100)}"
            )
        if round_no > 1:
            trace.line(
                f"later round: only >= {cfg.later_round_min_severity} findings may be newly raised"
            )
        blocking = state.open_findings(BLOCKING)
        trace.line(
            f"open findings now: {len(state.open_findings())} ({len(blocking)} blocking); "
            f"new this round: {len(res.new_findings)}"
        )

    # ---- state -----------------------------------------------------------
    ledger.spent_usd += round_ledger.spent_usd
    ledger.calls += round_ledger.calls
    ledger.input_tokens += round_ledger.input_tokens
    ledger.cached_tokens += round_ledger.cached_tokens
    ledger.output_tokens += round_ledger.output_tokens
    ledger.failed_requests += round_ledger.failed_requests
    for k, v in round_ledger.by_stage.items():
        stage = k.split(":", 1)[0]
        ledger.by_stage[stage] = ledger.by_stage.get(stage, 0.0) + v
    failed_paths: list[str] = []
    bundle_paths = {b.label: b.paths for b in bundles}
    for b in res.bundles:
        if b.stop in ("llm_error", "fatal", "budget"):
            res.incomplete.append(f"{b.label}: {b.stop} — {b.error[:200]}")
            failed_paths.extend(bundle_paths.get(b.label, []))
    res.retried = list(state.pending_files) if retry_only else []
    # Spend is always booked. But a round in which part of the review did not
    # happen must never be recorded as a review of this head: that would make
    # the unchanged-head rule skip it and count it as a dry round — a broken
    # alias or a spent key turning into a permanent, silent "all clear".
    # What did get reviewed is kept; only the files of failed bundles are
    # remembered as pending, and the next `/lens` retries exactly those.
    if res.bundles and len(failed_paths) == sum(len(b.paths) for b in bundles):
        pass  # nothing was reviewed: leave the head unreviewed so the next run redoes it all
    else:
        state.reviewed_head, state.reviewed_base = head, base
        state.model, state.config_hash = cfg.model, cfg.raw_hash
        state.round = round_no
        state.dry_rounds = state.dry_rounds + 1 if not res.new_findings else 0
        state.pending_files = sorted(set(failed_paths))
    state.ledger = ledger.to_dict()
    state.history.append(
        {
            "round": round_no,
            "head": head[:12],
            "base": range_base[:12],
            "mode": mode,
            "label": mode_label,
            "new": len(res.new_findings),
            "resolved": len(res.resolved_free) + len(res.resolved_verified),
            "blocking": len(state.open_findings(BLOCKING)),
            "incomplete": bool(res.failed or res.incomplete),
            "usd": round(round_ledger.spent_usd, 4),
            "calls": round_ledger.calls,
            "run": run_url,
        }
    )
    del state.history[:-MAX_HISTORY]  # bounded: /lens force can go past max_rounds

    if post:
        t_phase = time.monotonic()
        with trace.group("8 · publish"):
            res.summary_is_new = not prior_summary
            publish(gh, number, head, res)
        res.timings_ms["publish"] = int((time.monotonic() - t_phase) * 1000)
    res.timings_ms["total"] = int((time.monotonic() - t_start) * 1000)
    return res


def dismiss(
    gh: GitHub,
    number: int,
    ids: list[str],
    reason: str,
    *,
    actor: str,
    pr_author: str,
    post: bool = True,
    run_url: str = "",
) -> RunResult:
    """`/lens dismiss F-… <reason>`: close findings the team decided not to fix.

    No model call and no review: the findings are marked dismissed with who and
    why, the summary and the `lens` status are refreshed, and the dismissal is a
    row in the round history. A blocking finding cannot be dismissed by the PR's
    own author — someone else has to agree it can ship."""
    state, _ = find_state(gh, number)
    if state is None:
        return RunResult(
            "skipped", reason="lens has not reviewed this PR yet: nothing to dismiss"
        )
    by_id = {f.id: f for f in state.findings}
    closed: list[str] = []
    refused: list[str] = []
    for fid in ids:
        f = by_id.get(fid)
        if f is None or f.status != "open":
            refused.append(f"`{fid}` is not an open finding")
            continue
        if f.severity in BLOCKING and actor and actor == pr_author:
            refused.append(
                f"`{fid}` is {f.severity}: the PR author can't dismiss a blocking finding — ask a reviewer"
            )
            continue
        f.status, f.fixed_round, f.fixed_by = "wontfix", state.round, "dismissed"
        f.dismissed_by, f.dismiss_reason = actor, reason
        closed.append(fid)
    label = (
        f"dismiss · {', '.join(closed)} by @{actor}"
        if closed
        else "dismiss · nothing closed"
    )
    res = RunResult(
        "dismissed" if closed else "skipped",
        reason="; ".join(refused),
        mode="dismiss",
        mode_label=label,
        state=state,
        run_url=run_url,
    )
    trace.line(
        f"dismiss by @{actor}: closed {closed or 'none'}; refused {refused or 'none'}"
    )
    if closed:
        state.history.append(
            {
                "round": state.round,
                "head": state.reviewed_head[:12],
                "base": "",
                "mode": "dismiss",
                "label": label,
                "new": 0,
                "resolved": len(closed),
                "blocking": len(state.open_findings(BLOCKING)),
                "incomplete": False,
                "usd": 0.0,
                "calls": 0,
                "run": run_url,
            }
        )
        del state.history[:-MAX_HISTORY]
    if not post:
        return res
    head = str(((gh.pr(number) or {}).get("head") or {}).get("sha") or "")
    if head and state.reviewed_head and head != state.reviewed_head:
        res.notes.append(
            "There are commits since the last review; comment `/lens` to review them."
        )
    url = (
        gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res)) if closed else ""
    )
    lines = [f"- ⚠️ {r}" for r in refused]
    if closed:
        brief = verdict_brief(res, url)
        gh.comment(number, "\n".join([brief, "", *lines]) if lines else brief)
        if state.reviewed_head:
            gh.set_status(state.reviewed_head, *verdict_status(res), url)
    else:
        gh.comment(number, "lens: nothing was dismissed.\n\n" + "\n".join(lines))
    return res


RISKY_PREFIXES = (
    "application_sdk/credentials/",
    "application_sdk/storage/",
    "application_sdk/handler/",
    "application_sdk/server/",
    ".github/workflows/",
    ".github/actions/",
)


def _risk(b: Bundle) -> tuple[int, int]:
    risky = any(p.startswith(RISKY_PREFIXES) for p in b.paths)
    return (0 if risky else 1, -b.changed_lines)


def plan_bundles(files: list, cfg: Config, res: RunResult) -> list[Bundle]:  # noqa: ANN001
    """Bundles in review order — riskiest first — within `max_bundles`.

    Past the cap, bundles are first re-packed larger (up to half the context
    ceiling) so every file still gets reviewed; only if that is not enough are
    the lowest-risk bundles set aside, and each skipped file is named in the
    summary. Ordering by risk means that whatever a short budget cuts is the
    least important code, never the credentials path."""
    bundles = group(files)
    if len(bundles) > cfg.max_bundles:
        total = sum(b.diff_tokens() for b in bundles)
        bigger = min(
            max(total // cfg.max_bundles + 1, 9000),
            cfg.limits.context_limit_tokens // 2,
        )
        bundles = group(files, max_diff_tokens=bigger, max_files=12)
    bundles.sort(key=_risk)
    for b in bundles[cfg.max_bundles :]:
        res.skipped_files.extend(
            (p, "bundle cap (lowest-risk code, after re-packing)") for p in b.paths
        )
    return bundles[: cfg.max_bundles]


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
            max_tokens=8000,
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
_SEV_LABEL = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low (nit)",
}


def inline_body(f: Finding) -> str:
    out = f"{_SEV_ICON[f.severity]} **{f.severity} · {f.category}** — {f.title}\n\n{f.body}"
    if f.suggestion and f.end_line:
        out += f"\n\n```suggestion\n{f.suggestion.rstrip()}\n```"
    return out + f"\n\n<sub>lens {f.id}</sub>"


_FIXED_BY = {
    "code-gone": "its code was removed or rewritten",
    "verified": "verified fixed",
}
_CLOSE_HINT = (
    "Fix them and comment `/lens`, or close one the team won't fix with "
    "`/lens dismiss <id> <reason>`."
)


def _how(f: Finding) -> str:
    if f.fixed_by == "dismissed":
        why = f": {f.dismiss_reason}" if f.dismiss_reason else ""
        return f"dismissed by @{f.dismissed_by}{why}".replace("|", "/")
    return _FIXED_BY.get(f.fixed_by, "—")


def _resolved_section(st: PRState) -> list[str]:
    """Resolved findings keep what they were, so editing the summary in place loses nothing."""
    fixed = sorted(
        (f for f in st.findings if f.status in ("fixed", "wontfix")),
        key=lambda f: (f.fixed_round, f.id),
    )
    if not fixed:
        return []
    out = [
        f"\n<details><summary>✔️ Resolved ({len(fixed)})</summary>\n",
        "| id | severity | where | finding | found | resolved | how |",
        "|---|---|---|---|---|---|---|",
    ]
    for f in fixed:
        where = (
            f"`{f.path}:{f.line or f.head_line}`"
            if (f.line or f.head_line)
            else f"`{f.path}`"
        )
        when = f"round {f.fixed_round}" if f.fixed_round else "—"
        out.append(
            f"| {f.id} | {_SEV_ICON[f.severity]} {f.severity} | {where} | {f.title} "
            f"| round {f.round} | {when} | {_how(f)} |"
        )
    out.append("\n</details>")
    return out


def _history_section(st: PRState) -> list[str]:
    """One row per round: what kind of run it was, what it covered, and what changed."""
    rows = [h for h in st.history if h.get("round")]
    if not rows:
        return []
    out = [
        f"\n<details><summary>🕘 Round history ({len(rows)})</summary>\n",
        "| round | run | commits | new | resolved | blocking open | cost | log |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for h in rows:
        span = (
            f"`{h['base'][:7]}..{h['head'][:7]}`"
            if h.get("base")
            else f"`{h.get('head', '')[:7]}`"
        )
        run_kind = h.get("label") or h.get("mode", "")
        if h.get("incomplete"):
            run_kind += " · ⚠️ incomplete"
        log = f"[run]({h['run']})" if h.get("run") else "—"
        out.append(
            f"| {h['round']} | {run_kind} | {span} | {h.get('new', 0)} | {h.get('resolved', 0)} "
            f"| {h.get('blocking', '—')} | ${float(h.get('usd', 0)):.3f} | {log} |"
        )
    out.append("\n</details>")
    return out


def render_summary(res: RunResult) -> str:
    st = res.state or PRState()
    blocking = st.open_findings(BLOCKING)
    by_level = {s: st.open_findings((s,)) for s in SEVERITIES}
    counts = " · ".join(
        f"{_SEV_ICON[s]} {len(by_level[s])} {_SEV_LABEL[s]}" for s in SEVERITIES
    )
    if res.incomplete:
        verdict = (
            "⚠️ **Review incomplete** — part of this change was not reviewed; this is not an all-clear. "
            "Comment `/lens` to retry the part that failed.\n\n"
            + "\n".join(f"- {r}" for r in res.incomplete)
        )
    elif blocking:
        verdict = f"❌ **Changes requested** — {len(blocking)} blocking (critical/high) finding(s) open"
    elif st.open_findings():
        verdict = (
            f"🟡 **Not ready to merge** — {len(st.open_findings())} medium/low finding(s) still open. "
            f"{_CLOSE_HINT}"
        )
    else:
        verdict = "✅ **Ready to merge** — every finding is resolved"
    lines = [
        SUMMARY_MARKER,
        f"### lens · round {st.round} · {res.mode_label or res.mode}",
        verdict,
        "",
        f"**Open findings:** {counts}",
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
    for sev in SEVERITIES:
        items = by_level[sev]
        if not items:
            continue
        blocks = " — blocks merge" if sev in BLOCKING else ""
        lines.append(
            f"#### {_SEV_ICON[sev]} {_SEV_LABEL[sev].capitalize()} ({len(items)}){blocks}"
        )
        lines.append("| id | where | finding |\n|---|---|---|")
        for f in items:
            where = (
                f"`{f.path}:{f.line or f.head_line}`"
                if (f.line or f.head_line)
                else f"`{f.path}`"
            )
            if f.scope == "unchanged":
                where += " (unchanged code: same pattern)"
            elif not f.line:
                where += " (not on a diff line)"
            lines.append(f"| {f.id} | {where} | {f.title} |")
        lines.append("")
    lines.extend(_resolved_section(st))
    lines.extend(_history_section(st))
    if res.unplaced:
        lines.append(
            f"\n<details><summary>{len(res.unplaced)} finding(s) not posted inline — "
            "details (counted above)</summary>\n"
        )
        for f in res.unplaced:
            loc = f"{f.path}:{f.head_line}" if f.head_line else f.path
            lines.append(f"- **{f.severity}** `{loc}` — {f.title}: {f.body}")
        lines.append("\n</details>")
    t = res.triage
    mech_files = {p for ps in t.mechanical.values() for p in ps} | set(t.duplicate_of)
    if mech_files:
        lines.append(
            f"\n<details><summary>{len(mech_files)} file(s) with mechanical changes — "
            "proven behaviour-neutral in code, not sent to the model</summary>\n"
        )
        lines.extend(f"- {line}" for line in t.summary_lines())
        if t.renames:
            lines.append(
                "- renames: " + ", ".join(f"`{a}` → `{b}`" for a, b in t.renames)
            )
        lines.append("\n</details>")
    if st.pending_files:
        lines.append(
            f"\n**{len(st.pending_files)} file(s) could not be reviewed this run** and will be retried "
            "by the next `/lens`: " + ", ".join(f"`{p}`" for p in st.pending_files[:20])
        )
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
    failed = int(led.get("failed_requests", 0))
    for n in res.notes:
        lines.append(f"\n> ℹ️ {n}")
    lines.append(
        f"\n<sub>round {st.round} · ${led.get('spent_usd', 0):.3f} of ${led.get('cap_usd', 0):.2f} · "
        f"{led.get('calls', 0)} model calls · {failed} failed requests · "
        f"{hit:.0%} prompt cache hits · {st.model}</sub>"
    )
    head = "\n".join(lines) + "\n"
    # GitHub refuses a comment over 65,536 characters, and a refused edit would lose
    # the state. Only a very large PR gets here: open findings' stored bodies are then
    # shortened (the verify call still sees the code itself), then the Resolved table
    # is dropped from view. The findings, their ids and the quoted code always stay.
    for keep in (BODY_KEEP, 200, 0):
        body = head + st.encode(body_keep=keep)
        if len(body) <= COMMENT_LIMIT:
            return body
    lean = "\n".join(line for line in _without_resolved(lines)) + "\n"
    return lean + st.encode(body_keep=0)


def _without_resolved(lines: list[str]) -> list[str]:
    out, skipping = [], False
    for line in lines:
        if line.startswith("\n<details><summary>✔️ Resolved"):
            skipping = True
            out.append(
                "\n✔️ Resolved findings are not listed: this PR has too many to fit in one comment."
            )
            continue
        if skipping:
            skipping = line != "\n</details>"
            continue
        out.append(line)
    return out


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


def verdict_brief(res: RunResult, summary_url: str) -> str:
    """This run's verdict, posted at the BOTTOM of the conversation.

    The sticky summary is edited in place, so it stays wherever the PR's first
    review put it — often far above the latest `/lens`. Every run therefore
    also posts this where the requester is looking: the verdict, counts at
    every level, the approach check, cost, and a link to the full summary."""
    st = res.state or PRState()
    by_level = {s: len(st.open_findings((s,))) for s in SEVERITIES}
    counts = " · ".join(
        f"{_SEV_ICON[s]} {by_level[s]} {_SEV_LABEL[s]}" for s in SEVERITIES
    )
    state, description = verdict_status(res)
    icon = {"success": "✅", "failure": "❌", "error": "⚠️"}.get(state, "ℹ️")
    if state == "failure" and not st.open_findings(BLOCKING):
        icon = "🟡"
    lines = [
        f"{icon} **lens · round {st.round} · {res.mode_label or res.mode}** — {description}",
        "",
        f"**Open findings:** {counts}",
    ]
    if res.new_findings:
        inline = sum(1 for f in res.new_findings if f.line)
        where = (
            "inline below"
            if inline == len(res.new_findings)
            else f"{inline} inline, the rest in the summary"
        )
        lines.append(f"**New this round:** {len(res.new_findings)} ({where})")
    if state == "failure" and not st.open_findings(BLOCKING):
        lines.append(_CLOSE_HINT)
    fixed = len(res.resolved_free) + len(res.resolved_verified)
    if fixed:
        lines.append(f"**Resolved this round:** {fixed}")
    ap = st.approach or {}
    if ap.get("verdict"):
        # The holistic review, in full: how lens reads the change, and whether the
        # approach is the right one — not just a one-word verdict.
        lines.append("")
        lines.append(
            f"**Approach check — {'⚠️ concerns (advisory)' if ap['verdict'] == 'concerns' else '✅ sound'}**"
        )
        if ap.get("problem"):
            lines.append(f"- *Problem:* {ap['problem']}")
        if ap.get("approach"):
            lines.append(f"- *How the PR solves it:* {ap['approach']}")
        for c in ap.get("concerns") or []:
            alt = f" *Instead:* {c['alternative']}" if c.get("alternative") else ""
            lines.append(f"- ⚠️ **{c.get('title', '')}** — {c.get('why', '')}{alt}")
        lines.append("")
    led = st.ledger or {}
    lines.append(
        f"<sub>${float(led.get('spent_usd', 0)):.3f} of ${float(led.get('cap_usd', 0)):.2f} · "
        f"{led.get('calls', 0)} model calls · {led.get('failed_requests', 0)} failed requests</sub>"
    )
    links = [f"[Full summary]({summary_url})"] if summary_url else []
    if res.run_url:
        links.append(f"[Run log]({res.run_url})")
    if links:
        lines.append("\n" + " · ".join(links))
    return "\n".join(lines)


def publish(gh: GitHub, number: int, head: str, res: RunResult) -> None:
    """Summary, then inline comments carrying this run's verdict, then status.

    A comment GitHub refuses (a 422 on a line it does not consider part of the
    diff) is moved to the summary instead of being lost, and one bad line never
    sinks the rest. The verdict always lands at the bottom of the conversation:
    as the review body when there are new findings, as a comment otherwise."""
    url = gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))
    # A summary created by this run is already at the bottom and carries the verdict,
    # so the verdict is not repeated under it: no extra comment, and a review with
    # inline comments gets a one-line pointer. Later runs edit the summary in place,
    # far above, so they post the verdict at the bottom again.
    body = (
        f"lens: {len(res.new_findings)} new finding(s) — the verdict is in the summary above."
        if res.summary_is_new
        else verdict_brief(res, url)
    )
    todo = [
        f for f in res.new_findings if f.line
    ]  # the rest are carried by the summary
    posted = False
    refused = False
    for i in range(0, len(todo), 50):
        batch = todo[i : i + 50]
        try:
            gh.review(number, head, body, [_inline(f) for f in batch])
            posted = True
        except GitHubError:
            for f in batch:
                try:
                    gh.review(number, head, body, [_inline(f)])
                    posted = True
                except GitHubError:
                    res.unplaced.append(f)
                    refused = True
    if refused:
        url = gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))
    if not posted and not res.summary_is_new:
        gh.comment(number, verdict_brief(res, url))
    state, description = verdict_status(res)
    gh.set_status(head, state, description, url)
    trace.line(f"sticky summary: {url or '(url unavailable)'}")
    trace.line(
        f"inline comments: {len(todo) - sum(1 for f in res.unplaced if f in todo)} posted"
        + (", some refused by GitHub (moved to the summary)" if refused else "")
        + (
            "; verdict in the new summary"
            if res.summary_is_new
            else "; verdict carried by the review"
            if posted
            else "; verdict posted as a comment"
        )
    )
    trace.line(f"status 'lens' on {head[:9]}: {state} — {description}")


def verdict_status(res: RunResult) -> tuple[str, str]:
    """The `lens` commit status: the one green/red answer for the reviewed head.

    Green means ready to merge: every finding, nits included, is fixed or was
    dismissed with a reason. Any open finding keeps it red; the description says
    whether it is blocking or only medium/low. The approach check is advisory and
    never turns it red. An incomplete review is `error`, never green: part of
    the change was not looked at."""
    st = res.state or PRState()
    led = st.ledger or {}
    cost = f"${float(led.get('spent_usd', 0)):.2f}"
    if res.incomplete:
        return "error", f"review incomplete — {res.incomplete[0][:90]}"
    blocking = st.open_findings(BLOCKING)
    if blocking:
        ids = ", ".join(f.id for f in blocking[:4]) + ("…" if len(blocking) > 4 else "")
        return "failure", f"{len(blocking)} blocking: {ids}"
    minor = st.open_findings()
    if minor:
        ids = ", ".join(f.id for f in minor[:3]) + ("…" if len(minor) > 3 else "")
        return "failure", f"not ready: {len(minor)} medium/low open ({ids})"
    return "success", f"ready to merge — every finding resolved · {cost}"


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
