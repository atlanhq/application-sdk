"""One connector-reviewer run on one PR: admit → scope → (free checks) → review → verify → post.

The round rules that make the loop converge are here, in code:

- **Admission.** connector-reviewer runs only when asked. An unchanged head is never
  re-reviewed (humans included) unless forced; past `max_rounds`, connector-reviewer
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
  file or that `/review force` asked for.
- **Merge rule.** Blocking = open critical/high findings. Medium/low never block.
"""

from __future__ import annotations

import ast
import json
import re
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from . import advisory, baseline, exempt, holistic, prompts, sdk_source, skeptic, trace
from . import context as code_context
from . import facts as code_facts
from .agent import AgentLimits, BundleResult, review_bundle, suspect_paths
from .baseline import PRAdditions
from .bundle import Bundle, group
from .config import Config
from .diff import (
    FileDiff,
    Hunk,
    Line,
    anchor,
    in_removed_lines,
    locate_in_text,
    parse_unified_diff,
    snippet_in_text,
)
from .event import plain
from .findings import (
    BLOCKING,
    BODY_KEEP,
    EVIDENCE_KEEP_RESOLVED,
    SEVERITIES,
    STATE_MARKER,
    Finding,
    PRState,
    merge_new,
    quote_stored,
    unique_ids,
)
from .github import GitHub, GitHubError, bot_login
from .index import Index, Symbol, build_index, index_source
from .llm import BudgetExhausted, Client, Ledger, LLMError
from .principles import PrincipleSet
from .select import DEFAULT_EXCLUDE, excluded, model_visible, select_files
from .tools import SdkSource, Workspace, parse_args, secret_diff, secret_path
from .triage import Triage, triage
from .typed import EMPTY, objects

SUMMARY_MARKER = "<!-- connector-reviewer-summary -->"
# The share of a round's budget held for the approach check, which runs on a
# ledger of its own alongside the line review.
APPROACH_SHARE = 0.15
# The share held for the skeptic pass on new blocking findings (skeptic.py), on a
# ledger of its own the same way. Only spend is booked: an unused share stays in
# the PR's cap for later rounds.
SKEPTIC_SHARE = 0.10
COMMENT_LIMIT = 64_000  # GitHub's cap is 65,536 characters; keep a margin
MAX_HISTORY = 20  # rounds kept in the state and shown in the summary
MECHANICAL_SHOWN = 20  # withheld files named per reason in the summary's box


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
    verify_opt_in_tokens: int = 0  # what `/review verify-budget` would give it
    verify_opted_in: bool = False
    # Open items (F-… and A…) not re-checked because this round changed none of the
    # PR's own files (e.g. only a merge from the base branch): no verify call ran.
    verify_skipped: list[str] = field(default_factory=list)
    # New blocking findings this round's base-branch check judged fixed or moot:
    # dropped before publishing (see _check_new_against_base).
    new_dropped: list[str] = field(default_factory=list)
    # One skeptic check per new blocking finding (skeptic.py): what each concluded.
    skeptic: list[skeptic.Outcome] = field(default_factory=list)
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
            b.stop in ("llm_error", "fatal", "refused") for b in self.bundles
        )

    @property
    def refused(self) -> list[BundleResult]:
        """Bundles whose request the model provider refused (`provider_rejected`)."""
        return [b for b in self.bundles if b.stop == "refused"]


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
        # A removed line is the author's when the PR's own diff removes the same
        # text from this file: old-side numbers differ between the two diffs, so
        # text is the stable key. A deletion only the merge brought in is not.
        own_removed = Counter(
            ln.text for h in own.hunks for ln in h.lines if ln.kind == "-"
        )
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
                    if ln.kind == "-" and own_removed[ln.text] > 0:
                        own_removed[ln.text] -= 1
                        mine = True  # an author's deletion, even with nothing added
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


_SHA = re.compile(r"[0-9a-f]{40}")


def base_ref_output(gh: GitHub, number: int) -> tuple[str, str]:
    """The `$GITHUB_OUTPUT` lines of `base-ref`, and a log line.

    `base=` is the pull request's base commit (`base.sha`, the same `base`
    `run` diffs against), which the workflow checks the app out at, so the
    unchanged files, the index and the facts are the code this pull request
    is judged against, not the default branch as it is today (a pull request
    against another branch, or a replay of an old one). Written only when it
    is 40 hex; empty otherwise, and the checkout then falls back to its
    default ref, which `base_checkout_note` reports. Never raises."""
    if number <= 0:
        return "base=\n", "no pull request to read the base commit from"
    try:
        pr = gh.pr(number) or EMPTY
        sha = str((pr.get("base") or EMPTY).get("sha") or "")
    except Exception as e:  # noqa: BLE001 - a failed read is a reason, never a crash
        return "base=\n", f"the pull request could not be read ({type(e).__name__})"
    if not _SHA.fullmatch(sha):
        return "base=\n", "the pull request names no base commit"
    return f"base={sha}\n", f"base commit {sha[:9]}"


def base_checkout_note(root: Path, base: str) -> str:
    """The incomplete reason when the app checkout at `root` is a git checkout
    whose commit is not the pull request's base `base`: `base-ref` failed and
    the checkout fell back to its default ref, or the base moved between that
    step and this review. "" when it is at `base`, or `root` is no checkout of
    its own (a local run)."""
    at = sdk_source.checkout_commit(root)
    if not at or at == base:
        return ""
    return (
        f"The app checkout is at {at[:9]}, not at this pull request's base "
        f"{base[:9]}: unchanged files, the index and the facts were read from "
        "other code, so this head is not taken as reviewed; comment `/review` "
        "to run it again"
    )


def base_merged_in(gh: GitHub, base: str, reviewed_head: str, head: str) -> str:
    """Why `reviewed_head..head` is taken to carry base-branch changes, or ""
    when it carries none.

    The range carries base-branch changes only when a merge brought them in,
    which moves the merge base with the base branch. Without one, every change
    in the range is the author's, including a line restored to what the base
    branch has and a commit that only deletes.

    A merge base that cannot be read (a rate limit, a missing SHA) is not proof
    of a merge: taking it as one could drop an author's restoration from review
    with nothing marking the run incomplete. The whole range is reviewed
    instead; at worst a merged-in base change is reviewed too, which costs a
    little and hides nothing."""
    before = gh.merge_base(base, reviewed_head)
    after = gh.merge_base(base, head)
    if not before or not after:
        trace.line(
            "the merge base could not be read: reviewing the whole range "
            "since the last review, as if no base merge happened"
        )
        return ""
    if before != after:
        return f"the merge base moved from {before[:7]} to {after[:7]}"
    return ""


