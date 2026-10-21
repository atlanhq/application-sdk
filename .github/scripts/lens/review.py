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
  exists get one verify call, in a round whose new commits touched a PR
  file or that `/lens force` asked for.
- **Merge rule.** Blocking = open critical/high findings. Medium/low never block.
"""

from __future__ import annotations

import ast
import json
import re
import time
import urllib.parse
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import baseline, holistic, prompts, surface, trace
from .agent import AgentLimits, BundleResult, review_bundle
from .baseline import PRAdditions
from .bundle import Bundle, group
from .config import Config
from .diff import (
    FileDiff,
    Hunk,
    Line,
    locate_in_text,
    parse_unified_diff,
    snippet_in_text,
)
from .findings import BLOCKING, BODY_KEEP, SEVERITIES, Finding, PRState, merge_new
from .github import GitHub, bot_login
from .index import Index, Symbol, build_index, index_source
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
    # Open items (F-… and A…) sent to a verify call that spent its whole output
    # budget before answering: not re-judged this round, so not a verdict on them.
    verify_cut_off: list[str] = field(default_factory=list)
    verify_max_tokens: int = 0  # the budget that verify call had
    verify_opt_in_tokens: int = 0  # what `/lens verify-budget` would give it
    verify_opted_in: bool = False
    # Open items (F-… and A…) not re-checked because this round changed none of the
    # PR's own files (e.g. only a merge from the base branch): no verify call ran.
    verify_skipped: list[str] = field(default_factory=list)
    # New blocking findings this round's base-branch check judged fixed or moot:
    # dropped before publishing (see _check_new_against_base).
    new_dropped: list[str] = field(default_factory=list)
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
    # Files drawing new blocking findings round after round (see spiral_paths).
    spiral: list[tuple[str, list[int]]] = field(default_factory=list)
    run_url: str = (
        ""  # the Actions run doing this review (linked from the verdict and history)
    )
    # This run creates the sticky summary, so it is already at the bottom.
    summary_is_new: bool = False
    blob_base: str = (
        ""  # https://github.com/<repo>/blob/<head>: finding locations link here
    )
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


def head_side_text(
    gh: GitHub, files: list[FileDiff], head: str, max_files: int
) -> dict[str, str]:
    """The PR-head text of each changed file, "" for a path the PR removes.

    The checkout is the base branch, and this map overlays it (reads, file
    listing, search, the index), so anything missing here reads as the base
    branch's copy. A renamed file's old path is removed by the PR just like a
    deletion. Without it the old file stays visible, and a finding that says
    "this file is still here" can never be verified fixed. Old paths cost no
    request, so every rename in the diff is covered, not only the first
    `max_files` changed files. An old path the PR puts a file back at is never
    marked removed, wherever that file sits in the diff: inside the cap it has
    its head text, and past it it reads as any uncapped file does.
    """
    head_text: dict[str, str] = {}
    for fd in files[:max_files]:
        if fd.status == "deleted":
            head_text[fd.path] = ""
        elif not fd.is_binary:
            head_text[fd.path] = gh.file_at(fd.path, head) or ""
    for fd in renamed_away(files):
        head_text[str(fd.old_path)] = ""
    return head_text


def renamed_away(files: list[FileDiff]) -> list[FileDiff]:
    """The renames whose old path no file occupies at the PR head.

    A path the PR puts a file back at (anywhere in the diff) is not removed,
    so it is left out."""
    at_head = {fd.path for fd in files if fd.status != "deleted"}
    return [
        fd
        for fd in files
        if fd.status == "renamed"
        and fd.old_path
        and fd.old_path != fd.path
        and fd.old_path not in at_head
    ]


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
    verify_budget: bool = False,
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
    # Only a different MODEL is a different reviewer. A lens config change (cards,
    # prompts, limits) does not re-open code an earlier round already passed: that
    # moved the goalposts on reviewed code. It applies from the next new commits.
    # `/lens force` lifts the skip and round-cap rules: after new commits it
    # reviews just those (the cheap way to get an approval back after a small push);
    # on an unchanged head it re-reviews the whole PR under the current config.
    # Either way it re-checks every open finding, even when no PR file changed.
    same_reviewer = state.model == cfg.model
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
        blob_base=f"https://github.com/{gh.repo}/blob/{head}",
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
    head_text = head_side_text(gh, full_files, head, cfg.max_changed_files)

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
        or (
            cfg.verify
            and round_no > 1
            and (state.open_findings() or open_concerns(state))
        )
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

    # ---- the last release's public names this PR takes away ---------------
    # Once per round, shared by the verify call and every bundle: a removal is a
    # break only if the name shipped, which neither can tell from a diff.
    with trace.group("3b · released surface (last release vs PR head)"):
        released = surface.released_surface_removals(
            root, full_files, head_text, lambda p: gh.file_at(p, head)
        )
        trace.line(
            released.replace("\n", "; ")[:400]
            if released
            else "skipped: the PR changes no package module"
        )
    pr_meta["released_surface_removals"] = released

    # ---- what this PR itself added, against the merge base (FND-4163) -------
    # An incremental round sees only the commits since the last review, so code
    # an earlier commit of this PR added reads as pre-existing. A compatibility
    # claim about it is capped in code (agent.calibrate); both prompts are told.
    with trace.group("3c · added by this PR (merge base vs PR head)"):
        added = pr_added(gh, base, head, full_files, head_text)
        trace.line(
            f"{len(added.paths)} added path(s), "
            f"{sum(len(v) for v in added.symbols.values())} added symbol(s) in existing files"
        )
    pr_meta["paths_added"] = added.render_paths()
    pr_meta["symbols_added"] = added.render_symbols()

    # ---- verify still-open findings (1 call) -------------------------------
    # Every open finding, not only those whose own file changed: a fix often lands
    # in another file (a data file's finding fixed in the code that reads it). The
    # model also sees what changed this round, since the fix may not be at the quote.
    if cfg.verify and round_no > 1:
        # A round that changed no PR file (a merge from the base branch alone) has
        # nothing new to judge, so it skips verify — unless `/lens force` asked for a
        # re-check, which then judges the open items against the whole PR's change.
        recheck = bool(touched) or force
        verify_files = all_files if touched else full_files
        if not recheck:
            res.verify_skipped = [f.id for f in state.open_findings()] + [
                cid for cid, _ in open_concerns(state)
            ]
            if res.verify_skipped:
                trace.line(
                    "verify: skipped — no PR file changed this round, so "
                    f"{', '.join(res.verify_skipped)} were not re-checked"
                )
        to_verify = state.open_findings() if recheck else []
        # The approach check runs once per PR, so without this a concern the author
        # has since addressed would stay on the PR forever. It rides the same call.
        concerns = open_concerns(state) if recheck else []
        res.verify_opted_in = verify_budget
        res.verify_opt_in_tokens = cfg.limits.verify_max_tokens_opt_in
        res.verify_max_tokens = (
            res.verify_opt_in_tokens if verify_budget else cfg.limits.verify_max_tokens
        )
        with trace.group(
            f"4 · verify {len(to_verify)} still-open finding(s), {len(concerns)} approach concern(s)"
            f", output budget {res.verify_max_tokens}"
        ):
            verified = (
                _verify(
                    client,
                    ws,
                    to_verify,
                    round_diff(verify_files, to_verify),
                    concerns,
                    removed=removed_paths(full_files),
                    released=released,
                    added=added,
                    max_tokens=res.verify_max_tokens,
                )
                if to_verify or concerns
                else Verified()
            )
            fixed = verified.fixed
            res.verify_cut_off = verified.cut_off
            if verified.cut_off:
                trace.line(
                    f"WARNING: verify spent its whole output budget ({res.verify_max_tokens}) "
                    f"before answering for {', '.join(verified.cut_off)}: kept as they were, "
                    "not re-judged"
                )
            res.resolved_verified = [i for i in fixed if not _CONCERN_ID.match(i)]
            for f in to_verify:
                if f.id in res.resolved_verified:
                    f.fixed_round = round_no
                    f.fixed_by = "pr-history" if f.id in verified.moot else "verified"
            addressed = {i for i in fixed if _CONCERN_ID.match(i)}
            for cid, c in concerns:
                if cid in addressed:
                    c["status"], c["addressed_round"] = "addressed", round_no
            trace.line(
                f"verified fixed: {', '.join(res.resolved_verified) or 'none'}; "
                f"concerns addressed: {', '.join(sorted(addressed)) or 'none'}"
            )

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
            added=added,
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
        # A finding that is not on a diff line (unchanged code) is still a finding: it is
        # stored, counted and re-verified like any other. Every finding is delivered in the
        # summary and the verdict; lens opens no inline review threads.
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
    if cfg.verify and round_no > 1:
        fresh = _check_new_against_base(
            client,
            ws,
            state,
            fresh,
            res,
            changes=all_files if touched else full_files,
            removed=removed_paths(full_files),
            released=released,
            added=added,
            max_tokens=res.verify_max_tokens or cfg.limits.verify_max_tokens,
        )
        res.calls = list(getattr(client, "calls", []))  # the check's call too
    res.new_findings = merge_new(state, fresh, round_no=round_no)
    res.unplaced = [f for f in res.new_findings if not f.line]
    res.spiral = spiral_paths(state, res.new_findings, round_no)
    for path, rounds in res.spiral:
        trace.line(f"spiral: {path} drew new findings in rounds {rounds}")
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
    _book(ledger, round_ledger)
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
        blob_base=(
            f"https://github.com/{gh.repo}/blob/{state.reviewed_head}"
            if state.reviewed_head
            else ""
        ),
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


def _check_new_against_base(
    client: Client,
    ws: Workspace,
    state: PRState,
    fresh: list[Finding],
    res: RunResult,
    *,
    changes: list[FileDiff],
    removed: str,
    released: str,
    added: PRAdditions,
    max_tokens: int,
) -> list[Finding]:
    """`fresh` without the new blocking findings that are moot or fixed against
    the base branch.

    An incremental round's line review sees only the commits since the last
    review, so its "before" is this PR's own previous commit: a round that puts
    base-branch behaviour back reads as a regression. Before such a finding is
    published (and blocks merge), it gets the verify call, with <at_base_branch>
    and the moot rule. Only a round with a new blocking finding pays for it:
    one call per VERIFY_BATCH findings. A finding no verdict came back for
    publishes as raised, and the summary names it."""
    known = {f.id for f in state.findings}
    new = [f for f in fresh if f.id not in known and f.severity in BLOCKING]
    if not new:
        return fresh
    drop: set[str] = set()
    moot: set[str] = set()
    unchecked: dict[str, list[str]] = {}  # why -> ids that got no verdict
    with trace.group(f"6b · base-branch check of {len(new)} new blocking finding(s)"):
        for i in range(0, len(new), VERIFY_BATCH):
            batch = new[i : i + VERIFY_BATCH]
            got = _verify_batch(
                client,
                ws,
                batch,
                round_diff(changes, batch),
                removed=removed,
                released=released,
                added=added,
                max_tokens=max_tokens,
                new=True,
            )
            drop |= set(got.fixed)
            moot |= set(got.moot)
            why = (
                "the PR budget ran out"
                if got.error == "budget"
                else "the model call failed"
                if got.error
                else "the check spent its whole output budget"
                if got.cut_off
                else "no verdict came back for it"
            )
            # A finding the reply did not judge (an empty or partial reply, as
            # much as a failed call) was not checked: it publishes as raised.
            for f in batch:
                if f.id not in got.judged:
                    unchecked.setdefault(why, []).append(f.id)
        for f in new:
            if f.id in drop:
                how = "moot" if f.id in moot else "fixed"
                trace.line(
                    f"dropped {f.id} {f.severity} {f.path}: judged {how} against the base branch"
                )
        for why, ids in unchecked.items():
            res.notes.append(
                f"New blocking finding(s) {', '.join(ids)} were published without "
                f"the base-branch check: {why}."
            )
            trace.line(f"not checked ({why}): {', '.join(ids)}")
        res.new_dropped = [f.id for f in new if f.id in drop]
        if not drop and not unchecked:
            trace.line("every new blocking finding stands")
    return [f for f in fresh if f.id not in drop]


def _book(ledger: Ledger, spent: Ledger) -> None:
    """Add one run's spend to the PR's ledger."""
    ledger.spent_usd += spent.spent_usd
    ledger.calls += spent.calls
    ledger.input_tokens += spent.input_tokens
    ledger.cached_tokens += spent.cached_tokens
    ledger.output_tokens += spent.output_tokens
    ledger.failed_requests += spent.failed_requests
    for k, v in spent.by_stage.items():
        stage = k.split(":", 1)[0]
        ledger.by_stage[stage] = ledger.by_stage.get(stage, 0.0) + v


# ---- /lens recheck -------------------------------------------------------------

CAPABILITIES_DOC = "docs/agents/sdk-capabilities.md"
RECHECK_REASON_KEEP = 300


@dataclass
class RecheckEvidence:
    facts: list[str]  # deterministic, one line each; facts[0] is the merge-base fact
    absent_on_merge_base: bool
    code_now: str
    at_merge_base: str


def _private(parts: list[str]) -> bool:
    return any(p.startswith("_") and not p.startswith("__") for p in parts)


def recheck_evidence(
    ws: Workspace,
    f: Finding,
    added: PRAdditions,
    merge_base_text: Callable[[str], str | None],
    capabilities: str,
) -> RecheckEvidence:
    """What git and the repository say about one finding's subject, with no
    model call: merge-base existence, callers, privacy, the capability manifest."""
    text = ws.text(f.path) or ""
    lines = text.splitlines()
    at = locate_in_text(text, f.evidence)
    sym = ws.index.enclosing(f.path, at) if at else None
    lo, hi = (
        (sym.start, sym.end)
        if sym and sym.end - sym.start < 120
        else (max(at - 15, 1), at + 15)
    )
    code_now = (
        "\n".join(f"{i:>5} {lines[i - 1]}" for i in range(lo, min(hi, len(lines)) + 1))
        if at
        else "(the quoted code is not in this file any more)"
    )
    fd = ws.diffs.get(f.path)
    base_path = fd.old_path if fd and fd.status == "renamed" and fd.old_path else f.path
    dotted = sym.qualname.split(":", 1)[1] if sym else ""
    facts: list[str] = []
    absent = False
    at_base = ""
    why = added.added(f.path, sym)
    if why:
        facts.append(why)
        absent = True
    else:
        base = merge_base_text(base_path)
        if base is None:
            facts.append(f"merge-base copy of {base_path} could not be read")
        elif (names := baseline.dotted_names(base_path, base)) is None:
            # Unknown is not absent: a copy that does not parse proves nothing.
            facts.append(f"merge-base copy of {base_path} could not be parsed")
        elif sym is not None:
            if dotted in names:
                facts.append(f"present on merge base: {dotted} in {base_path}")
            else:
                facts.append(f"absent on merge base: {dotted} in {base_path}")
                absent = True
        elif not at:
            # The quote is gone from the head, so its enclosing symbol is unknown:
            # its absence from the merge base says nothing about that symbol.
            facts.append(
                f"the quoted code is no longer in {f.path} at the PR head: "
                "merge-base presence of its symbol unknown"
            )
        elif snippet_in_text(base, f.evidence):
            facts.append(f"present on merge base: the quoted code is in {base_path}")
        else:
            facts.append(f"absent on merge base: the quoted code is not in {base_path}")
            absent = True
        if base is not None:
            base_at = locate_in_text(base, f.evidence)
            if base_at:
                at_base = _numbered(base, base_at - 15, base_at + 15)
            elif sym is not None:
                # The quote is new in the PR, but the symbol around it may have
                # shipped: show that symbol as the merge base has it, so a claim
                # that the PR changed its behaviour can be checked against it.
                got = base_symbol_excerpt(ws.root, base_path, base, f, sym, at)
                at_base = f"{base_path}: {dotted}\n{got}" if got else ""
    module = f.path[:-3].replace("/", ".") if f.path.endswith(".py") else f.path
    private = _private(f.path.removesuffix(".py").split("/")) or _private(
        dotted.split(".") if dotted else []
    )
    facts.append(
        f"{'private' if private else 'public'}: module {module}"
        + (f", symbol {dotted}" if dotted else "")
    )
    listed = module in capabilities or (
        bool(sym) and f"{module}.{sym.name}" in capabilities
    )
    facts.append(
        f"{'listed' if listed else 'not listed'} in {CAPABILITIES_DOC}"
        if capabilities
        else f"{CAPABILITIES_DOC} not found on the base branch"
    )
    if sym is not None:
        callers = [
            c
            for c in ws.index.callers_of(sym.name, limit=8)
            if c.qualname != sym.qualname
        ]
        total = len(ws.index.callers.get(sym.name, []))
        facts.append(
            f"callers of {sym.name} at the PR head ({total} by name): "
            + (
                ", ".join(
                    f"{c.path}:{c.start} {c.qualname.split(':')[-1]}"
                    + (" (added by this PR)" if added.added(c.path, c) else "")
                    for c in callers
                )
                or "none"
            )
        )
    return RecheckEvidence(facts, absent, code_now, at_base or "(none)")


# The tags a recheck request is built from. PR-derived text (code, quotes,
# finding prose, paths) must not be able to open or close one of them.
_RECHECK_TAGS = re.compile(
    r"<(/?)(finding|quoted_when_raised|code_now|at_merge_base|evidence|author_claim)\b",
    re.IGNORECASE,
)


def _inert(text: str) -> str:
    """`text` with every recheck tag delimiter made literal: `</code_now>` in a
    PR's source reads as `‹/code_now>`, so it cannot end the block it sits in or
    forge an <evidence> block. Other `<` (comparisons, generics) are untouched."""
    return _RECHECK_TAGS.sub(lambda m: f"‹{m.group(1)}{m.group(2)}", text)


def _recheck_call(
    client: Client,
    items: list[tuple[Finding, RecheckEvidence]],
    author_claim: str,
    max_tokens: int,
) -> dict[str, tuple[str, str]] | None:
    """One model call over every finding that needs one: id -> (status, reason).
    None when no verdict came back (budget, transport, or out of output tokens)."""
    parts = []
    if author_claim:
        # Untrusted: shown as a claim to check, and unable to close or open a tag.
        claim = author_claim.replace("<", "‹").replace(">", "›")
        parts.append(f"<author_claim>\n{claim}\n</author_claim>")
    for f, ev in items:
        path = _inert(f.path).replace('"', "'")
        parts.append(
            f'<finding id="{f.id}" path="{path}" severity="{f.severity}">\n'
            f"{_inert(f.title)}. {_inert(f.body)}\n"
            f"<quoted_when_raised>\n{_inert(f.evidence)}\n</quoted_when_raised>\n"
            f"<code_now>\n{_inert(ev.code_now)}\n</code_now>\n"
            f"<at_merge_base>\n{_inert(ev.at_merge_base)}\n</at_merge_base>\n"
            "<evidence>\n"
            + "\n".join(f"- {_inert(x)}" for x in ev.facts)
            + "\n</evidence>\n</finding>"
        )
    messages = [
        {"role": "system", "content": prompts.RECHECK_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]
    try:
        comp = client.complete(
            "recheck",
            messages,
            max_tokens=max_tokens,
            tools=prompts.RECHECK_TOOLS,
            tool_choice="required",
            cache_key="lens-recheck",
        )
    except (BudgetExhausted, LLMError) as e:
        trace.line(f"recheck: no verdict — {trace.short(str(e), 200)}")
        return None
    if not comp.tool_calls:
        return None
    ids = {f.id for f, _ in items}
    out: dict[str, tuple[str, str]] = {}
    for it in verdict_items(comp.tool_calls):
        iid = str(it.get("id"))
        if iid in ids and it.get("status") in ("resolved", "open"):
            out[iid] = (
                str(it["status"]),
                " ".join(str(it.get("reason") or "").split()),
            )
    return out


def recheck(
    *,
    gh: GitHub,
    number: int,
    ids: list[str],
    reason: str,
    root: Path,
    cfg: Config,
    client_factory: Any,
    actor: str,
    post: bool = True,
    run_url: str = "",
) -> RunResult:
    """`/lens recheck F-… [reason]`: re-judge open findings with repo-aware evidence.

    Deterministic first: the evidence (merge-base existence, callers, privacy,
    the capability manifest) is gathered with no model call, and a compatibility
    finding whose subject is absent on the merge base is resolved on that alone.
    Anything else gets one model call, from what is left of the PR's $ cap.
    It is not a round: `state.round` and `reviewed_head` do not move, so the
    round cap never blocks it and the next `/lens` still reviews any new commits.
    Who may ask is decided in event.py; the PR author may."""
    state, _ = find_state(gh, number)
    if state is None:
        return RunResult(
            "skipped", reason="lens has not reviewed this PR yet: nothing to re-check"
        )
    by_id = {f.id: f for f in state.findings}
    targets: list[Finding] = []
    refused: list[str] = []
    for fid in dict.fromkeys(ids):
        f = by_id.get(fid)
        if f is None or f.status != "open":
            refused.append(f"`{fid}` is not an open finding")
        else:
            targets.append(f)
    ledger = (
        Ledger.from_dict(state.ledger, cfg.cap_usd_per_pr)
        if state.ledger
        else Ledger(cfg.cap_usd_per_pr)
    )
    res = RunResult(
        "skipped",
        mode="recheck",
        state=state,
        ledger=ledger,
        run_url=run_url,
        blob_base=(
            f"https://github.com/{gh.repo}/blob/{state.reviewed_head}"
            if state.reviewed_head
            else ""
        ),
    )
    resolved: list[tuple[Finding, str]] = []
    kept: list[tuple[Finding, str, list[str]]] = []
    spent = Ledger(cap_usd=max(ledger.remaining, 0.0))
    if targets:
        pr = gh.pr(number)
        head, base = pr["head"]["sha"], pr["base"]["sha"]
        full_files = parse_unified_diff(gh.diff(base, head))
        head_text = head_side_text(gh, full_files, head, cfg.max_changed_files)
        mb = gh.merge_base(base, head)

        def read_base(path: str) -> str | None:
            return gh.file_at(path, mb) if mb else None

        added = baseline.pr_additions(
            full_files, head_reader(gh, head, head_text, full_files), read_base
        )
        ws = Workspace(
            root=root,
            head_text=head_text,
            diffs={fd.path: fd for fd in full_files},
            index=build_index(root, overrides=head_text),
        )
        caps = root / CAPABILITIES_DOC
        capabilities = caps.read_text(encoding="utf-8") if caps.is_file() else ""
        ask: list[tuple[Finding, RecheckEvidence]] = []
        for f in targets:
            ev = recheck_evidence(ws, f, added, read_base, capabilities)
            trace.line(f"recheck {f.id}: " + "; ".join(ev.facts))
            if f.compatibility and ev.absent_on_merge_base:
                resolved.append(
                    (f, f"{ev.facts[0]}: a compatibility claim about code this PR adds")
                )
            else:
                ask.append((f, ev))
        if ask and ledger.remaining <= 0.01:
            refused += [
                f"`{f.id}` was not re-checked: the PR budget of ${cfg.cap_usd_per_pr:.2f} is spent"
                for f, _ in ask
            ]
        elif ask:
            client: Client = client_factory(spent)
            verdicts = _recheck_call(client, ask, reason, cfg.limits.verify_max_tokens)
            if verdicts is None:
                res.incomplete.append("re-check: no verdict came back")
            for f, ev in ask:
                status, why = (verdicts or {}).get(f.id, ("", ""))
                if status == "resolved":
                    resolved.append(
                        (f, f"{ev.facts[0]} — {why}" if why else ev.facts[0])
                    )
                else:
                    kept.append((f, why or "no verdict came back", ev.facts))
            _book(ledger, spent)
    for f, why in resolved:
        f.status, f.fixed_round, f.fixed_by = "fixed", state.round, "rechecked"
        f.dismissed_by, f.dismiss_reason = actor, why[:RECHECK_REASON_KEEP]
    closed = [f.id for f, _ in resolved]
    res.action = "rechecked" if targets else "skipped"
    res.reason = "; ".join(refused)
    res.resolved_verified = closed
    res.mode_label = (
        f"recheck · {', '.join(f.id for f in targets) or 'nothing'} by @{actor}"
    )
    state.ledger = ledger.to_dict()
    if targets:
        state.history.append(
            {
                "round": state.round,
                "head": state.reviewed_head[:12],
                "base": "",
                "mode": "recheck",
                "label": res.mode_label,
                "new": 0,
                "resolved": len(closed),
                "blocking": len(state.open_findings(BLOCKING)),
                "incomplete": bool(res.incomplete),
                "usd": round(spent.spent_usd, 4),
                "calls": spent.calls,
                "run": run_url,
            }
        )
        del state.history[:-MAX_HISTORY]
    trace.line(
        f"recheck by @{actor}: resolved {closed or 'none'}; kept {[f.id for f, *_ in kept] or 'none'}; "
        f"refused {refused or 'none'}; ${spent.spent_usd:.4f}"
    )
    if not post:
        return res
    lines = [f"- ✅ `{f.id}` resolved: {why}" for f, why in resolved]
    for f, why, facts in kept:
        lines.append(f"- ❌ `{f.id}` still stands: {why}")
        lines += [f"  - {x}" for x in facts]
    lines += [f"- ⚠️ {r}" for r in refused]
    if not targets:
        gh.comment(number, "lens: nothing was re-checked.\n\n" + "\n".join(lines))
        return res
    url = gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))
    gh.comment(number, "\n".join([verdict_brief(res, url), "", *lines]))
    if state.reviewed_head:
        gh.set_status(state.reviewed_head, *verdict_status(res), url)
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


ROUND_DIFF_CHARS = 12_000  # this round's changes shown to the verify call


def removed_paths(files: list[FileDiff]) -> str:
    """Every path the whole PR deletes or renames away, one per line, for the
    verify call.

    Verify sees the site of each finding and this round's diff, never the
    repository, so a finding that says another file "is still present" could
    not be judged at all and stayed open for good. The list is the whole PR's,
    not this round's: a rename made in an earlier round is still in effect. A
    path the PR puts a file back at is not removed and is left out
    (`renamed_away`, the same rule the head-text overlay uses).
    """
    lines = [f"deleted: {fd.path}" for fd in files if fd.status == "deleted"]
    lines += [f"renamed: {fd.old_path} -> {fd.path}" for fd in renamed_away(files)]
    return "\n".join(sorted(lines, key=lambda line: line.split(": ", 1)[1]))


def pr_added(
    gh: GitHub,
    base: str,
    head: str,
    full_files: list[FileDiff],
    head_text: dict[str, str],
) -> PRAdditions:
    """`baseline.pr_additions` with the merge-base copies read through the API.

    No merge base, no symbols: it fails closed (added paths still come from the
    PR's diff, which GitHub computes against the merge base)."""
    mb = gh.merge_base(base, head)

    def read_base(path: str) -> str | None:
        return gh.file_at(path, mb) if mb else None

    return baseline.pr_additions(
        full_files, head_reader(gh, head, head_text, full_files), read_base
    )


BASELINE_EXTRA_FILES = 200  # head reads past `max_changed_files`, for the baseline only


def head_reader(
    gh: GitHub, head: str, head_text: dict[str, str], full_files: list[FileDiff]
) -> Callable[[str], str | None]:
    """PR-head text for the baseline: the overlay first, then the API.

    The overlay holds only the first `max_changed_files` files, and a Python
    file past it would otherwise drop out of the baseline, so a compatibility
    finding on a symbol it adds could still block. Those files are read here,
    up to BASELINE_EXTRA_FILES; past that the rest fail closed (judged as before)
    and the trace says so."""
    extra = [
        fd.path
        for fd in full_files
        if fd.path.endswith(".py")
        and fd.status in ("modified", "renamed")
        and fd.path not in head_text
    ]
    allowed = set(extra[:BASELINE_EXTRA_FILES])
    if len(extra) > len(allowed):
        trace.line(
            f"baseline: {len(extra) - len(allowed)} changed Python file(s) past "
            f"{BASELINE_EXTRA_FILES} extra reads were left out (judged as before)"
        )

    def read(path: str) -> str | None:
        if path in head_text:
            return head_text[path] or None
        return gh.file_at(path, head) if path in allowed else None

    return read


def added_blocks(added: PRAdditions | None) -> list[str]:
    if added is None:
        return []
    out = []
    if added.paths:
        out.append(
            f"<paths_added_by_this_pr>\n{added.render_paths()}\n</paths_added_by_this_pr>"
        )
    if added.symbols:
        out.append(
            f"<symbols_added_by_this_pr>\n{added.render_symbols()}\n</symbols_added_by_this_pr>"
        )
    return out


def round_diff(files: list[FileDiff], open_: list[Finding]) -> str:
    """What changed this round, for the verify call: files holding an open finding
    first, then the rest, within a fixed budget (a cached, bounded prompt)."""
    owners = {f.path for f in open_}
    out, used = [], 0
    for fd in sorted(files, key=lambda d: (d.path not in owners, d.path)):
        if fd.is_binary or not fd.hunks:
            continue
        block = f"--- {fd.path}\n{fd.render(max_lines=200)}"
        if used + len(block) > ROUND_DIFF_CHARS:
            out.append(f"--- {fd.path} (diff omitted: budget)")
            continue
        out.append(block)
        used += len(block)
    return "\n".join(out)


def _current_line(ws: Workspace, f: Finding, text: str) -> int:
    """Where the finding's quoted code is NOW. Stored line numbers are from an
    earlier head and drift as commits land; the quote is what identifies the site,
    so when it is gone the model is told so instead of shown unrelated code."""
    return locate_in_text(text, f.evidence)  # 0 = the quote is gone; never guess a line


BASE_EXCERPT_LINES = 120  # a longer base symbol is shown as a window, like <code_now>


def at_base_branch(
    ws: Workspace, f: Finding, head_sym: Symbol | None, head_line: int = 0
) -> str:
    """The finding's enclosing function or class as it is on the BASE branch.

    The checkout is the base branch; `ws.text` overlays the PR head, so the
    base copy is read from disk. Verify needs it to tell a break against what
    shipped from one against an intermediate commit of this PR: without it
    the call sees only the PR head and the round's diff. The symbol is found
    at the head (enclosing the finding's line), then looked up in the base
    file by its dotted name, which survives a rename of the file. A finding
    outside any function or class (a module constant) gets the base lines
    around its quoted code instead."""
    fd = ws.diffs.get(f.path)
    rel = (
        fd.old_path if fd and fd.status == "renamed" and fd.old_path else f.path
    ) or ""
    root = ws.root.resolve()
    path = (root / rel).resolve()
    if not rel or root not in path.parents or not path.is_file():
        return f"absent on base: {rel or f.path} (the file does not exist there)"
    try:
        text = path.read_text(encoding="utf-8")
        ast.parse(text)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError):
        return f"(the base copy of {rel} could not be read or parsed)"
    if head_sym is None:
        # Module level: no symbol to look up, so the quote locates the site.
        at = locate_in_text(text, f.evidence)
        if not at:
            return f"absent on base: the quoted code is not in {rel}"
        return f"{rel}: module level\n{_numbered(text, at - 15, at + 15)}"
    dotted = head_sym.qualname.split(":", 1)[1]
    got = base_symbol_excerpt(root, rel, text, f, head_sym, head_line)
    return f"{rel}: {dotted}\n{got}" if got else f"absent on base: {dotted} in {rel}"


