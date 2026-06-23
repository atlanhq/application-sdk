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
    MERGE_CONFLICT = "merge_conflict"  # mergeable=CONFLICTING
    NON_DEP_FILES = "non_dep_files"  # changed files outside auto-approve allowlist
    AWAITING_APPROVAL = (
        "awaiting_approval"  # eligible; atlan-ci approval not yet posted
    )
    UNKNOWN = "unknown"


class ChecksState(str, Enum):
    GREEN = "green"
    FAILING = "failing"
    PENDING = "pending"
    UNKNOWN = "unknown"


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
    # populated by classify()
    category: Category = Category.UNKNOWN
    update_type: UpdateType = UpdateType.UNKNOWN
    auto_merge_expected: bool = False
    blocking_reason: BlockingReason = BlockingReason.UNKNOWN
    age_days: int = 0


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