def author_files_in_range(
    incremental: list[FileDiff], pr: list[FileDiff], finding_paths: set[str]
) -> dict[str, FileDiff]:
    """After a base merge: the whole `reviewed_head..head` diff of every file
    the author may have changed, by path, for verify.

    `only_pr_changes` keeps only lines the PR adds against the base branch, so
    it also sets aside an author's restoration of base lines and an author's
    deletion. Verify must still see those. A file in the range counts when the
    PR changes it or an open finding sits in it; a file the base merge alone
    changed may count too, which costs one verify call, never a missed fix."""
    pr_paths = {fd.path for fd in pr}
    return {
        fd.path: fd
        for fd in incremental
        if fd.hunks and (fd.path in pr_paths or fd.path in finding_paths)
    }


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

    A change that may hold secrets is never fetched: a file renamed from `.env`
    carries the `.env` content under an ordinary name, and anything in this map
    can reach a prompt through a lookup (read, search, the index).
    """
    head_text: dict[str, str] = {}
    for fd in files[:max_files]:
        if fd.status == "deleted":
            head_text[fd.path] = ""
        elif not fd.is_binary and not secret_diff(fd):
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


def secret_origin(files: list[FileDiff], pr: list[FileDiff]) -> list[FileDiff]:
    """`files` (a round's range), where a file the whole PR renamed from a path
    that may hold secrets keeps that rename source.

    In a later round's range such a file reads as an ordinary change of its new
    path, while its content is still the secret file's. Carrying the source over
    lets `secret_diff` refuse it in every prompt, as it does in the PR's diff."""
    origin = {fd.path: fd.old_path for fd in pr if fd.old_path and secret_diff(fd)}
    return [
        FileDiff(
            path=fd.path,
            old_path=origin[fd.path],
            status=fd.status,
            is_binary=fd.is_binary,
            hunks=fd.hunks,
        )
        if fd.path in origin and not secret_diff(fd)
        else fd
        for fd in files
    ]


# The reasons `select.select_files` and `plan_bundles` give for a file the review
# meant to read and did not. Anything else skipped (binary, a deleted test file,
# a deleted file too large to read, excluded by pattern, may hold secrets) is
# left out on purpose.
# A test file deleted outright that weakens an expectation, in a round with no
# line review to hand the facts to: nothing in the change can be commented on.
WEAKENED_UNREVIEWED = "deleted test: weakens expectations, no line to review"
UNREAD_REASONS = ("over ", "bundle cap", WEAKENED_UNREVIEWED)


def unread_skips(skipped: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The skipped files that are unread source: too large, past the bundle cap,
    or a deleted test that weakens expectations with no line review to see it."""
    return [(p, why) for p, why in skipped if why.startswith(UNREAD_REASONS)]


def _no_verdict(
    gh: GitHub, head: str, state: PRState, post: bool, run_url: str, why: str
) -> None:
    """An `error` status on a head this request did not review (round cap, spent
    budget), so no head is ever left without a verdict or with a stale one.
    A head that was already reviewed keeps the verdict it has."""
    if post and head and head != state.reviewed_head:
        gh.set_status(head, "error", why, run_url)


def _whole_files(files: list[FileDiff]) -> list[FileDiff]:
    """One diff per path, in first-seen order. A file over the bundle token cap
    is reviewed in parts (`bundle._parts`); the facts are about the file, so
    its parts are joined back into one diff before they are computed."""
    joined: dict[str, FileDiff] = {}
    for fd in files:
        have = joined.get(fd.path)
        if have is None:
            joined[fd.path] = fd
        else:
            joined[fd.path] = FileDiff(
                path=fd.path,
                old_path=fd.old_path,
                status=fd.status,
                is_binary=fd.is_binary,
                hunks=[*have.hunks, *fd.hunks],
            )
    return list(joined.values())


def compute_facts(
    gh: GitHub,
    number: int,
    pr: dict[str, Any],
    base: str,
    ws: Workspace,
    files: list[FileDiff],
    cfg: Config,
    tests_changed: list[FileDiff] | None = None,
) -> code_facts.Facts:
    """The regression and history facts for the files under review, in review
    order. No model call; the history half makes at most
    3 + `history_max_files` + `history_max_commits` GitHub requests (the
    commit messages take up to three pages).

    `tests_changed` are the test files this round changed, taken before file
    selection: a deleted test file is skipped by selection ("deletion only"),
    and it is exactly a weakened expectation FP15 asks about."""
    fx = code_facts.Facts()
    if cfg.facts.regression:
        fx.symbols = code_facts.regression_symbols(ws, files)
        fx.expectations = code_facts.weakened_expectations(
            files if tests_changed is None else tests_changed
        )
        fx.regression_read = True
    if cfg.facts.history and files:
        names = {
            fd.path: {s.name for s in code_context.changed_symbols(ws, fd)}
            for fd in files
            if fd.path.endswith(".py")
        }
        total = pr.get("commits")
        # The commit messages are read inside history's wall-time cap, and a
        # failed read (HTTP, network or a bad body) is a note, never a crash.
        fx.history = code_facts.history(
            gh,
            base=base,
            files=files,
            changed_names=names,
            pr_title=str(pr.get("title") or ""),
            pr_messages=[],
            cfg=cfg.facts,
            read_messages=lambda: gh.pr_commit_messages(number),
            pr_commits=total if isinstance(total, int) else 0,
        )
        fx.history_read = True
    return fx


def changed_tests(files: list[FileDiff], cfg: Config) -> list[FileDiff]:
    """The Python test files in `files`, deleted ones included, minus what is
    never read: a change that may hold secrets, and anything the exclude
    patterns name (fixtures, golden files, snapshots)."""
    exclude = tuple(DEFAULT_EXCLUDE) + tuple(cfg.exclude)
    return [
        fd
        for fd in files
        if fd.path.endswith(".py")
        and code_context._is_test(fd.path)
        and not fd.is_binary
        and not secret_diff(fd)
        and not excluded(fd.path, exclude)
    ]


def exclude_patterns(cfg: Config) -> tuple[str, ...]:
    """The exclude patterns selection applies to the line review: the defaults
    and the repository's own. Every other prompt that shows a diff applies them
    too (`select.model_visible`)."""
    return tuple(DEFAULT_EXCLUDE) + tuple(cfg.exclude)


def _approach(
    client: Client,
    ws: Workspace,
    files: list[FileDiff],
    pr_meta: dict[str, Any],
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
) -> tuple[holistic.ApproachCheck, int]:
    """The approach check, run in its own thread; returns it and its wall time (ms)."""
    t0 = time.monotonic()
    ac = holistic.check(client, ws, files, pr_meta, exclude=exclude)
    return ac, int((time.monotonic() - t0) * 1000)


def find_state(gh: GitHub, number: int) -> tuple[PRState | None, str]:
    for c in gh.issue_comments(number):
        body = c.get("body") or ""
        # Only the App's own comment is trusted: see github.bot_login().
        if (
            SUMMARY_MARKER in body
            and (c.get("user") or EMPTY).get("login") == bot_login()
        ):
            return PRState.decode(body), body
    return None, ""


def sdk_pins_block(
    ws: Workspace,
    lock: sdk_source.HeadLock,
    at_head: Callable[[str], str | None],
) -> str:
    """The `<other_sdk_packages>` block for the line review (FND-5316): the
    Atlan SDK packages the head under review pins that `sdk:` cannot read;
    "" when it pins none. Everything is read at that head, never from the
    checkout, which is the pull request's base: the lock is `lock` (read at the
    head; when it could not be read, nothing comes from a lock), and each
    `pyproject.toml` path the workspace knows (at most
    `sdk_source.MAX_PYPROJECTS`) is read with `at_head`, or taken from the
    pull request's own text when it changed the file."""
    paths = [p for p in ws.files() if sdk_source.is_pyproject(p)]
    pyprojects: dict[str, str] = {}
    for p in sorted(paths)[: sdk_source.MAX_PYPROJECTS]:
        try:
            text = ws.head_text[p] if p in ws.head_text else at_head(p)
        except Exception:  # noqa: BLE001 - a failed read leaves the file out
            text = None
        if text:
            pyprojects[p] = text
    others = sdk_source.other_pins(lock.text, pyprojects)
    return sdk_source.pins_block(others, ws.sdk.label if ws.sdk else None)


def run(
    *,
    gh: GitHub,
    number: int,
    root: Path,
    cfg: Config,
    principles: PrincipleSet,
    client_factory: Any,
    force: bool = False,
    verify_budget: bool = False,
    post: bool = True,
    run_url: str = "",
    on_pending: Callable[[str], None] | None = None,
    sdk: SdkSource | None = None,
    sdk_note: str = "",
    sdk_dir: str | None = None,
    sdk_head: str = "",
) -> RunResult:
    """One review round. `on_pending(head)` is called right after the
    `pending` status is set on `head`, so the caller can record which head
    `finalize` must check if this run never posts a verdict. `sdk_dir` is the
    SDK checkout the workflow made (FND-5331), opened here against the
    `uv.lock` of the very head this round reviews, and only when the checkout
    is at the ref that lock pins; `sdk_head` is the head the workflow chose the
    checkout's ref from, named when the two differ (FND-5316). A caller that
    built the source itself passes `sdk` and `sdk_note` instead."""
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
            trace.line("no previous connector-reviewer state on this PR: first review")
        trace.line(
            f"model={cfg.model} api={cfg.api} effort={cfg.reasoning_effort} config={cfg.raw_hash} force={force}"
        )

    # ---- exempt pull requests (0 model calls) ----------------------------
    # Decided in code from who opened it, its branch, its commits and its files.
    # Only a pull request the reviewer has never reviewed can be exempt: once a
    # forced review has looked at it, a plain `/review` must not paper over what
    # that review found.
    diffs: dict[tuple[str, str], str] = {}

    def diff(a: str, b: str) -> str:
        if (a, b) not in diffs:
            diffs[(a, b)] = gh.diff(a, b)
        return diffs[(a, b)]

    if not force and state.round == 0:
        found = exemption(gh, number, pr, diff(base, head), cfg)
        if found is not None:
            trace.line(f"decision: EXEMPT — {found.kind}: {found.why}")
            return _exempt(
                gh, number, head, state, found, bool(prior_summary), post, run_url
            )

    # ---- admission (0 model calls) --------------------------------------
    # Only a different MODEL is a different reviewer. A connector-reviewer config change (principles,
    # prompts, limits) does not re-open code an earlier round already passed: that
    # moved the goalposts on reviewed code. It applies from the next new commits.
    # `/review force` lifts the skip and round-cap rules: after new commits it
    # reviews just those (the cheap way to get a green status back after a small push);
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
            f"head {head[:8]} already reviewed; comment `/review force` to re-run",
            state=state,
        )
    if state.round >= cfg.max_rounds and not force:
        trace.line(f"decision: SKIP — round cap {cfg.max_rounds} reached")
        _no_verdict(
            gh,
            head,
            state,
            post,
            run_url,
            f"not reviewed — round cap ({cfg.max_rounds}) reached; `/review force` reviews it",
        )
        return RunResult(
            "skipped",
            f"round cap ({cfg.max_rounds}) reached; comment `/review force` to review anyway",
            state=state,
        )
    # No "N clean rounds, stop" rule: connector-reviewer runs only when a human asks, and a
    # request on an unchanged head is already refused above. New commits are
    # always reviewed, however many earlier rounds came back clean.

    ledger = (
        Ledger.from_dict(state.ledger, cfg.cap_usd_per_pr)
        if state.ledger
        else Ledger(cfg.cap_usd_per_pr)
    )
    if ledger.remaining <= 0.01:
        trace.line(f"decision: SKIP — PR budget ${cfg.cap_usd_per_pr:.2f} spent")
        _no_verdict(
            gh,
            head,
            state,
            post,
            run_url,
            f"review incomplete — PR budget of ${cfg.cap_usd_per_pr:.2f} spent; "
            "`/review waive <reason>` is the way past it",
        )
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
    # Files left unread last round (too large, or past the bundle cap) ride along
    # with every review, whole, so each round decides afresh whether they can be read.
    carried = set(state.pending_files) | set(state.unread)
    # After a base merge, the author's own range diff of each file verify must see.
    verify_range: dict[str, FileDiff] = {}
    if retry_only:
        full_files = parse_unified_diff(diff(base, head))
        all_files = [fd for fd in full_files if fd.path in carried]
        range_base = base
    else:
        range_base = state.reviewed_head if incremental else base
        all_files = parse_unified_diff(diff(range_base, head))
        full_files = (
            all_files if not incremental else parse_unified_diff(diff(base, head))
        )
        merged_in = (
            base_merged_in(gh, base, state.reviewed_head, head) if incremental else ""
        )
        if merged_in:
            # A merge of the base branch into the PR also "descends" from the last
            # reviewed head, so reviewed_head..head carries the base branch's changes
            # too. Line review keeps only what is part of THIS PR's own change;
            # verify still sees each file the author may have changed, whole.
            trace.line(f"base branch merged in since the last review: {merged_in}")
            verify_range = {
                p: fd
                for p, fd in author_files_in_range(
                    all_files, full_files, {f.path for f in state.open_findings()}
                ).items()
                if p not in carried
            }
            all_files, merged_paths, merged_lines = only_pr_changes(
                all_files, full_files
            )
            if merged_paths or merged_lines:
                trace.line(
                    f"excluded changes that came from merging the base branch: "
                    f"{len(merged_paths)} file(s), {merged_lines} line(s)"
                )
        elif incremental:
            # No merge: every change since the last review is the author's, a
            # restoration of base lines and a pure deletion included.
            trace.line("no base-branch merge since the last review")
        # Files a previous run failed to review ride along with the new commits, and
        # so do files it could not read; an unread file comes whole, never as the
        # slice of it the new commits touched.
        all_files = [fd for fd in all_files if fd.path not in state.unread]
        have = {fd.path for fd in all_files}
        all_files += [
            fd for fd in full_files if fd.path in carried and fd.path not in have
        ]
    # Whether a change may hold secrets is judged against the whole PR's diff:
    # a later round's range shows a file renamed from `.env` as an ordinary one.
    all_files = secret_origin(all_files, full_files)

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
    # A real review replaces a waive, and an exemption: the verdict is the
    # reviewer's again.
    state.waiver = {}
    state.exempt = {}
    # A principle file that was skipped is said in the summary, never silently.
    res.notes.extend(principles.notes)
    # The app checkout is the code this pull request is judged against: its
    # base commit, not the default branch (FND-5316). When it is not, the
    # review is incomplete and the head stays unreviewed, so the next
    # `/review` redoes it, never a green verdict read against other code.
    off_base = base_checkout_note(root, base)
    if off_base:
        trace.line(off_base)
        res.incomplete.append(off_base)
    # The head's own lock (FND-5316): the SDK source and the other-SDK block
    # are bound to the head this round reviews, never to the base checkout.
    lock = sdk_source.head_lock_at(gh, head)
    if sdk_dir is not None:
        sdk, sdk_note = SdkSource.open(
            sdk_dir, lock.resolution(), reviewed_head=head, chosen_at=sdk_head
        )
        trace.line(
            f"SDK source: application-sdk {sdk.label} at {sdk.root}"
            if sdk
            else sdk_note
        )
    if sdk_note:
        res.notes.append(sdk_note)
    if post:
        # Pending while it runs: the checks box links straight to the live log.
        gh.set_status(
            head, "pending", f"reviewing · round {round_no} · {mode_label}", run_url
        )
        if on_pending is not None:
            on_pending(head)
    trace.line(
        f"decision: REVIEW round {round_no} — {mode_label}; range={range_base[:9]}..{head[:9]}, "
        f"{len(all_files)} file(s) in range ({len(full_files)} in the whole PR), budget left ${ledger.remaining:.4f}"
    )

    # Recomputed from the whole PR each round, so a reverted change drops its note.
    state.advisories = advisory.notes(advisory.changed_paths(full_files))

    # ---- head text for changed files (data only; never executed) -----------
    head_text = head_side_text(gh, full_files, head, cfg.max_changed_files)

    # ---- free resolution: the quoted code is gone ------------------------
    touched = {fd.path for fd in all_files}
    pr_diffs = {fd.path: fd for fd in full_files}
    for f in state.open_findings():
        if not quote_stored(f):
            continue  # no quote to match (the state outgrew a comment): verify judges it
        text = head_text.get(f.path)
        if text is None and f.path not in touched:
            continue  # file untouched since: finding still stands
        if not text or not snippet_in_text(text, f.evidence):
            pr_fd = pr_diffs.get(f.path)
            if pr_fd is not None and in_removed_lines(pr_fd, f.evidence):
                # A finding about code the PR removes quotes what is absent at
                # the head by design: absence is not a fix while the PR still
                # removes it. Verify judges it.
                continue
            f.status, f.fixed_round, f.fixed_by = "fixed", round_no, "code-gone"
            res.resolved_free.append(f.id)

    if res.resolved_free:
        trace.line(
            f"resolved for free (quoted code gone): {', '.join(res.resolved_free)}"
        )

    # ---- scope -----------------------------------------------------------
    sel = select_files(all_files, exclude=tuple(DEFAULT_EXCLUDE) + tuple(cfg.exclude))
    # The weakened-expectation facts are read from the round's test files before
    # selection, which skips a test file deleted outright.
    tests_changed = changed_tests(all_files, cfg)
    weakened = (
        code_facts.weakened_expectations(tests_changed) if cfg.facts.regression else []
    )
    weakened_paths = {e.path for e in weakened}
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
    if not bundles and weakened_paths:
        # No line review to carry the facts: a test deleted outright has no line
        # to comment on. It must not pass unseen, so it counts as unread.
        res.skipped_files[:] = [
            (p, WEAKENED_UNREVIEWED if p in weakened_paths else why)
            for p, why in res.skipped_files
        ]
    # Source that was meant to be read and was not can never give a green verdict.
    state.unread = dict(unread_skips(res.skipped_files))
    if state.unread:
        trace.line(
            f"unread: {len(state.unread)} file(s) the review could not read — the verdict "
            "is incomplete until they are read or waived"
        )
    with trace.group("2 · scope: files, triage, bundles"):
        for fd in sel.reviewed:
            trace.line(
                f"selected  {fd.path} ({fd.status}, +{fd.additions}/-{fd.deletions})"
            )
        for path, why in sel.skipped:
            trace.line(f"skipped   {path}: {why}")
        withheld = res.triage.withheld()
        for reason, paths in res.triage.mechanical.items():
            whole = withheld.get(reason, [])
            part = [p for p in paths if p not in whole]
            for label, ps in (
                ("mechanical, not sent", whole),
                ("mechanical hunks set aside, the rest reviewed", part),
            ):
                if ps:
                    trace.line(
                        f"{label} ({reason}): {', '.join(ps[:10])}{' …' if len(ps) > 10 else ''}"
                    )
        for dup, rep_path in res.triage.duplicate_of.items():
            trace.line(f"duplicate {dup}: same change as {rep_path} (reviewed once)")
        if res.triage.renames:
            trace.line(
                "renames: " + ", ".join(f"{a}->{b}" for a, b in res.triage.renames)
            )
        for b in bundles:
            applied = ", ".join(p.id for p in principles.for_paths(b.paths)) or "none"
            trace.line(
                f"bundle {b.label!r}: {len(b.files)} file(s), {b.changed_lines} changed lines, "
                f"~{b.diff_tokens()} diff tokens, principles {applied}: {', '.join(b.paths)}"
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
        sdk=sdk,
    )
    ws.sdk_pins = sdk_pins_block(ws, lock, lambda p: gh.raw_file_at(p, head))
    round_cap = ledger.remaining * (cfg.first_round_share if round_no == 1 else 1.0)
    # The approach check runs alongside the line review, on a ledger of its own
    # carved out of the round's: whatever it spends, it can never take a
    # bundle's share and leave that bundle unfinished.
    run_approach = bool(cfg.approach and (not state.approach or force) and sel.reviewed)
    approach_cap = round_cap * APPROACH_SHARE if run_approach else 0.0
    # The skeptic pass, likewise, on a ledger of its own: a refutation check can
    # never spend a bundle's share, and a bundle can never leave it none.
    run_skeptic = bool(cfg.skeptic.enabled and bundles)
    skeptic_cap = round_cap * SKEPTIC_SHARE if run_skeptic else 0.0
    round_ledger = Ledger(cap_usd=round_cap - approach_cap - skeptic_cap)
    approach_ledger = Ledger(cap_usd=approach_cap)
    skeptic_ledger = Ledger(cap_usd=skeptic_cap)
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

    # ---- what this PR itself added, against the merge base (FND-4163) -------
    # An incremental round sees only the commits since the last review, so code
    # an earlier commit of this PR added reads as pre-existing. A compatibility
    # claim about it is capped in code (agent.calibrate); both prompts are told.
    with trace.group("3b · added by this PR (merge base vs PR head)"):
        added = pr_added(gh, base, head, full_files, head_text)
        trace.line(
            f"{len(added.paths)} added path(s), "
            f"{sum(len(v) for v in added.symbols.values())} added symbol(s) in existing files"
        )
    pr_meta["paths_added"] = added.render_paths()
    pr_meta["symbols_added"] = added.render_symbols()

    # ---- regression and history facts (0 model calls) ----------------------
    # Computed in code and handed to every bundle's prompt (facts.py). The
    # history half reads the base branch's recent commits through the REST API,
    # within hard caps; a failure there means fewer facts, never an incomplete review.
    if bundles or weakened:
        t_facts = time.monotonic()
        with trace.group("3c · facts: regression and history (computed in code)"):
            fx = compute_facts(
                gh,
                number,
                pr,
                base,
                ws,
                _whole_files([fd for b in bundles for fd in b.files]),
                cfg,
                tests_changed=tests_changed,
            )
            res.timings_ms["facts"] = int((time.monotonic() - t_facts) * 1000)
            trace.line(f"regression: {fx.regression_line()}")
            trace.line(
                f"history: {fx.history_line()}; {fx.history.requests} request(s), "
                f"{res.timings_ms['facts']} ms"
            )
            for n in fx.history.notes:
                trace.line(f"history: {n}")
        pr_meta["facts"] = fx
        state.facts = {
            "regression": fx.regression_line(),
            "history": fx.history_line(),
            "head": head,
            "history_requests": fx.history.requests,
        }

    # ---- verify still-open findings (1 call) -------------------------------
    # Every open finding, not only those whose own file changed: a fix often lands
    # in another file (a data file's finding fixed in the code that reads it). The
    # model also sees what changed this round, since the fix may not be at the quote.
    if cfg.verify and round_no > 1:
        # A round that changed no PR file (a merge from the base branch alone) has
        # nothing new to judge, so it skips verify — unless `/review force` asked for a
        # re-check, which then judges the open items against the whole PR's change.
        # A file the author changed counts even when line review set its change
        # aside with a base merge's (a restoration of base lines, a deletion).
        round_files = list(
            {**{fd.path: fd for fd in all_files}, **verify_range}.values()
        )
        recheck = bool(round_files) or force
        verify_files = round_files if round_files else full_files
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
                    round_diff(verify_files, to_verify, exclude_patterns(cfg)),
                    concerns,
                    removed=removed_paths(full_files),
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
    # ---- approach check: once per PR, ALONGSIDE the line review --------------
    # It runs in its own thread next to the bundles (at most concurrency + 1 calls
    # in flight), so it no longer adds its time to the review's. The price: on the
    # round it runs, the bundles do not see its reading of the PR's intent. It is
    # stored in state and reused on every later invocation, where the bundles do
    # see it — a re-review never pays to re-understand the PR (only `/review
    # force` redoes it). Advisory only: it never changes the status.
    pr_meta["mechanical"] = res.triage.summary_lines() + [
        f"rename: {a} -> {b}" for a, b in res.triage.renames
    ]
    pr_meta["understanding"] = holistic.understanding_from_state(state.approach or {})
    approach_client: Client | None = None
    approach_pool = ThreadPoolExecutor(max_workers=1) if run_approach else None
    approach_job: Future[tuple[holistic.ApproachCheck, int]] | None = None
    if approach_pool is not None:
        side: Client = client_factory(approach_ledger)
        approach_client = side
        trace.line(
            "approach check: running alongside the line review "
            f"(its own budget ${approach_cap:.4f})"
        )
        approach_job = approach_pool.submit(
            _approach,
            side,
            ws,
            full_files if incremental else sel.reviewed,
            dict(pr_meta),
            exclude_patterns(cfg),
        )
    elif state.approach:
        trace.line("approach check: reused from an earlier round (not re-run)")

    # ---- review ----------------------------------------------------------
    confirmed = [f for f in state.findings if f.status == "open"]

    # Each bundle may spend its share of what is left (by size of real change),
    # so one large bundle can never starve the others into an incomplete review.
    left = round_ledger.remaining
    weights = {b.label: max(b.changed_lines, 20) for b in bundles}
    total_w = sum(weights.values()) or 1
    # The code the app ships first, tests and everything else after (FND-5316):
    # a code bundle that ends on an empty turn gets its retry (agent._retry_final)
    # before any test bundle spends a cent, and test bundles share what is left.
    code, rest = split_code_bundles(bundles)

    def one(b: Bundle, budget: float, is_code: bool) -> BundleResult:
        return review_bundle(
            client,
            ws,
            b,
            principles,
            pr_meta,
            confirmed,
            cfg.limits,
            reflect=cfg.reflect,
            budget_usd=budget,
            added=added,
            retry_empty=is_code,
        )

    with trace.group(f"6 · line review: {len(bundles)} bundle(s)"):
        trace.line(
            f"up to {cfg.concurrency} bundles in parallel; lines are prefixed [bundle]; "
            f"{len(code)} app-code bundle(s) first, then {len(rest)} other(s)"
        )
        with ThreadPoolExecutor(max_workers=max(1, cfg.concurrency)) as pool:

            def first(b: Bundle) -> BundleResult:
                return one(b, left * weights[b.label] / total_w, True)

            res.bundles = list(pool.map(first, code))
            after = round_ledger.remaining
            rest_w = sum(weights[b.label] for b in rest) or 1

            def then(b: Bundle) -> BundleResult:
                return one(b, after * weights[b.label] / rest_w, False)

            res.bundles += list(pool.map(then, rest))
    res.timings_ms["review"] = int((time.monotonic() - t_phase) * 1000)
    if approach_job is not None and approach_pool is not None:
        with trace.group("5 · approach check (once per PR, alongside the line review)"):
            ac, ms = approach_job.result()
            approach_pool.shutdown()
            res.timings_ms["approach"] = ms
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
    if getattr(client, "fell_back", ""):
        res.notes.append(client.fell_back)
    elif approach_client is not None and getattr(approach_client, "fell_back", ""):
        res.notes.append(approach_client.fell_back)

    skeptic_client: Client | None = None

    def all_calls() -> list[dict[str, Any]]:
        own = list(getattr(client, "calls", []))
        side = list(getattr(approach_client, "calls", [])) if approach_client else []
        sk = list(getattr(skeptic_client, "calls", [])) if skeptic_client else []
        return own + side + sk

    res.calls = all_calls()
    fresh: list[Finding] = []
    for br in res.bundles:
        # A finding that is not on a diff line (unchanged code) is still a finding: it is
        # stored, counted and re-verified like any other. Every finding is delivered in the
        # summary and the verdict; connector-reviewer opens no inline review threads.
        fresh.extend(br.findings)
        fresh.extend(br.unplaced)
    # Each bundle's ids are unique already; two bundles can still meet on one
    # file. Before the base check (keyed by id) and `merge_new` (FND-5316).
    fresh = unique_ids(fresh)
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
            added=added,
            max_tokens=res.verify_max_tokens or cfg.limits.verify_max_tokens,
            exclude=exclude_patterns(cfg),
        )
        res.calls = all_calls()  # the check's call too
    res.new_findings = merge_new(state, fresh, round_no=round_no)
    # ---- skeptic: one refutation attempt per NEW blocking finding ----------
    # After merge_new, so a finding carried over from an earlier round (a known
    # id) is never re-litigated; before the verdict, so a refuted finding no
    # longer blocks. The objects are the state's own: a change lands there.
    to_doubt = skeptic.candidates(res.new_findings)
    if run_skeptic and to_doubt:
        t_sk = time.monotonic()
        sk_client: Client = client_factory(skeptic_ledger)
        skeptic_client = sk_client
        with trace.group(
            f"6c · skeptic: {len(to_doubt)} new blocking finding(s), "
            f"its own budget ${skeptic_cap:.4f}"
        ):
            res.skeptic = skeptic.run_all(
                sk_client,
                ws,
                res.new_findings,
                cfg.skeptic,
                lambda f: _cited_principle(principles, f),
                cfg.concurrency,
                # A checkout that is not the base shows no base code: an
                # unknown base never drops a finding.
                base_for=None if off_base else (lambda f: skeptic_base(ws, f)),
            )
            tally = skeptic.counts(res.skeptic)
            trace.line(
                "skeptic: " + ", ".join(f"{k} {v}" for k, v in tally.items() if v)
            )
            _drop_by_skeptic(state, res)
        res.timings_ms["skeptic"] = int((time.monotonic() - t_sk) * 1000)
        res.calls = all_calls()
    elif to_doubt:
        trace.line(
            f"skeptic: off in config; {len(to_doubt)} new blocking finding(s) not checked"
        )
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
                f"NEW {f.id} {f.severity:<8} {f.rule_id or '-':<5} {f.path}:{f.line} "
                f"{trace.short(f.title, 100)}"
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
    _book(ledger, approach_ledger)
    _book(ledger, skeptic_ledger)
    failed_paths: list[str] = []
    bundle_paths = {b.label: b.paths for b in bundles}
    for b in res.bundles:
        if b.stop == "refused":
            suspects = suspect_paths(b.refusal)
            res.incomplete.append(
                f"{b.label}: the model provider refused the request "
                f"(turn {b.refusal.get('turn', 0)}); suspect files: "
                + (", ".join(f"`{p}`" for p in suspects[:10]) or "none named")
                + (f" (+{len(suspects) - 10} more)" if len(suspects) > 10 else "")
            )
            failed_paths.extend(bundle_paths.get(b.label, []))
        elif b.stop in ("llm_error", "fatal", "budget"):
            res.incomplete.append(f"{b.label}: {b.stop} — {b.error[:200]}")
            failed_paths.extend(bundle_paths.get(b.label, []))
        elif b.unanswered:
            # No answer at all (FND-5316): its 0 findings are not a clean read,
            # so the review is incomplete and the next `/review` retries it.
            note = no_review_note(b, bundles)
            trace.line(note)
            res.incomplete.append(note)
            failed_paths.extend(bundle_paths.get(b.label, []))
    res.retried = list(state.pending_files) if retry_only else []
    # Spend is always booked. But a round in which part of the review did not
    # happen must never be recorded as a review of this head: that would make
    # the unchanged-head rule skip it and count it as a dry round — a broken
    # alias or a spent key turning into a permanent, silent "all clear".
    # What did get reviewed is kept; only the files of failed bundles are
    # remembered as pending, and the next `/review` retries exactly those.
    if res.bundles and len(failed_paths) == sum(len(b.paths) for b in bundles):
        pass  # nothing was reviewed: leave the head unreviewed so the next run redoes it all
    elif off_base:
        pass  # reviewed against the wrong code: not a review of this head either
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
            "incomplete": bool(res.failed or res.incomplete or state.unread),
            "usd": round(
                round_ledger.spent_usd
                + approach_ledger.spent_usd
                + skeptic_ledger.spent_usd,
                4,
            ),
            "calls": round_ledger.calls + approach_ledger.calls + skeptic_ledger.calls,
            "run": run_url,
        }
    )
    del state.history[:-MAX_HISTORY]  # bounded: /review force can go past max_rounds

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
    """`/review dismiss F-… <reason>`: close findings the team decided not to fix.

    No model call and no review: the findings are marked dismissed with who and
    why, the summary and the `connector-reviewer` status are refreshed, and the dismissal is a
    row in the round history. A blocking finding cannot be dismissed by the PR's
    own author — someone else has to agree it can ship."""
    state, _ = find_state(gh, number)
    if state is None:
        return RunResult(
            "skipped",
            reason="connector-reviewer has not reviewed this PR yet: nothing to dismiss",
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
        head=state.reviewed_head,
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
    head = str(((gh.pr(number) or EMPTY).get("head") or EMPTY).get("sha") or "")
    if head and state.reviewed_head and head != state.reviewed_head:
        res.notes.append(
            "There are commits since the last review; comment `/review` to review them."
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
        gh.comment(
            number, "connector-reviewer: nothing was dismissed.\n\n" + "\n".join(lines)
        )
    return res


def exemption(
    gh: GitHub, number: int, pr: dict[str, Any], diff_text: str, cfg: Config
) -> exempt.Exemption | None:
    """The class this pull request is exempt under, or None.

    Gathers what `exempt.classify` needs: the author, the branch, the author
    of every commit, and every path the diff touches (a rename's old path
    included). A commit list that cannot be read, or that is shorter than the
    pull request says it is, is "not known", which is never exempt."""
    author = str((pr.get("user") or EMPTY).get("login") or "")
    head_ref = str((pr.get("head") or EMPTY).get("ref") or "")
    try:
        authors = gh.pr_commit_authors(number)
    except GitHubError as e:
        trace.line(f"exempt: the commit list could not be read ({trace.short(e, 120)})")
        authors = []
    total = pr.get("commits")
    if isinstance(total, int) and total != len(authors):
        trace.line(
            f"exempt: {len(authors)} of {total} commits listed: authors not known"
        )
        authors = []
    paths = advisory.changed_paths(parse_unified_diff(diff_text))
    return exempt.classify(
        author=author,
        head_ref=head_ref,
        commit_authors=authors,
        paths=paths,
        cfg=cfg.exempt,
    )


def _exempt(
    gh: GitHub,
    number: int,
    head: str,
    state: PRState,
    found: exempt.Exemption,
    had_summary: bool,
    post: bool,
    run_url: str,
) -> RunResult:
    """Record the exemption and publish it: the summary, the verdict at the
    bottom when a summary was already there, and a `success` status."""
    state.exempt = {
        "class": found.kind,
        "why": found.why,
        "reminder": found.reminder,
        "head": head,
    }
    res = RunResult(
        "exempt",
        reason=f"{found.kind}: {found.why}",
        mode="exempt",
        mode_label=f"exempt · {found.kind}",
        state=state,
        run_url=run_url,
        head=head,
    )
    if post:
        url = gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))
        if had_summary:
            gh.comment(number, verdict_brief(res, url))
        gh.set_status(head, *verdict_status(res), url)
    return res