def _numbered(text: str, lo: int, hi: int) -> str:
    lines = text.splitlines()
    return "\n".join(
        f"{i:>5} {lines[i - 1]}" for i in range(max(lo, 1), min(hi, len(lines)) + 1)
    )


def base_symbol_excerpt(
    root: Path, rel: str, text: str, f: Finding, head_sym: Symbol, head_line: int
) -> str:
    """`head_sym`'s counterpart in `text` (a base copy of `rel`), numbered, or ""
    when the base copy has no symbol of that dotted name.

    The whole symbol when it is under BASE_EXCERPT_LINES, else a window around
    the finding's site: its quote when the base has it, otherwise the same
    offset into the symbol as the finding has into the head symbol. Shared by
    verify (the checkout on disk) and `/lens recheck` (the merge base via the API)."""
    dotted = head_sym.qualname.split(":", 1)[1]
    idx = Index(root=str(root))
    index_source(idx, rel, text)
    base_sym = next(
        (
            idx.symbols[q]
            for q in idx.by_path.get(rel, [])
            if q.split(":", 1)[1] == dotted
        ),
        None,
    )
    if base_sym is None:
        return ""
    lo, hi = base_sym.start, base_sym.end
    if hi - lo >= BASE_EXCERPT_LINES:
        at = locate_in_text(text, f.evidence)
        if not lo <= at <= hi:
            # The quote is PR-only: take the same offset into the base symbol
            # as the finding has into the head symbol, not the symbol's top.
            at = lo + max(head_line - head_sym.start, 0)
        mid = min(at, hi)
        lo, hi = max(mid - 15, lo), min(mid + 15, hi)
    return _numbered(text, lo, hi)


