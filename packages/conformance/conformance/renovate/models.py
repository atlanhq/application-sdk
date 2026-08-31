"""Typed model contracts for the Renovate fleet dashboard scanner."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Category(str, Enum):
    """Renovate PR category, derived from self-managed labels (branch/title fallback)."""

    LOCK_MAINTENANCE = "lock-maintenance"
    GITHUB_ACTIONS = "github-actions"
    CONTRACT_TOOLKIT = "contract-toolkit"
    CONFORMANCE_PACKAGE = "conformance-package"
    SDK_PACKAGE = "sdk-package"
    PYTHON_DEP = "python-dep"
    UNKNOWN = "unknown"


class UpdateType(str, Enum):
    """Semver update type reported by Renovate labels."""

    MAJOR = "major"
    MINOR = "minor"
    PATCH = "patch"
    DIGEST = "digest"
    PIN = "pin"
    UNKNOWN = "unknown"


class BlockingReason(str, Enum):
    """Why an open Renovate PR has not merged."""

    AWAITING_HUMAN_REVIEW = (
        "awaiting_human_review"  # autoMergeExpected=False — by design
    )
    CHECKS_FAILING = "checks_failing"  # required CI checks red
    CHECKS_PENDING = "checks_pending"  # required CI checks still running
    BOUNDED_LOCK_REFUSAL_EXPIRED = (
        # A sub-case of CHECKS_FAILING, not an ordinary broken build. The
        # release-age driver refused this lock refresh and left a deliberately
        # un-installable uv.lock behind so a required check would hold the branch
        # (see withhold() in .github/scripts/renovate_uv_lock_bounded.py) — but the
        # window it named has since elapsed, so the thing that blocked it no longer
        # would. Nothing re-evaluates it on its own: the branch is not behind base,
        # so Renovate reuses it without re-running postUpgradeTasks, and the marker
        # committed into uv.lock carries no clock. Recovery is to delete the branch
        # and let the next Renovate pass rebuild it (recreateClosed: true in the
        # shared preset makes that safe) — which is what the reaper step in
        # renovate.yaml now does automatically, so reaching this state at all
        # means the reaper did not run. FND-782; mechanism in FND-695.
        "bounded_lock_refusal_expired"
    )
    BOUNDED_LOCK_REFUSAL_STANDING = (
        # The other half of a bounded-lock refusal: the driver stamped a reason
        # that waiting does not fix — a broken interpreter, an unsatisfiable
        # floor, a floor that was admitted and still failed, a yanked-pin
        # rollback. The reaper deliberately leaves these alone, because recycling
        # a real wedge every four hours would hide it behind a lane that looks
        # busy. A human owns the branch. Distinct from
        # BOUNDED_LOCK_REFUSAL_EXPIRED so an alarm can fire on a reaper outage
        # without also firing on every fault a human is legitimately still
        # working through. FND-909.
        "bounded_lock_refusal_standing"
    )
    MERGE_CONFLICT = "merge_conflict"  # mergeable=CONFLICTING
    NON_DEP_FILES = "non_dep_files"  # changed files outside auto-approve allowlist
    AUTOMERGE_NOT_ARMED = (
        # Eligible, every gate green, and code-owner approval is in — but
        # GitHub-native auto-merge was never enabled on the PR. With a required
        # merge queue nothing else will merge it, so it sits green-and-approved
        # forever with no visible fault. Precise signal: autoMergeRequest is null.
        "automerge_not_armed"
    )
    AUTOMERGE_STALE = (
        # Age backstop: an auto-merge-eligible, green PR still open well past when
        # the auto-approve → auto-merge → queue pipeline should have carried it.
        # Catches wedged merge queues, a down approval workflow, and failure modes
        # not modelled explicitly — without flagging freshly-opened PRs.
        "automerge_stale"
    )
    AWAITING_APPROVAL = (
        # Transient: recently became eligible and is expected to merge imminently
        # (atlan-ci approval pending, or freshly armed and awaiting the queue).
        "awaiting_approval"
    )
    UNKNOWN = "unknown"


class ChecksState(str, Enum):
    GREEN = "green"
    FAILING = "failing"
    PENDING = "pending"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PRDep:
    """One dependency a Renovate PR delivers: package name + version change.

    Parsed from the PR body's version table (primary) or the PR title
    (fallback) — see ``classify.extract_deps``. Versions are kept verbatim as
    Renovate rendered them (a constraint change may read ``>=3.20,<4``, a
    lockfile bump ``3.26.1``); consumers that need semver should parse
    defensively. ``from_version`` may be empty when only the target version is
    known (title-only parse)."""

    name: str
    from_version: str
    to_version: str


@dataclass(frozen=True)
class RenovatePR:
    """Normalised representation of a single Renovate PR."""

    number: int
    url: str
    title: str
    branch: str
    labels: list[str]
    mergeable: str  # "MERGEABLE" | "CONFLICTING" | "UNKNOWN"
    review_decision: str  # "APPROVED" | "REVIEW_REQUIRED" | "" | ...
    checks_state: ChecksState
    files: list[str]  # changed filenames
    created_at: datetime
    updated_at: datetime
    is_draft: bool
    body: str
    # Raw: GitHub-native auto-merge armed (GraphQL autoMergeRequest non-null).
    # Defaulted so pre-existing RenovatePR constructions stay valid; scan._parse_pr
    # populates it from the fetched field.
    auto_merge_enabled: bool = False
    # Raw: committedDate of the branch head. The clock the bounded-lock refusal
    # signal expires against — created_at is wrong for it, because Renovate
    # rewrites a lock branch in place across many pushes without reopening the PR.
    # None when the input predates the field (a hand-rolled `gh pr list` dump, or a
    # stored scan from before FND-782); the refusal signal then simply does not fire.
    head_committed_at: Optional[datetime] = None
    # Raw: the release-age window named by the tripwire `[options]` table in this
    # branch's uv.lock ("P3D"), or "" when the lock has no tripwire. Parsed out by
    # scan._parse_pr so the multi-hundred-KB lock text itself is never retained.
    lock_refusal_window: str = ""
    # Raw: why the driver refused, per the stamp on the tripwire line
    # ("window-empty"), or "" for an unstamped tripwire — one written before
    # FND-909, or no tripwire at all. Empty is deliberately not self-healing:
    # classify falls back to the window clock rather than assuming a reason.
    lock_refusal_reason: str = ""
    # populated by classify()
    category: Category = Category.UNKNOWN
    update_type: UpdateType = UpdateType.UNKNOWN
    auto_merge_expected: bool = False
    blocking_reason: BlockingReason = BlockingReason.UNKNOWN
    age_days: int = 0
    # populated by classify() from the PR body/title — which packages this PR
    # delivers, so downstream dashboards can join "repo is behind on tool X" to
    # the exact PR that fixes it. Empty for lock-file-maintenance PRs (they
    # carry no version table) and unparseable bodies.
    deps: tuple[PRDep, ...] = ()


@dataclass
class AutoMergeStats:
    """Auto-merge counts over a trailing time window."""

    window_days: int
    auto_merged: int  # merged + atlan-ci auto-approval signature detected
    human_merged: int  # merged without that signature
    total_merged: int


@dataclass
class RepoRenovateSummary:
    open_total: int
    needs_human: int  # autoMergeExpected=False
    auto_merge_eligible_but_stuck: int  # autoMergeExpected=True but still open
    by_category: dict[str, int] = field(default_factory=dict)
    by_blocking_reason: dict[str, int] = field(default_factory=dict)


@dataclass
class RepoRenovateReport:
    """Per-repo renovate dashboard payload — matches repos/<slug>.json contract."""

    repo: str
    collected_at: str  # ISO-8601 UTC
    open_prs: list[RenovatePR]
    summary: RepoRenovateSummary
    auto_merged: Optional[AutoMergeStats]


@dataclass
class FleetRenovateReport:
    """Fleet-wide aggregate — matches fleet.json contract."""

    collected_at: str
    fleet_size: int
    repos_with_open_prs: int
    total_open_prs: int
    by_category: dict[str, int]
    by_blocking_reason: dict[str, int]
    auto_merged_in_window: Optional[AutoMergeStats]