def active_exemption(st: PRState, head: str) -> dict[str, Any] | None:
    """The exemption that covers `head`, or None. Like a waive, it covers one
    commit: a later commit is classified again when someone asks."""
    ex = st.exempt or {}
    return ex if head and ex.get("class") and ex.get("head") == head else None


def _exempt_line(st: PRState) -> str:
    ex = st.exempt or {}
    if not ex.get("class"):
        return ""
    reminder = f" {ex['reminder']}" if ex.get("reminder") else ""
    return (
        f"✅ **Exempt: {ex['class']}** — {ex.get('why', '')}. It was not reviewed."
        f"{reminder} Comment `/review force` to review it anyway."
    )


def waive(
    gh: GitHub,
    number: int,
    reason: str,
    *,
    actor: str,
    post: bool = True,
    run_url: str = "",
) -> RunResult:
    """`/review waive <reason>`: set the verdict aside for the current head.

    No model call and no review. Who, why and the head sha go into the state
    and the round history; the `connector-reviewer` status on that head turns
    `success` with "waived by @login: reason"; the summary says the pull
    request was waived and that its open findings were not resolved.

    A waive belongs to one head. A new commit has no status of its own, and a
    later review of the same head (`/review force`) clears the waive and puts
    the reviewer's verdict back (`run`). Anyone who may ask for a review may
    waive, the pull request's author included: event.py decides who that is."""
    state, prior = find_state(gh, number)
    state = state or PRState()
    head = str(((gh.pr(number) or EMPTY).get("head") or EMPTY).get("sha") or "")
    if not head:
        return RunResult(
            "skipped",
            reason="the pull request's head could not be read: nothing was waived",
        )
    state.waiver = {"by": actor, "reason": reason, "head": head}
    label = f"waive · by @{actor}"
    res = RunResult(
        "waived",
        mode="waive",
        mode_label=label,
        state=state,
        run_url=run_url,
        head=head,
        blob_base=(
            f"https://github.com/{gh.repo}/blob/{state.reviewed_head}"
            if state.reviewed_head
            else ""
        ),
    )
    state.history.append(
        {
            "round": state.round,
            "head": head[:12],
            "base": "",
            "mode": "waive",
            "label": label,
            "new": 0,
            "resolved": 0,
            "blocking": len(state.open_findings(BLOCKING)),
            "incomplete": False,
            "usd": 0.0,
            "calls": 0,
            "run": run_url,
        }
    )
    del state.history[:-MAX_HISTORY]
    trace.line(f"waive by @{actor} at {head[:9]}: {trace.short(reason, 120)}")
    if not post:
        return res
    url = gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))
    if prior:  # a new summary is already the bottom comment
        gh.comment(number, verdict_brief(res, url))
    gh.set_status(head, *verdict_status(res), url)
    return res