VERIFY_BATCH = 20  # findings per verify call; every open finding is checked


_CONCERN_ID = re.compile(r"^A\d+$")


def open_concerns(state: PRState) -> list[tuple[str, dict[str, Any]]]:
    """The approach check's concerns not yet addressed, with stable ids A1, A2, …
    (the concern list is fixed once per PR, so its order is the id)."""
    ap = state.approach or {}
    if ap.get("verdict") != "concerns":
        return []
    return [
        (f"A{i}", c)
        for i, c in enumerate(ap.get("concerns") or [], 1)
        if c.get("status", "open") == "open"
    ]


@dataclass
class Verified:
    fixed: list[str] = field(default_factory=list)  # judged fixed: F-… and A…
    # Sent to a call that spent its whole output budget before answering.
    cut_off: list[str] = field(default_factory=list)
    # Of `fixed`, the findings judged moot: never a defect, because the failure
    # needs history only an earlier commit of this PR could have produced.
    moot: list[str] = field(default_factory=list)
    # Why no verdict came back at all ("budget" or the model error), "" when one did.
    error: str = ""
    # Every id the reply gave a verdict for (fixed, moot or open).
    judged: list[str] = field(default_factory=list)


def _verify(
    client: Client,
    ws: Workspace,
    open_: list[Finding],
    changes: str = "",
    concerns: list[tuple[str, dict[str, Any]]] | None = None,
    *,
    removed: str = "",
    released: str = "",
    added: PRAdditions | None = None,
    max_tokens: int = AgentLimits.verify_max_tokens,
) -> Verified:
    """Every open finding, VERIFY_BATCH at a time, and the open approach concerns
    (with the first batch). The paths the PR removes, the last release's public
    names it removes (`surface`), then the round's changes, lead each call, so the batches share one cached prefix. Returns the ids
    judged fixed, and the ids no verdict came back for because a call ran out
    of output budget first."""
    batches = [
        open_[i : i + VERIFY_BATCH] for i in range(0, len(open_), VERIFY_BATCH)
    ] or [[]]
    out = Verified()
    for n, batch in enumerate(batches):
        got = _verify_batch(
            client,
            ws,
            batch,
            changes,
            concerns if n == 0 else None,
            removed=removed,
            released=released,
            added=added,
            max_tokens=max_tokens,
        )
        out.fixed += got.fixed
        out.cut_off += got.cut_off
        out.moot += got.moot
        out.judged += got.judged
        out.error = out.error or got.error
    return out