def active_waiver(st: PRState, head: str) -> dict[str, Any] | None:
    """The waive that covers `head`, or None. A waive covers one commit."""
    w = st.waiver or {}
    return w if head and w.get("head") == head else None


def _waiver_lines(st: PRState, active: bool) -> list[str]:
    """What the summary says while a waive is on record. While it covers the
    head the status is for (`active`, the same test `verdict_status` makes), the
    status is green, and the headline says so: the verdict it set aside follows
    as detail, never as a second headline."""
    w = st.waiver or {}
    if not w.get("head"):
        return []
    left = len(st.open_findings())
    unresolved = (
        f"{left} open finding(s) were **not resolved**; they are listed below."
        if left
        else "There were no open findings."
    )
    icon = "✅" if active else "🟣"
    return [
        f"{icon} **Waived by @{w.get('by', '?')}:** {w.get('reason', '')} "
        f"(at `{str(w['head'])[:7]}`)",
        "",
        f"The reviewer's verdict was set aside for that commit. {unresolved} "
        "A later commit is not covered: it has no status until it is reviewed "
        "or waived. Without the waive, the verdict is:",
        "",
    ]


def _check_new_against_base(
    client: Client,
    ws: Workspace,
    state: PRState,
    fresh: list[Finding],
    res: RunResult,
    *,
    changes: list[FileDiff],
    removed: str,
    added: PRAdditions,
    max_tokens: int,
    exclude: tuple[str, ...] = DEFAULT_EXCLUDE,
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
                round_diff(changes, batch, exclude),
                removed=removed,
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


def _drop_by_skeptic(state: PRState, res: RunResult) -> None:
    """Remove the new findings a skeptic check dropped (`Outcome.dropped`).
    The skeptic runs after `merge_new`, so each is in the state already. The
    summary names each one and why, since it no longer has a row."""
    gone = {o.finding_id: o.dropped for o in res.skeptic if o.dropped}
    if not gone:
        return
    for f in res.new_findings:
        if f.id in gone:
            res.notes.append(
                f"The skeptic dropped {f.id} ({f.severity}, "
                f"{skeptic.clean(f.title, 120)}): {gone[f.id]}."
            )
            trace.line(f"dropped {f.id} {f.severity} {f.path}: {gone[f.id]}")
    state.findings[:] = [f for f in state.findings if f.id not in gone]
    res.new_findings = [f for f in res.new_findings if f.id not in gone]


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


# ---- /review recheck -------------------------------------------------------------

RECHECK_REASON_KEEP = 300


@dataclass
class RecheckEvidence:
    facts: list[str]  # deterministic, one line each; facts[0] is the merge-base fact
    absent_on_merge_base: bool
    code_now: str
    at_merge_base: str


def _caller_facts(idx: Index, sym: Symbol, added: PRAdditions) -> list[str]:
    """Who can call `sym` by name at the PR head, resolved by scope: a nested
    symbol's callers sit inside its enclosing function, so the facts name that
    function's callers too; a same-name call elsewhere is never listed."""

    def listing(callers: list[Symbol]) -> str:
        return (
            ", ".join(
                f"{c.path}:{c.start} {c.qualname.split(':')[-1]}"
                + (" (added by this PR)" if added.added(c.path, c) else "")
                for c in callers[:8]
            )
            or "none"
        )

    got = idx.scoped_callers(sym)
    dotted = sym.qualname.split(":", 1)[1]
    unrelated = (
        f"; {got.unrelated} same-name call{'s' * (got.unrelated != 1)} elsewhere "
        "unrelated: no import reaches it"
        if got.unrelated
        else ""
    )
    if got.local_to is not None:
        outer = got.local_to.qualname.split(":", 1)[1]
        return [
            f"local symbol: {dotted} is defined inside {outer}, so no code outside "
            f"it can call it by name (callers inside {outer}: "
            f"{listing(got.callers)}{unrelated})",
            *_caller_facts(idx, got.local_to, added),
        ]
    untyped = (
        f"; {got.untyped} more .{sym.name}() call{'s' * (got.untyped != 1)} on a "
        "receiver the index cannot type"
        if got.untyped
        else ""
    )
    reach = (
        f"on a receiver typed as {got.owner.name}"
        if got.owner
        else "in its file or importing it"
    )
    return [
        f"callers of {dotted} at the PR head ({len(got.callers)} {reach}"
        f"{unrelated}{untyped}): {listing(got.callers)}"
    ]


def recheck_evidence(
    ws: Workspace,
    f: Finding,
    added: PRAdditions,
    merge_base_text: Callable[[str], str | None],
) -> RecheckEvidence:
    """What git and the repository say about one finding's subject, with no
    model call: merge-base existence and callers."""
    text = ws.text(f.path) or ""
    lines = text.splitlines()
    at = locate_in_text(text, f.evidence)
    if _new_copy_of_removed(ws, f, at):
        at = 0
    # Code the PR removes is absent at the head by design, as in verify: show
    # where it was removed, and say so, so absence is not read as a fix.
    removed_at = None if at else _removed_site(ws, f)
    site = at or removed_at or 0
    near = ws.index.enclosing(f.path, site) if site else None
    lo, hi = (
        (near.start, near.end)
        if near and near.end - near.start < 120
        else (max(site - 15, 1), site + 15)
    )
    code_now = (
        "\n".join(f"{i:>5} {lines[i - 1]}" for i in range(lo, min(hi, len(lines)) + 1))
        if site
        else _quote_gone(f)
    )
    if removed_at is not None:
        code_now = (
            f"{REMOVED_NOTE}; the site it was removed from, as it is now:)\n{code_now}"
            if site
            else f"{REMOVED_NOTE}; the PR deletes the whole file)"
        )
    # The symbol around a removal site is a neighbour of the removed code, not
    # its subject: it frames <code_now> only, and no merge-base or caller fact.
    sym = near if removed_at is None else None
    fd = ws.diffs.get(f.path)
    base_path = fd.old_path if fd and fd.status == "renamed" and fd.old_path else f.path
    dotted = sym.qualname.split(":", 1)[1] if sym else ""
    facts: list[str] = []
    absent = False
    at_base = ""
    why = "" if removed_at is not None else added.added(f.path, sym)
    if why:
        facts.append(why)
        absent = True
    else:
        # A file renamed from `.env`: its merge-base copy is the secret file itself.
        base = None if secret_path(base_path) else merge_base_text(base_path)
        if base is None:
            facts.append(f"merge-base copy of {base_path} could not be read")
        elif removed_at is not None:
            # Never "absent": the PR's own diff removes this code.
            facts.append(
                f"present on merge base: the quoted code is in {base_path}, and "
                "this PR removes it: its absence at the PR head is not a fix"
                if snippet_in_text(base, f.evidence)
                else f"the quoted code is code this PR removes, but it is not in "
                f"the merge-base copy of {base_path}: its absence at the PR head "
                "is not a fix"
            )
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
                (
                    f"the quoted code is no longer in {f.path} at the PR head"
                    if quote_stored(f)
                    else QUOTE_NOT_STORED
                )
                + ": merge-base presence of its symbol unknown"
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
    if sym is not None:
        facts += _caller_facts(ws.index, sym, added)
    return RecheckEvidence(facts, absent, code_now, at_base or "(none)")


# Files GitHub Actions reads steps from: a workflow (top level only) or an action.
_ACTIONS_FILE = re.compile(
    r"^\.github/(?:workflows/[^/]+|actions/(?:.+/)?action)\.ya?ml$"
)
# `repository: owner/name` on a step (checkout's `with:`), optionally quoted. The
# key must open its line: only indentation, a list dash, or code_now's line-number
# prefix may precede it, so a comment (`# repository: o/decoy`) is never a target.
# The charset is GitHub's own, so a name cannot carry a tag, a path or an expression.
_CHECKOUT_TARGET = re.compile(
    r"^(?:[ \t]*\d+ )?[ \t]*(?:-[ \t]+)?repository:[ \t]*"
    r"[\"']?([A-Za-z0-9-]+/[A-Za-z0-9_.-]+?)(?:\.git)?[\"']?[ \t]*(?:#.*)?$",
    re.MULTILINE,
)
MAX_CHECKOUT_TARGETS = 3


def checkout_target_facts(
    path: str, texts: list[str], visibility: Callable[[str], str | None]
) -> list[str]:
    """For a workflow or action file, the GitHub API visibility of each repo a
    step checks out (`repository: owner/name`) in `texts`, at most
    MAX_CHECKOUT_TARGETS of them. Any other file: no facts and no lookup. A
    failed lookup is stated as unknown, never dropped and never fatal."""
    if not _ACTIONS_FILE.match(path):
        return []
    targets: list[str] = []
    for text in texts:
        for m in _CHECKOUT_TARGET.finditer(text):
            name = m.group(1)
            if name.split("/")[1] not in (".", "..") and name not in targets:
                targets.append(name)
    out: list[str] = []
    for name in targets[:MAX_CHECKOUT_TARGETS]:
        vis = visibility(name)
        out.append(
            f"checkout target {name}: visibility {vis} (GitHub API)"
            if vis
            else f"checkout target {name}: visibility unknown (the GitHub API lookup failed)"
        )
    return out


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
) -> tuple[dict[str, tuple[str, str]] | None, bool]:
    """One model call over every finding that needs one: (id -> (status, reason),
    cut_off). The verdicts are None when none came back (budget, transport, or out
    of output tokens); cut_off is True when the call stopped at its output budget
    before answering for every finding, which `/review recheck verify-budget` can fix."""
    parts: list[str] = []
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
            cache_key="connector-reviewer-recheck",
        )
    except (BudgetExhausted, LLMError) as e:
        trace.line(f"recheck: no verdict — {trace.short(str(e), 200)}")
        return None, False
    capped = _out_of_output_budget(comp, max_tokens)
    if not comp.tool_calls:
        return None, capped
    ids = {f.id for f, _ in items}
    out: dict[str, tuple[str, str]] = {}
    for it in verdict_items(comp.tool_calls):
        iid = str(it.get("id"))
        if iid in ids and it.get("status") in ("resolved", "open"):
            out[iid] = (
                str(it["status"]),
                " ".join(str(it.get("reason") or "").split()),
            )
    # A call that stopped at its cap mid tool call can carry truncated arguments
    # that parse to no verdict, or verdicts for only some findings: the rest were
    # cut off, not judged. Completed verdicts are kept either way.
    return out or None, capped and len(out) < len(ids)


def recheck_cut_off_line(
    ids: list[str], max_tokens: int, opted_in: bool, cfg: Config
) -> str:
    """Why a re-check gave no verdict, and the opt-in that gives it more room.

    The re-check's model call shares verify's output budget, and reasoning tokens
    count against it, so a hard finding can spend all of it before answering."""
    line = (
        f"The re-check spent all {max_tokens:,} output tokens before answering, so "
        f"{', '.join(ids)} {'was' if len(ids) == 1 else 'were'} not re-judged"
    )
    if opted_in:
        return line + (
            " (this was already the opt-in budget, `agent.verify_max_tokens_opt_in` "
            "in the reviewer's own `config.toml`, which lives in application-sdk)"
        )
    return line + (
        f"; comment `/review recheck verify-budget {' '.join(ids)} <reason>` to retry "
        f"with {cfg.limits.verify_max_tokens_opt_in:,}"
    )


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
    verify_budget: bool = False,
) -> RunResult:
    """`/review recheck [verify-budget] F-… [reason]`: re-judge open findings with repo-aware evidence.

    Deterministic first: the evidence (merge-base existence, callers, the
    visibility of a workflow's checkout targets) is gathered with no model call,
    and a compatibility finding whose subject is absent on the merge base is
    resolved on that alone.
    Anything else gets one model call, from what is left of the PR's $ cap.
    It is not a round: `state.round` and `reviewed_head` do not move, so the
    round cap never blocks it and the next `/review` still reviews any new commits.
    Who may ask is decided in event.py; the PR author may."""
    state, _ = find_state(gh, number)
    if state is None:
        return RunResult(
            "skipped",
            reason="connector-reviewer has not reviewed this PR yet: nothing to re-check",
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
        head=state.reviewed_head,
        blob_base=(
            f"https://github.com/{gh.repo}/blob/{state.reviewed_head}"
            if state.reviewed_head
            else ""
        ),
    )
    resolved: list[tuple[Finding, str]] = []
    kept: list[tuple[Finding, str, list[str]]] = []
    spent = Ledger(cap_usd=max(ledger.remaining, 0.0))
    cut_off = False
    cut_off_note = ""
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
        ask: list[tuple[Finding, RecheckEvidence]] = []
        seen_visibility: dict[str, str | None] = {}

        def visibility(name: str) -> str | None:
            if name not in seen_visibility:
                seen_visibility[name] = gh.repo_visibility(name)
            return seen_visibility[name]

        for f in targets:
            ev = recheck_evidence(ws, f, added, read_base)
            ev.facts += checkout_target_facts(
                f.path, [f.evidence, ev.code_now], visibility
            )
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
            max_tokens = (
                cfg.limits.verify_max_tokens_opt_in
                if verify_budget
                else cfg.limits.verify_max_tokens
            )
            verdicts, cut_off = _recheck_call(client, ask, reason, max_tokens)
            if cut_off:
                res.incomplete.append("re-check: ran out of output budget")
                cut_off_note = recheck_cut_off_line(
                    [f.id for f, _ in ask if f.id not in (verdicts or {})],
                    max_tokens,
                    verify_budget,
                    cfg,
                )
            elif verdicts is None:
                res.incomplete.append("re-check: no verdict came back")
            for f, ev in ask:
                status, why = (verdicts or {}).get(f.id, ("", ""))
                if status == "resolved":
                    resolved.append(
                        (f, f"{ev.facts[0]} — {why}" if why else ev.facts[0])
                    )
                else:
                    kept.append(
                        (
                            f,
                            why
                            or (
                                "not re-judged: the re-check ran out of output budget"
                                if cut_off
                                else "no verdict came back"
                            ),
                            ev.facts,
                        )
                    )
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
    if cut_off_note:
        lines.append(f"- ⚠️ {cut_off_note}")
    if not targets:
        gh.comment(
            number, "connector-reviewer: nothing was re-checked.\n\n" + "\n".join(lines)
        )
        return res
    url = gh.upsert_comment(number, SUMMARY_MARKER, render_summary(res))
    gh.comment(number, "\n".join([verdict_brief(res, url), "", *lines]))
    if state.reviewed_head:
        gh.set_status(state.reviewed_head, *verdict_status(res), url)
    return res