def _verify_batch(
    client: Client,
    ws: Workspace,
    open_: list[Finding],
    changes: str,
    concerns: list[tuple[str, dict[str, Any]]] | None = None,
    *,
    removed: str = "",
    released: str = "",
    added: PRAdditions | None = None,
    max_tokens: int = AgentLimits.verify_max_tokens,
    new: bool = False,
) -> Verified:
    """One verify call. `new` marks the findings as raised this round, not
    earlier: they are judged against the base branch before being published."""
    items = (
        [f"<paths_removed_by_this_pr>\n{removed}\n</paths_removed_by_this_pr>"]
        if removed
        else []
    )
    if released:
        items.append(
            f"<released_surface_removals>\n{released}\n</released_surface_removals>"
        )
    items += added_blocks(added)
    if changes:
        items.append(f"<changes_this_round>\n{changes}\n</changes_this_round>")
    for cid, c in concerns or []:
        items.append(
            f'<concern id="{cid}">\n{c.get("title", "")}. {c.get("why", "")}\n</concern>'
        )
    mark = ' new="true"' if new else ""
    for f in open_:
        text = ws.text(f.path) or ""
        lines = text.splitlines()
        at = _current_line(ws, f, text)
        sym = ws.index.enclosing(f.path, at) if at else None
        lo, hi = (
            (sym.start, sym.end)
            if sym and sym.end - sym.start < 120
            else (max(at - 15, 1), at + 15)
        )
        code = (
            "\n".join(
                f"{i:>5} {lines[i - 1]}" for i in range(lo, min(hi, len(lines)) + 1)
            )
            if at
            else "(the quoted code is not in this file any more)"
        )
        items.append(
            f'<finding id="{f.id}" path="{f.path}"{mark}>\n{f.title}. {f.body}\n'
            f"<quoted_when_raised>\n{f.evidence}\n</quoted_when_raised>\n"
            f"<code_now>\n{code}\n</code_now>\n"
            f"<at_base_branch>\n{at_base_branch(ws, f, sym, at)}\n</at_base_branch>\n</finding>"
        )
    messages = [
        {"role": "system", "content": prompts.VERIFY_SYSTEM},
        {"role": "user", "content": "\n".join(items)},
    ]
    try:
        comp = client.complete(
            "verify",
            messages,
            max_tokens=max_tokens,
            tools=prompts.VERIFY_TOOLS,
            tool_choice="required",
            cache_key="lens-verify",
        )
    except BudgetExhausted:
        return Verified(error="budget")
    except LLMError as e:
        return Verified(error=str(e)[:200] or "model error")
    if not comp.tool_calls and _out_of_output_budget(comp, max_tokens):
        # No verdict is not "nothing fixed": the model reasoned to the cap and
        # never answered. Keep every item as it was, and say so.
        return Verified(
            cut_off=[f.id for f in open_] + [cid for cid, _ in concerns or []]
        )
    fixed: list[str] = []
    moot: list[str] = []
    judged: list[str] = []
    by_id = {f.id: f for f in open_}
    concern_ids = {cid for cid, _ in concerns or []}
    for it in verdict_items(comp.tool_calls):
        verdict = it.get("status")
        iid, ok = str(it.get("id")), verdict in ("fixed", "moot")
        f = by_id.get(iid)
        if f and verdict in ("fixed", "moot", "open"):
            judged.append(f.id)
        if f and ok:
            f.status = "fixed"
            fixed.append(f.id)
            if verdict == "moot":
                moot.append(f.id)
        elif iid in concern_ids and ok:
            fixed.append(iid)
    return Verified(fixed=fixed, moot=moot, judged=judged)