# Review order for the standard connector app layout, riskiest first. A path in
# none of the tiers (docs, a README, atlan.yaml) ranks after all of them.
RISKY_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("app/",),  # the connector's own source
    ("contract/",),  # the Pkl contract
    ("Dockerfile", "pyproject.toml"),  # the image and the dependencies
    (".github/",),  # CI
    ("tests/",),
)
# Generated from the contract, so never ranked as the connector's source.
GENERATED_PREFIX = "app/generated/"


def risk_rank(path: str) -> int:
    """The tier of RISKY_PREFIXES `path` falls in (0 is the riskiest), or
    `len(RISKY_PREFIXES)` for a path in none of them."""
    if not path.startswith(GENERATED_PREFIX):
        for rank, prefixes in enumerate(RISKY_PREFIXES):
            if path.startswith(prefixes):
                return rank
    return len(RISKY_PREFIXES)


def _risk(b: Bundle) -> tuple[int, int]:
    """A bundle ranks as its riskiest file; larger changes first within a tier."""
    rank = min((risk_rank(p) for p in b.paths), default=len(RISKY_PREFIXES))
    return (rank, -b.changed_lines)


# The tier tests rank in: below it is the code the app ships (app, contract,
# build files, CI); from it on, tests and everything else.
_TESTS_RANK = RISKY_PREFIXES.index(("tests/",))


def split_code_bundles(bundles: list[Bundle]) -> tuple[list[Bundle], list[Bundle]]:
    """(the bundles that hold code the app ships, the rest), each in its
    given order. A bundle ranks as its riskiest file, so app code packed with
    its tests is code. `review.run` reviews the first group to the end, a
    retry of an empty final turn included, before the second starts."""
    code = [b for b in bundles if _risk(b)[0] < _TESTS_RANK]
    return code, [b for b in bundles if _risk(b)[0] >= _TESTS_RANK]


NO_REVIEW_FILES_SHOWN = 5  # files named in a no-review note


def no_review_note(result: BundleResult, bundles: list[Bundle]) -> str:
    """The incomplete reason for a bundle that ended with no answer at all
    (`BundleResult.unanswered`): its 0 findings mean it was not read to a
    conclusion, not that it is clean, and its files are retried."""
    paths = next((b.paths for b in bundles if b.label == result.label), [])
    shown = ", ".join(f"`{p}`" for p in paths[:NO_REVIEW_FILES_SHOWN])
    if len(paths) > NO_REVIEW_FILES_SHOWN:
        shown += f" and {len(paths) - NO_REVIEW_FILES_SHOWN} more"
    kind = "App-code bundle" if result.retried else "Bundle"
    how = (
        "its final turn and the retry both gave no answer"
        if result.retried
        else "its last turn gave no answer"
    )
    return (
        f"{kind} `{result.label}` got no real review: {how}, so its 0 findings do "
        f"not mean it is clean ({shown}); `/review` retries it"
    )


# Every skip reason past the cap starts with this (`UNREAD_REASONS`): a state
# written by an earlier version says "bundle cap (lowest-risk code, …)".
BUNDLE_CAP = "bundle cap"
MAX_RANGES_SHOWN = 6  # line ranges named per partly reviewed file


def plan_bundles(files: list, cfg: Config, res: RunResult) -> list[Bundle]:
    """Bundles in review order — riskiest first — within `max_bundles`.

    Past the cap, bundles are first re-packed larger (up to half the context
    ceiling) so every file still gets reviewed. If that is still over the cap,
    the code the app ships (app, then contract, build files and CI) and the
    rest (tests, then anything else) are packed apart, in that order
    (FND-5316): a test file no longer rides in the bundle of the source it
    tests, where its tokens took the room that source's own code needed, so
    whatever the cap cuts is tests and other files before any app code. Each
    skipped file is named in the summary, with the lines left unread when only
    part of it was cut."""
    bundles = group(files)
    if len(bundles) > cfg.max_bundles:
        total = sum(b.diff_tokens() for b in bundles)
        bigger = min(
            max(total // cfg.max_bundles + 1, 9000),
            cfg.limits.context_limit_tokens // 2,
        )
        bundles = group(files, max_diff_tokens=bigger, max_files=12)
        if len(bundles) > cfg.max_bundles:
            diffs = cast("list[FileDiff]", files)
            shipped = [f for f in diffs if risk_rank(f.path) < _TESTS_RANK]
            rest = [f for f in diffs if risk_rank(f.path) >= _TESTS_RANK]
            bundles = [
                b
                for pool in (shipped, rest)
                for b in group(pool, max_diff_tokens=bigger, max_files=12)
            ]
    # The pools never share a bundle, so this keeps every shipped bundle first.
    bundles.sort(key=_risk)
    kept, cut = bundles[: cfg.max_bundles], bundles[cfg.max_bundles :]
    res.skipped_files.extend(_cap_skips(kept, cut, cfg.max_bundles))
    return kept


def _cap_skips(
    kept: list[Bundle], cut: list[Bundle], cap: int
) -> list[tuple[str, str]]:
    """Each file past the cap, once, with why: a file reviewed in parts
    (`bundle._parts`) can have parts on both sides of the cap, and then the
    lines left unread are named."""
    read = {f.path for b in kept for f in b.files}
    unread: dict[str, list[tuple[int, int]]] = {}
    for b in cut:
        for f in b.files:
            unread.setdefault(f.path, []).extend(
                (h.new_start, h.new_start + max(h.new_len, 1) - 1) for h in f.hunks
            )
    out: list[tuple[str, str]] = []
    for path, spans in unread.items():
        if path in read:
            out.append(
                (
                    path,
                    f"{BUNDLE_CAP}: {_line_ranges(spans)} not reviewed, the rest was",
                )
            )
        else:
            out.append((path, f"{BUNDLE_CAP}: not reviewed, past the {cap}-bundle cap"))
    return out


def _line_ranges(spans: list[tuple[int, int]]) -> str:
    """`lines 120-480, 900-1250` from hunk spans (head side), merged and capped."""
    merged: list[list[int]] = []
    for a, b in sorted(spans):
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    shown = [f"{a}-{b}" if a != b else f"{a}" for a, b in merged[:MAX_RANGES_SHOWN]]
    more = len(merged) - MAX_RANGES_SHOWN
    word = "line" if len(merged) == 1 and merged[0][0] == merged[0][1] else "lines"
    return f"{word} {', '.join(shown)}" + (
        f" and {more} more range(s)" if more > 0 else ""
    )


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
        and not secret_diff(fd)
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
    out: list[str] = []
    if added.paths:
        out.append(
            f"<paths_added_by_this_pr>\n{added.render_paths()}\n</paths_added_by_this_pr>"
        )
    if added.symbols:
        out.append(
            f"<symbols_added_by_this_pr>\n{added.render_symbols()}\n</symbols_added_by_this_pr>"
        )
    return out


def round_diff(
    files: list[FileDiff],
    open_: list[Finding],
    exclude: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE,
) -> str:
    """What changed this round, for the verify call: files holding an open finding
    first, then the rest, within a fixed budget (a cached, bounded prompt).

    `files` is the round's range or the whole PR, never filtered by selection,
    so a change that may hold secrets and an excluded file are dropped here."""
    files = model_visible(files, exclude)
    owners = {f.path for f in open_}
    out: list[str] = []
    used = 0
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


QUOTE_NOT_STORED = (
    "this finding's quoted code was not stored (the review state outgrew one "
    "comment), so where it sits now is unknown"
)


def _quote_gone(f: Finding) -> str:
    """What `<code_now>` says when the quote is not found: gone from the file,
    or never stored, which says nothing about the code."""
    if quote_stored(f):
        return "(the quoted code is not in this file any more)"
    return f"({QUOTE_NOT_STORED}: judge it from its title and body)"


def _current_line(ws: Workspace, f: Finding, text: str) -> int:
    """Where the finding's quoted code is NOW. Stored line numbers are from an
    earlier head and drift as commits land; the quote is what identifies the site,
    so when it is gone the model is told so instead of shown unrelated code."""
    return locate_in_text(text, f.evidence)  # 0 = the quote is gone; never guess a line


REMOVED_NOTE = (
    "(the quoted code is code this PR removes, and the finding is about that "
    "removal: its absence is not a fix"
)


def _removed_site(ws: Workspace, f: Finding) -> int | None:
    """For a finding that quotes code the PR still removes: the PR-head line its
    deletion sits at (0 when the file is deleted whole). None otherwise."""
    fd = ws.diffs.get(f.path)
    if fd is None or not in_removed_lines(fd, f.evidence):
        return None
    span = anchor(fd, f.evidence)
    return span[0] if span else 0


def _new_copy_of_removed(ws: Workspace, f: Finding, at: int) -> bool:
    """Whether the head match at `at` is a copy the PR added of code it removes.

    A short quote such as `return x` can survive in a newly added function
    after the original is deleted. That copy is not the quoted code still
    being there, so the removal stays the finding's subject."""
    fd = ws.diffs.get(f.path)
    return (
        bool(at)
        and fd is not None
        and at in fd.added_lines
        and in_removed_lines(fd, f.evidence)
    )


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
    if rel and secret_path(rel):
        # A file renamed from `.env`: the base copy is the secret file itself.
        return f"(the base copy of {rel} is not shown: it may hold secrets)"
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
            if not quote_stored(f):
                return f"({QUOTE_NOT_STORED})"
            return f"absent on base: the quoted code is not in {rel}"
        return f"{rel}: module level\n{_numbered(text, at - 15, at + 15)}"
    dotted = head_sym.qualname.split(":", 1)[1]
    got = base_symbol_excerpt(root, rel, text, f, head_sym, head_line)
    return f"{rel}: {dotted}\n{got}" if got else f"absent on base: {dotted} in {rel}"


def skeptic_base(ws: Workspace, f: Finding) -> str:
    """`f`'s enclosing function or quoted lines as they are on the base branch,
    for the skeptic to compare against (FND-5316); "" when there is nothing to
    compare. Code the PR removes (`REMOVED_NOTE`) and a new file, symbol or
    line have no base counterpart, and a base copy that cannot be read, may
    hold secrets or has no stored quote is unknown: none of them can be "the
    same on the base"."""
    text = ws.text(f.path) or ""
    at = _current_line(ws, f, text)
    if not at or _new_copy_of_removed(ws, f, at):
        return ""  # removed code, or a quote that is gone: no counterpart
    got = at_base_branch(ws, f, ws.index.enclosing(f.path, at), at)
    # Every answer that is not an excerpt starts "absent on base" or "(".
    return "" if got.startswith(("absent on base", "(")) else got


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
    verify (the checkout on disk) and `/review recheck` (the merge base via the API)."""
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
    added: PRAdditions | None = None,
    max_tokens: int = AgentLimits.verify_max_tokens,
) -> Verified:
    """Every open finding, VERIFY_BATCH at a time, and the open approach concerns
    (with the first batch). The paths the PR removes, what it adds, then the
    round's changes, lead each call, so the batches share one cached prefix.
    Returns the ids judged fixed, and the ids no verdict came back for because a
    call ran out of output budget first."""
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
        # Code the PR removes is absent at the head by design: show where it
        # was removed, and say so, so absence is not read as a fix.
        removed_at = None if at else _removed_site(ws, f)
        at = at or removed_at or 0
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
            else _quote_gone(f)
        )
        if removed_at is not None:
            code = (
                f"{REMOVED_NOTE}; the site it was removed from, as it is now:)\n{code}"
                if at
                else f"{REMOVED_NOTE}; the PR deletes the whole file)"
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
            cache_key="connector-reviewer-verify",
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
    for tc in objects(tool_calls):
        fn = tc.get("function")
        args = (
            parse_args(str(cast("dict[str, Any]", fn).get("arguments") or ""))
            if isinstance(fn, dict)
            else {}
        )
        items = args.get("items")
        if isinstance(items, list):
            out += objects(items)
    return out


def _out_of_output_budget(comp: Any, max_tokens: int) -> bool:
    """The call stopped at its output cap: `length` on Chat Completions,
    `incomplete` on Responses, or every allowed token spent."""
    used = int((comp.usage or EMPTY).get("completion_tokens") or 0)
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
    "Fix them and comment `/review`, or close one the team won't fix with "
    "`/review dismiss <id> <reason>`."
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
            f"| {'after ' if h.get('mode') in ('dismiss', 'waive') else ''}{h['round']} | {run_kind} | {span} | {h.get('new', 0)} | {h.get('resolved', 0)} "
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
    if res.mode == "waive":
        return f"waive after round {round_no} · {label.removeprefix('waive · ')}"
    if res.mode == "exempt":
        return label.replace("exempt · ", "not reviewed · exempt: ")
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
    out: list[tuple[str, list[int]]] = []
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
    cs: list[dict[str, Any]] = ap.get("concerns") or []
    still = [(i, c) for i, c in enumerate(cs, 1) if c.get("status", "open") == "open"]
    done = [(i, c) for i, c in enumerate(cs, 1) if c.get("status") == "addressed"]
    out: list[str] = []
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


def _cited_principle(principles: PrincipleSet, f: Finding) -> str:
    """The principle `f` cites, rendered as the line review saw it; "" for none."""
    for p in principles.for_paths([f.path]):
        if p.id == f.rule_id:
            return p.render([f.path])
    return ""


def _contested(f: Finding) -> str:
    """The contested mark shown after a finding's title; "" when not contested."""
    return f" ⚖️ contested — {f.contested}" if f.contested else ""


def _principle(f: Finding) -> str:
    """The principle a finding rests on, as shown in the summary: its id (FPnn
    from the fleet set, RPnn from the repository's own), or a note that it is a
    concrete bug, which needs no principle (`agent.cite`)."""
    return f"`{f.rule_id}`" if f.rule_id else "— (concrete bug)"


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
    (what used to be an inline comment; connector-reviewer opens no review threads). `chars` caps the
    text for a PR too large to show every finding in full."""
    ext = "." + f.path.rsplit(".", 1)[-1] if "." in f.path else ""

    def cap(text: str) -> str:
        return (
            text
            if chars is None or len(text) <= chars
            else text[:chars].rstrip() + " …"
        )

    out = [
        f"<details><summary>{_SEV_ICON[f.severity]} {f.id} · {f.severity} · {f.category} · "
        f"{_principle(f)} — {f.title}{_contested(f)}</summary>\n",
        f"**Where:** {_where(f, blob_base)}\n",
    ]
    if f.contested:
        out.append(
            "**⚖️ Contested:** raised as blocking; an independent check of its causal chain "
            "against the code did not confirm every step, so it is medium and does not block. "
            f"Its reason: {f.contested}\n"
        )
    out.append(cap(f.body.strip()))
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


UNREAD_HINT = (
    "Part of the change could not be read (listed below: too large, past the bundle cap, or a "
    "deleted test with no line to review), so a retry would not read it either: split it into "
    "smaller pull requests, or, once a person has reviewed those files, comment "
    "`/review waive <reason>`."
)
# Said once wherever a file past the bundle cap is listed (FND-5316).
CAP_NOTE = (
    "the bundle cap: a run line-reviews a set number of bundles of the diff, app code first, "
    "then the contract, build files and CI, then tests and other files"
)
MAX_UNREAD_LISTED = 20  # unread files named in the incomplete reason
MAX_SKIPPED_LISTED = 50  # skipped files named in the summary's list


def incomplete_reasons(res: RunResult) -> list[str]:
    """Why the review of this head is incomplete, from the run and from the state.

    The state's part (files left unreviewed by a failure, files too large to
    read) survives a dismissal or a re-check, which build a fresh result: an
    incomplete review stays incomplete until a review covers those files."""
    st = res.state or PRState()
    out = list(res.incomplete)
    if st.pending_files and not res.incomplete:
        out.append(
            f"{len(st.pending_files)} file(s) could not be reviewed in the last run: "
            "comment `/review` to retry them"
        )
    if st.unread:
        items = list(st.unread.items())
        listed = ", ".join(f"`{p}` ({why})" for p, why in items[:MAX_UNREAD_LISTED])
        more = (
            f' (+{len(items) - MAX_UNREAD_LISTED} more, under "file(s) not reviewed")'
            if len(items) > MAX_UNREAD_LISTED
            else ""
        )
        capped = any(why.startswith(BUNDLE_CAP) for _, why in items)
        out.append(
            f"{len(items)} file(s) not read: {listed}{more}"
            + (f". Past {CAP_NOTE}" if capped else "")
        )
    return out


def _retry_hint(res: RunResult) -> str:
    """What an author can do about an incomplete review. A refusal by the model
    provider is deterministic: `/review` on the same content is refused again and
    spends a round, so it is never offered as the fix for one. Nor is a file too
    large to read: a retry reads the same file."""
    st = res.state or PRState()
    if st.unread and not res.incomplete and not st.pending_files:
        return UNREAD_HINT
    hint = _failure_hint(res)
    return f"{hint} {UNREAD_HINT}" if st.unread else hint


def _failure_hint(res: RunResult) -> str:
    refused = res.refused
    if not refused:
        return "Comment `/review` to retry the part that failed."
    hint = (
        "The model provider refused part of this change (`provider_rejected`): retrying with "
        "`/review` sends the same content and is refused again. Change or split the suspect files "
        "below, then push; the run report (`connector-reviewer-run.json`) lists every file and line range each "
        "refused request held."
    )
    if len(refused) < len(res.incomplete):
        hint += " For the other parts that failed, comment `/review` to retry."
    return hint


def render_summary(res: RunResult) -> str:
    st = res.state or PRState()
    blocking = st.open_findings(BLOCKING)
    by_level = {s: st.open_findings((s,)) for s in SEVERITIES}
    counts = " · ".join(
        f"{_SEV_ICON[s]} {len(by_level[s])} {_SEV_LABEL[s]}" for s in SEVERITIES
    )
    incomplete = incomplete_reasons(res)
    if incomplete:
        verdict = (
            "⚠️ **Review incomplete** — part of this change was not reviewed; this is not an all-clear. "
            + _retry_hint(res)
            + "\n\n"
            + "\n".join(f"- {r}" for r in incomplete)
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
    # Not reviewed at all: the exemption is the verdict.
    verdict = _exempt_line(st) or verdict
    waived = bool(active_waiver(st, res.head or st.reviewed_head))
    if waived:
        verdict = set_aside(verdict)
    lines = [
        SUMMARY_MARKER,
        f"### connector-reviewer summary · updated after round {st.round} ({res.mode_label or res.mode})",
        *_waiver_lines(st, waived),
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
            f"**The reviewer reads this PR as** — {ap['problem']} *How:* {ap.get('approach', '')}\n"
        )
    if ap.get("verdict") == "concerns":
        lines.extend(_concern_lines(ap, brief=False))
        lines.append("")
    elif ap.get("verdict") == "sound":
        lines.append("**Approach check** — sound.\n")
    lines.extend(_facts_lines(st, res))
    for note in st.advisories:
        lines.append(f"**Advisory** (does not block) — {note}\n")
    for sev in SEVERITIES:
        items = by_level[sev]
        if not items:
            continue
        blocks = " — blocks merge" if sev in BLOCKING else ""
        lines.append(
            f"#### {_SEV_ICON[sev]} {_SEV_LABEL[sev].capitalize()} ({len(items)}){blocks}"
        )
        lines.append("| id | where | principle | finding |\n|---|---|---|---|")
        for f in items:
            lines.append(
                f"| {f.id} | {_where(f, res.blob_base)} | {_principle(f)} | {f.title}{_contested(f)} |"
            )
        lines.append("")
    opened = st.open_findings()
    details_at = len(lines)  # the details block is sized last (see the size backstop)
    lines.extend(_resolved_section(st))
    lines.extend(_history_section(st))
    t = res.triage
    # Only files with nothing left to review: a file whose mechanical hunk was
    # set aside and whose real change was reviewed was sent (FND-5316).
    withheld = t.withheld()
    mech_files = {p for ps in withheld.values() for p in ps} | set(t.duplicate_of)
    if mech_files:
        lines.append(
            f"\n<details><summary>{len(mech_files)} file(s) with mechanical changes — "
            "proven behaviour-neutral in code, not sent to the model</summary>\n"
        )
        for reason, paths in withheld.items():
            shown = ", ".join(f"`{p}`" for p in paths[:MECHANICAL_SHOWN])
            more = len(paths) - MECHANICAL_SHOWN
            lines.append(
                f"- {reason}: {shown}" + (f" and {more} more" if more > 0 else "")
            )
        if t.duplicate_of:
            lines.append(
                f"- same change repeated in other files (reviewed once): "
                f"{len(t.duplicate_of)} file(s)"
            )
        if t.renames:
            lines.append(
                "- renames: " + ", ".join(f"`{a}` → `{b}`" for a, b in t.renames)
            )
        lines.append("\n</details>")
    if st.pending_files:
        lines.append(
            f"\n**{len(st.pending_files)} file(s) could not be reviewed this run** and will be retried "
            "by the next `/review`"
            + (
                " (a part the provider refused is refused again unless its content changes)"
                if res.refused
                else ""
            )
            + ": "
            + ", ".join(f"`{p}`" for p in st.pending_files[:20])
        )
    if res.skipped_files:
        lines.append(
            f"\n<details><summary>{len(res.skipped_files)} file(s) not reviewed</summary>\n"
        )
        lines.extend(
            f"- `{p}` — {why}" for p, why in res.skipped_files[:MAX_SKIPPED_LISTED]
        )
        if len(res.skipped_files) > MAX_SKIPPED_LISTED:
            lines.append(f"- … and {len(res.skipped_files) - MAX_SKIPPED_LISTED} more")
        if any(why.startswith(BUNDLE_CAP) for _, why in res.skipped_files):
            lines.append(f"\nPast {CAP_NOTE}.")
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
        head = _no_state_marker(head)
        for keep in (BODY_KEEP, 200, 0):
            body = head + st.encode(body_keep=keep)
            if len(body) <= COMMENT_LIMIT:
                return body
    lean = _no_state_marker("\n".join(_without_resolved(lines)) + "\n")
    body = lean + st.encode(body_keep=0)
    if len(body) <= COMMENT_LIMIT:
        return body
    # The marker, the title and the verdict (the waiver line too, when there is one).
    top = "\n".join(lines[: lines.index(verdict) + 1]) + "\n"
    return _fit_state(lean, _no_state_marker(top), st)


STATE_TOO_LARGE = (
    "\n> ⚠️ This pull request's review state is too large for one comment: the "
    "tables are not shown, and stored quoted code is shortened. Every open "
    "finding and its id are still tracked.\n"
)


def _quotes_not_stored(n: int) -> str:
    return (
        "\n> ⚠️ This pull request's review state is too large for one comment: the "
        f"tables are not shown, resolved findings are not stored, and {n} open "
        "finding(s) are stored without their quoted code. Every open finding and its "
        "id are still tracked; one without its quote stays open until a re-check "
        "judges it.\n"
    )


def _open_not_stored(n: int) -> str:
    return (
        "\n> ⚠️ This pull request's review state is too large for one comment even "
        f"without quoted code: {n} open finding(s), lowest severity first, could not "
        "be stored. If their code is still there, a later round can raise them again.\n"
    )


STATE_NOT_STORED = (
    "\n> ⚠️ This pull request's review state is too large for one comment and "
    "could not be stored: the next round reviews this pull request afresh.\n"
)


def _fewest(most: int, fits: Callable[[int], bool]) -> int | None:
    """The fewest of `most` things to leave out for `fits`, or None when even all
    of them is not enough. A bisection: the size only shrinks as more are left out.
    The value returned is always one `fits` was checked true for."""
    if most <= 0 or not fits(most):
        return None
    lo, hi = 1, most
    while lo < hi:
        mid = (lo + hi) // 2
        if fits(mid):
            hi = mid
        else:
            lo = mid + 1
    return lo


def _fit_state(lean: str, top: str, st: PRState) -> str:
    """The summary when even the lean view and the stored bodies at 0 are over
    the limit: GitHub would refuse the edit, and the next run would read the
    previous state. Cut in this order until it fits: open findings' quoted code
    to its leading lines (whole lines, so free resolution still matches), the
    view to its first lines, the quoted code to its first line, then the oldest
    resolved findings.

    Past that, only open findings are left, and their first lines can still be
    too long (a quote is one minified line). Then the largest quotes are left out,
    the fewest that fit: each such finding keeps its id, title and place, stays
    open, and is never resolved for a quote that is not there (`quote_stored`).
    With every quote out, a finding is a title (≤120 characters), a path and a few
    numbers, so only hundreds of open findings could still be over; then the
    fewest open findings are left out, lowest severity and newest first. Should
    the state be too large with none of them (only its bounded round history,
    ledger and notes are left), the summary goes without a state block: GitHub
    accepts it, and the next round starts afresh instead of reading a stale one.
    Every value returned here is within the limit."""
    for text, keep in (
        (lean, EVIDENCE_KEEP_RESOLVED),
        (top + STATE_TOO_LARGE, EVIDENCE_KEEP_RESOLVED),
        (top + STATE_TOO_LARGE, 0),
    ):
        body = text + st.encode(body_keep=0, evidence_keep=keep)
        if len(body) <= COMMENT_LIMIT:
            return body

    def fits(body: str) -> bool:
        return len(body) <= COMMENT_LIMIT

    def dropping(n: int) -> str:
        return (
            top
            + STATE_TOO_LARGE
            + st.encode(body_keep=0, evidence_keep=0, drop_resolved=n)
        )

    resolved = sum(1 for f in st.findings if f.status != "open")
    n = _fewest(resolved, lambda k: fits(dropping(k)))
    if n is not None:
        return dropping(n)

    def unquoting(n: int) -> str:
        return (
            top
            + _quotes_not_stored(n)
            + st.encode(
                body_keep=0, evidence_keep=0, drop_resolved=resolved, drop_quotes=n
            )
        )

    quoted = sum(1 for f in st.findings if f.status == "open" and quote_stored(f))
    n = _fewest(quoted, lambda k: fits(unquoting(k)))
    if n is not None:
        return unquoting(n)

    def leaving(n: int) -> str:
        return (
            top
            + _open_not_stored(n)
            + st.encode(
                body_keep=0,
                evidence_keep=0,
                drop_resolved=resolved,
                drop_quotes=quoted,
                drop_open=n,
            )
        )

    n = _fewest(len(st.open_findings()), lambda k: fits(leaving(k)))
    if n is not None:
        return leaving(n)
    return top[: COMMENT_LIMIT - len(STATE_NOT_STORED)] + STATE_NOT_STORED


_HEADLINE = re.compile(r"^\S+ \*\*(.+?)\*\*")


def set_aside(verdict: str) -> str:
    """A verdict a waive set aside: its icon and bold headline made plain, and
    the whole of it quoted, so it reads as detail under the waive's headline."""
    plain_text = _HEADLINE.sub(r"\1", verdict, count=1)
    return "\n".join(f"> {line}" if line else ">" for line in plain_text.splitlines())