def verdict_items(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The `items` of every verdicts call, keeping only well-formed entries.

    Tool arguments are model output: a missing or non-list `items`, or an entry
    that is not an object, is skipped, so its finding keeps its status."""
    out: list[dict[str, Any]] = []
    for tc in tool_calls:
        fn = tc.get("function") if isinstance(tc, dict) else None
        args = (
            parse_args(str((fn or {}).get("arguments") or ""))
            if isinstance(fn, dict)
            else {}
        )
        items = args.get("items")
        if isinstance(items, list):
            out += [it for it in items if isinstance(it, dict)]
    return out


def _out_of_output_budget(comp: Any, max_tokens: int) -> bool:
    """The call stopped at its output cap: `length` on Chat Completions,
    `incomplete` on Responses, or every allowed token spent."""
    used = int((comp.usage or {}).get("completion_tokens") or 0)
    return comp.finish_reason in ("length", "incomplete") or used >= max_tokens


# ---- rendering ---------------------------------------------------------------

_SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
_SEV_LABEL = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low (nit)",
}


_FIXED_BY = {
    "code-gone": "its code was removed or rewritten",
    "verified": "verified fixed",
    "pr-history": "closed: needed history only this PR's own earlier commits could produce",
}
_CLOSE_HINT = (
    "Fix them and comment `/lens`, or close one the team won't fix with "
    "`/lens dismiss <id> <reason>`."
)


def _how(f: Finding) -> str:
    if f.fixed_by == "dismissed":
        why = f": {f.dismiss_reason}" if f.dismiss_reason else ""
        return f"dismissed by @{f.dismissed_by}{why}".replace("|", "/")
    if f.fixed_by == "rechecked":
        why = f": {f.dismiss_reason}" if f.dismiss_reason else ""
        return f"re-checked at @{f.dismissed_by}'s request{why}".replace("|", "/")
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
            f"| {'after ' if h.get('mode') == 'dismiss' else ''}{h['round']} | {run_kind} | {span} | {h.get('new', 0)} | {h.get('resolved', 0)} "
            f"| {h.get('blocking', '—')} | ${float(h.get('usd', 0)):.3f} | {log} |"
        )
    out.append("\n</details>")
    return out


def _run_title(res: RunResult, round_no: int) -> str:
    """ "round 2 · re-review · …", or for a dismissal (which is not a review round)
    "dismissal after round 2 · F-… by @…"."""
    label = res.mode_label or res.mode
    if res.mode == "dismiss":
        return f"dismissal after round {round_no} · {label.removeprefix('dismiss · ')}"
    if res.mode == "recheck":
        return f"re-check after round {round_no} · {label.removeprefix('recheck · ')}"
    return f"round {round_no} · {label}"


SPIRAL_ROUNDS = (
    3  # a file drawing new findings in this many rounds is worth stepping back from
)


def spiral_paths(
    state: PRState, new: list[Finding], round_no: int
) -> list[tuple[str, list[int]]]:
    """Files where this round's new BLOCKING findings continue a run: the same file
    has drawn new findings in SPIRAL_ROUNDS or more rounds. Each fix uncovering the
    next corner case is a sign the approach, not the latest patch, needs another look."""
    if round_no < SPIRAL_ROUNDS:
        return []
    out = []
    for path in sorted({f.path for f in new if f.severity in BLOCKING}):
        rounds = sorted(
            {
                f.round
                for f in state.findings
                if f.path == path
                and f.severity in BLOCKING
                and f.fixed_by != "pr-history"  # never a defect: not a spiral
            }
        )
        if len(rounds) >= SPIRAL_ROUNDS:
            out.append((path, rounds))
    return out


def _spiral_lines(res: RunResult) -> list[str]:
    return [
        f"\n🔁 **Worth stepping back:** `{path}` has drawn new blocking findings in rounds "
        f"{', '.join(map(str, rounds))}. Each fix is uncovering another case; consider simplifying "
        "the approach instead of patching case by case."
        for path, rounds in res.spiral
    ]


def _concern_lines(ap: dict[str, Any], *, brief: bool) -> list[str]:
    """Open concerns in full; addressed ones as one line, so the PR shows what is
    still worth a look and not a concern the author has since dealt with."""
    cs = ap.get("concerns") or []
    still = [(i, c) for i, c in enumerate(cs, 1) if c.get("status", "open") == "open"]
    done = [(i, c) for i, c in enumerate(cs, 1) if c.get("status") == "addressed"]
    out = []
    if not brief:
        out.append(
            "**Approach check** — worth a second look (advisory, does not block):"
            if still
            else "**Approach check** — ✅ every concern has been addressed."
        )
    for i, c in still:
        alt = f" *Instead:* {c['alternative']}" if c.get("alternative") else ""
        mark = "⚠️ " if brief else ""
        out.append(f"- {mark}**{c.get('title', '')}** (A{i}) — {c.get('why', '')}{alt}")
    for i, c in done:
        out.append(
            f"- ✔️ ~~{c.get('title', '')}~~ (A{i}) — addressed in round {c.get('addressed_round', '?')}"
        )
    return out


_FENCE = {
    ".py": "python",
    ".toml": "toml",
    ".yml": "yaml",
    ".yaml": "yaml",
    ".md": "markdown",
    ".pkl": "",
}


def _where(f: Finding, blob_base: str) -> str:
    """The finding's location, linked to the file at the reviewed commit."""
    line = f.line or f.head_line
    label = f"{f.path}:{line}" if line else f.path
    if blob_base:
        url = f"{blob_base}/{urllib.parse.quote(f.path, safe='/')}"
        where = f"[`{label}`]({url}" + (f"#L{line}" if line else "") + ")"
    else:
        where = f"`{label}`"
    if f.scope == "unchanged":
        where += " (unchanged code: same pattern)"
    return where


def _details(f: Finding, blob_base: str, chars: int | None = None) -> str:
    """One finding in full, collapsed: where, what goes wrong, and the suggested change
    (what used to be an inline comment; lens opens no review threads). `chars` caps the
    text for a PR too large to show every finding in full."""
    ext = "." + f.path.rsplit(".", 1)[-1] if "." in f.path else ""

    def cap(text: str) -> str:
        return (
            text
            if chars is None or len(text) <= chars
            else text[:chars].rstrip() + " …"
        )

    out = [
        f"<details><summary>{_SEV_ICON[f.severity]} {f.id} · {f.severity} · {f.category} — "
        f"{f.title}</summary>\n",
        f"**Where:** {_where(f, blob_base)}\n",
        cap(f.body.strip()),
    ]
    if f.scenario.strip():
        out.append(f"\n**When it fails:** {cap(f.scenario.strip())}")
    if f.suggestion.strip() and chars != 0:
        # Longer than any backtick run inside, so a suggestion that itself holds a
        # fenced example (a Markdown doc) cannot close the block early.
        runs = [len(m) for m in re.findall(r"`+", f.suggestion)]
        fence = "`" * max(3, max(runs, default=0) + 1)
        out.append(
            f"\n**Suggested change:**\n\n{fence}{_FENCE.get(ext, '')}\n"
            f"{f.suggestion.rstrip()}\n{fence}"
        )
    out.append("\n</details>")
    return "\n".join(out)


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
        f"### lens summary · updated after round {st.round} ({res.mode_label or res.mode})",
        verdict,
        *_spiral_lines(res),
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
        lines.extend(_concern_lines(ap, brief=False))
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
            lines.append(f"| {f.id} | {_where(f, res.blob_base)} | {f.title} |")
        lines.append("")
    opened = st.open_findings()
    details_at = len(lines)  # the details block is sized last (see the size backstop)
    lines.extend(_resolved_section(st))
    lines.extend(_history_section(st))
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
    if verify_notice_line(res):
        lines.append("\n> " + verify_notice_line(res))
    for n in res.notes:
        lines.append(f"\n> ℹ️ {n}")
    lines.append(
        f"\n<sub>round {st.round} · ${led.get('spent_usd', 0):.3f} of ${led.get('cap_usd', 0):.2f} · "
        f"{led.get('calls', 0)} model calls · {failed} failed requests · "
        f"{hit:.0%} prompt cache hits · {st.model}</sub>"
    )
    # GitHub refuses a comment over 65,536 characters, and a refused edit would lose
    # the state. Only a very large PR gets past the first try: each finding's details
    # are shortened, then the stored bodies, then the details and the Resolved table
    # are dropped from view. The findings, their ids and the quoted code always stay.
    for detail_chars in (None, 600, 200):
        block = _details_block(opened, res.blob_base, detail_chars)
        head = "\n".join(lines[:details_at] + block + lines[details_at:]) + "\n"
        for keep in (BODY_KEEP, 200, 0):
            body = head + st.encode(body_keep=keep)
            if len(body) <= COMMENT_LIMIT:
                return body
    lean = "\n".join(_without_resolved(lines)) + "\n"
    return lean + st.encode(body_keep=0)


def _details_block(
    opened: list[Finding], blob_base: str, chars: int | None
) -> list[str]:
    if not opened:
        return []
    return [
        "**Details** — each finding, what goes wrong, and a suggested change:\n",
        *(_details(f, blob_base, chars) for f in opened),
        "",
    ]


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


def verify_cut_off_line(res: RunResult) -> str:
    """Why some open items were not re-judged, and whether the opt-in is worth it."""
    if not res.verify_cut_off:
        return ""
    ids = ", ".join(res.verify_cut_off)
    line = (
        f"⚠️ **Verify ran out of output budget** — the re-check of {ids} spent all "
        f"{res.verify_max_tokens:,} output tokens before answering, so they were **not "
        "re-judged** this round. They show as open because nothing checked them, not "
        "because a check found them unfixed."
    )
    if res.verify_opted_in:
        return line + (
            " This was already the opt-in budget (`agent.verify_max_tokens_opt_in` in "
            "`.github/lens/config.toml`)."
        )
    return line + (
        f" Worth opting in: comment `/lens force verify-budget` to re-check them with "
        f"{res.verify_opt_in_tokens:,}. Keep `force`: without it, a round whose push "
        "changes none of the PR's files (a merge from the base branch) re-checks nothing; "
        "with it, they are re-checked whatever the push changed."
    )


def verify_skipped_line(res: RunResult) -> str:
    """Why a round re-checked nothing, and how to make it re-check."""
    if not res.verify_skipped:
        return ""
    return (
        f"ℹ️ **Nothing was re-checked this round** — none of the PR's own files changed "
        "since the last review (a merge from the base branch alone changes none), so "
        f"{', '.join(res.verify_skipped)} were not re-judged. They show as open because "
        "nothing checked them. Comment `/lens force` to re-check them against the whole "
        "PR, or `/lens force verify-budget` if the last re-check ran out of output budget."
    )


def verify_notice_line(res: RunResult) -> str:
    """The one verify notice a round can carry: cut off, or not run at all."""
    return verify_cut_off_line(res) or verify_skipped_line(res)


def verify_cut_off_report(res: RunResult) -> dict[str, Any] | None:
    """The cut-off for machine readers: which items, the budget they had, the
    opt-in, and the same guidance the verdict gives. None when nothing was cut off."""
    if not res.verify_cut_off:
        return None
    return {
        "items": res.verify_cut_off,
        "max_tokens": res.verify_max_tokens,
        "opt_in_tokens": res.verify_opt_in_tokens,
        "opted_in": res.verify_opted_in,
        "message": verify_cut_off_line(res),
    }


def verify_skipped_report(res: RunResult) -> dict[str, Any] | None:
    """The skipped re-check for machine readers. None when verify ran or had nothing."""
    if not res.verify_skipped:
        return None
    return {"items": res.verify_skipped, "message": verify_skipped_line(res)}


def verdict_brief(res: RunResult, summary_url: str) -> str:
    """The verdict, kept within GitHub's comment limit: this round's new findings are
    shown in full, then shortened, then listed by title (the summary has them all)."""
    for chars in (None, 600, 200, 0):
        body = _verdict_brief(res, summary_url, chars)
        if len(body) <= COMMENT_LIMIT:
            return body
    return body[: COMMENT_LIMIT - 200] + "\n\n… (truncated: see the full summary)"


def _verdict_brief(res: RunResult, summary_url: str, detail_chars: int | None) -> str:
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
        f"{icon} **lens · {_run_title(res, st.round)}** — {description}",
        *_spiral_lines(res),
        "",
        f"**Open findings:** {counts}",
    ]
    if res.new_findings:
        lines.append(f"**New this round:** {len(res.new_findings)}\n")
        if detail_chars == 0:
            lines.extend(
                f"- {_SEV_ICON[f.severity]} {f.id} — {f.title} ({_where(f, res.blob_base)})"
                for f in res.new_findings
            )
            lines.append(
                "\nToo many to show here in full: the summary has every finding's details."
            )
        else:
            lines.extend(
                _details(f, res.blob_base, detail_chars) for f in res.new_findings
            )
    if state == "failure" and not st.open_findings(BLOCKING):
        lines.append(_CLOSE_HINT)
    fixed = len(res.resolved_free) + len(res.resolved_verified)
    if fixed:
        lines.append(f"**Resolved this round:** {fixed}")
    if verify_notice_line(res):
        lines.append("\n" + verify_notice_line(res))
    ap = st.approach or {}
    if ap.get("verdict"):
        # The holistic review, in full: how lens reads the change, and whether the
        # approach is the right one — not just a one-word verdict.
        lines.append("")
        concerns = ap.get("verdict") == "concerns"
        still = [
            c for c in ap.get("concerns") or [] if c.get("status", "open") == "open"
        ]
        label = (
            "⚠️ concerns (advisory)"
            if concerns and still
            else "✅ concerns addressed"
            if concerns
            else "✅ sound"
        )
        lines.append(f"**Approach check — {label}**")
        if ap.get("problem"):
            lines.append(f"- *Problem:* {ap['problem']}")
        if ap.get("approach"):
            lines.append(f"- *How the PR solves it:* {ap['approach']}")
        if concerns:
            lines.extend(_concern_lines(ap, brief=True))
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
    """Summary, then the verdict at the bottom, then the status.

    lens opens no inline review threads: every finding, with its explanation and
    suggested change, is in the summary and (for this round's new ones) in the
    verdict. Threads had to be resolved by hand to merge, and resolving them needs
    a token with write access to the repository contents, which the step that reads
    PR text must never hold. A summary created by this run is already the bottom
    comment and carries the verdict, so the verdict is not repeated under it."""
    url = gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))
    if not res.summary_is_new:
        gh.comment(number, verdict_brief(res, url))
    state, description = verdict_status(res)
    gh.set_status(head, state, description, url)
    trace.line(f"sticky summary: {url or '(url unavailable)'}")
    trace.line(
        "verdict in the new summary"
        if res.summary_is_new
        else "verdict posted at the bottom"
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
            "verify_cut_off": verify_cut_off_report(res),
            "verify_skipped": verify_skipped_report(res),
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