def _facts_lines(st: PRState, res: RunResult) -> list[str]:
    """The regression and history lines: the facts computed in code, then what
    the line review answered about them this round (one answer per bundle that
    gave one, at most two)."""
    fx = st.facts or {}
    if not fx.get("regression") and not fx.get("history"):
        return []
    out: list[str] = []
    for key, label in (("regression", "Regression"), ("history", "History")):
        line = f"**{label}** — {fx.get(key) or 'not computed'}."
        answers = [b.answers[key] for b in res.bundles if b.answers.get(key)][:2]
        for a in answers:
            line += f" *Line review:* {plain(a)}"
        out.append(line + "\n")
    return out


def _no_state_marker(text: str) -> str:
    """The summary with the state marker's name made inert.

    Finding titles and bodies come from the model, which reads the pull
    request. Text that spelled out a state block would otherwise sit in the
    same comment as the real one. The real block is appended after this and
    is the last in the comment, which is the one `PRState.decode` reads; this
    keeps the visible text from containing a second well-formed block at all."""
    return text.replace(STATE_MARKER + ":", STATE_MARKER + "\u200b:")


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
    out: list[str] = []
    skipping = False
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
            "the reviewer's own `config.toml`, which lives in application-sdk)."
        )
    return line + (
        f" Worth opting in: comment `/review force verify-budget` to re-check them with "
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
        "nothing checked them. Comment `/review force` to re-check them against the whole "
        "PR, or `/review force verify-budget` if the last re-check ran out of output budget."
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
    review put it — often far above the latest `/review`. Every run therefore
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
        f"{icon} **connector-reviewer · {_run_title(res, st.round)}** — {description}",
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
        # The holistic review, in full: how connector-reviewer reads the change, and whether the
        # approach is the right one — not just a one-word verdict.
        lines.append("")
        concerns = ap.get("verdict") == "concerns"
        raised: list[dict[str, Any]] = ap.get("concerns") or []
        still = [c for c in raised if c.get("status", "open") == "open"]
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

    connector-reviewer opens no inline review threads: every finding, with its explanation and
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
    trace.line(f"status 'connector-reviewer' on {head[:9]}: {state} — {description}")


def verdict_status(res: RunResult) -> tuple[str, str]:
    """The `connector-reviewer` commit status: the one green/red answer for the reviewed head.

    Green means ready to merge: every finding, nits included, is fixed or was
    dismissed with a reason. Any open finding keeps it red; the description says
    whether it is blocking ("changes requested") or only medium/low ("not
    ready"). The approach check is advisory and never turns it red. An
    incomplete review is `error`, never green: part of the change was not looked
    at — a failed bundle, a file left pending, or source too large to read, and
    the state's part of that holds through a dismissal too. A head that was
    waived (`waive`) or is exempt is green whatever the reviewer found, and says
    why."""
    st = res.state or PRState()
    led = st.ledger or {}
    cost = f"${float(led.get('spent_usd', 0)):.2f}"
    waived = active_waiver(st, res.head or st.reviewed_head)
    if waived:
        # A waive is the way past a verdict, an incomplete one included.
        return (
            "success",
            f"waived by @{waived.get('by', '?')}: {waived.get('reason', '')}",
        )
    exempted = active_exemption(st, res.head or st.reviewed_head)
    if exempted:
        return "success", f"exempt: {exempted['class']}"
    incomplete = incomplete_reasons(res)
    if incomplete:
        return "error", f"review incomplete — {incomplete[0][:90]}"
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
