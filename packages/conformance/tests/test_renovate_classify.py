"""Unit tests for the Renovate PR classifier (conformance.renovate.classify)."""

from __future__ import annotations

from datetime import datetime, timezone

from conformance.renovate.classify import classify
from conformance.renovate.models import (
    BlockingReason,
    Category,
    ChecksState,
    RenovatePR,
    UpdateType,
)

# Anchor "now" once so age computations are deterministic within a run. _OLD is
# clamped to day-of-month >= 1 so the 3-day-old PR stays in the same month.
_NOW = datetime.now(timezone.utc)
_OLD = datetime(_NOW.year, _NOW.month, max(1, _NOW.day - 3), tzinfo=timezone.utc)


def make_pr(
    *,
    number: int = 1,
    url: str = "https://github.com/atlanhq/atlan-foo-app/pull/1",
    title: str = "Update dependency foo to v1.2.3",
    branch: str = "renovate/foo-1.x",
    labels: list[str] | None = None,
    mergeable: str = "MERGEABLE",
    review_decision: str = "",
    checks_state: ChecksState = ChecksState.GREEN,
    files: list[str] | None = None,
    created_at: datetime = _NOW,
    updated_at: datetime = _NOW,
    is_draft: bool = False,
    body: str = "",
) -> RenovatePR:
    """Construct an unclassified RenovatePR with sane defaults for one scenario."""
    return RenovatePR(
        number=number,
        url=url,
        title=title,
        branch=branch,
        labels=labels if labels is not None else [],
        mergeable=mergeable,
        review_decision=review_decision,
        checks_state=checks_state,
        files=files if files is not None else [],
        created_at=created_at,
        updated_at=updated_at,
        is_draft=is_draft,
        body=body,
    )


# ── Category: label-driven path ──────────────────────────────────────────────


def test_category_lock_maintenance_via_label() -> None:
    pr = classify(make_pr(labels=["update:lock-maintenance"]))
    assert pr.category is Category.LOCK_MAINTENANCE


def test_category_github_actions_via_label() -> None:
    pr = classify(make_pr(labels=["update:github-actions"]))
    assert pr.category is Category.GITHUB_ACTIONS


def test_category_contract_toolkit_via_label() -> None:
    pr = classify(make_pr(labels=["contract-toolkit-update"]))
    assert pr.category is Category.CONTRACT_TOOLKIT


def test_category_python_dep_major_via_label() -> None:
    pr = classify(make_pr(labels=["update:major", "dependencies"]))
    assert pr.category is Category.PYTHON_DEP


# ── Category: fallback parsing path (no update: labels) ──────────────────────


def test_category_lock_maintenance_fallback() -> None:
    pr = classify(make_pr(labels=[], branch="renovate/lock-file-maintenance"))
    assert pr.category is Category.LOCK_MAINTENANCE


def test_category_github_actions_fallback() -> None:
    pr = classify(make_pr(labels=[], branch="renovate/github-actions"))
    assert pr.category is Category.GITHUB_ACTIONS


def test_category_contract_toolkit_fallback() -> None:
    pr = classify(make_pr(labels=[], branch="renovate/app-contract-toolkit-1.x"))
    assert pr.category is Category.CONTRACT_TOOLKIT


# ── UpdateType from labels ───────────────────────────────────────────────────


def test_update_type_major_from_label() -> None:
    pr = classify(make_pr(labels=["update:major"]))
    assert pr.update_type is UpdateType.MAJOR


def test_update_type_patch_from_label() -> None:
    pr = classify(make_pr(labels=["update:patch"]))
    assert pr.update_type is UpdateType.PATCH


# ── UpdateType fallback from body ────────────────────────────────────────────


def test_update_type_major_from_body() -> None:
    pr = classify(make_pr(labels=[], body="Updates foo from 1.2.3 -> 2.0.0"))
    assert pr.update_type is UpdateType.MAJOR


def test_update_type_minor_from_body() -> None:
    pr = classify(make_pr(labels=[], body="Updates foo from 1.2.3 -> 1.4.0"))
    assert pr.update_type is UpdateType.MINOR


def test_update_type_unknown_when_no_signal() -> None:
    pr = classify(make_pr(labels=[], body=""))
    assert pr.update_type is UpdateType.UNKNOWN


# ── auto_merge_expected ──────────────────────────────────────────────────────


def test_auto_merge_expected_lock_maintenance() -> None:
    pr = classify(make_pr(labels=["update:lock-maintenance"]))
    assert pr.auto_merge_expected is True


def test_auto_merge_expected_github_actions() -> None:
    pr = classify(make_pr(labels=["update:github-actions"]))
    assert pr.auto_merge_expected is True


def test_auto_merge_expected_contract_toolkit_minor() -> None:
    pr = classify(make_pr(labels=["contract-toolkit-update", "update:minor"]))
    assert pr.auto_merge_expected is True


def test_auto_merge_expected_contract_toolkit_major() -> None:
    pr = classify(make_pr(labels=["contract-toolkit-update", "update:major"]))
    assert pr.auto_merge_expected is False


def test_auto_merge_expected_python_dep_minor() -> None:
    pr = classify(make_pr(labels=["update:minor", "dependencies"]))
    assert pr.auto_merge_expected is False


def test_auto_merge_expected_python_dep_major() -> None:
    pr = classify(make_pr(labels=["update:major", "dependencies"]))
    assert pr.auto_merge_expected is False


# ── blocking_reason ──────────────────────────────────────────────────────────


def test_blocking_awaiting_human_review() -> None:
    # python-dep major is not auto-merge-eligible — blocked by design.
    pr = classify(make_pr(labels=["update:major", "dependencies"]))
    assert pr.blocking_reason is BlockingReason.AWAITING_HUMAN_REVIEW


def test_blocking_checks_failing() -> None:
    pr = classify(
        make_pr(labels=["update:lock-maintenance"], checks_state=ChecksState.FAILING)
    )
    assert pr.blocking_reason is BlockingReason.CHECKS_FAILING


def test_blocking_checks_pending() -> None:
    pr = classify(
        make_pr(labels=["update:lock-maintenance"], checks_state=ChecksState.PENDING)
    )
    assert pr.blocking_reason is BlockingReason.CHECKS_PENDING


def test_blocking_merge_conflict() -> None:
    pr = classify(make_pr(labels=["update:lock-maintenance"], mergeable="CONFLICTING"))
    assert pr.blocking_reason is BlockingReason.MERGE_CONFLICT


def test_blocking_non_dep_files() -> None:
    pr = classify(make_pr(labels=["update:github-actions"], files=["src/foo.py"]))
    assert pr.blocking_reason is BlockingReason.NON_DEP_FILES


def test_blocking_awaiting_approval() -> None:
    # github-actions, all green, no conflict, only dep files changed.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            mergeable="MERGEABLE",
            checks_state=ChecksState.GREEN,
        )
    )
    assert pr.blocking_reason is BlockingReason.AWAITING_APPROVAL


# ── Non-dep file detection ───────────────────────────────────────────────────


def test_dep_files_allowed() -> None:
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[
                ".github/workflows/test.yaml",
                "uv.lock",
                "packages/foo/uv.lock",
                "pyproject.toml",
            ],
        )
    )
    assert pr.blocking_reason is not BlockingReason.NON_DEP_FILES


def test_non_dep_file_triggers_block() -> None:
    pr = classify(make_pr(labels=["update:github-actions"], files=["src/app.py"]))
    assert pr.blocking_reason is BlockingReason.NON_DEP_FILES


# ── age_days ─────────────────────────────────────────────────────────────────


def test_age_days_computed() -> None:
    pr = classify(make_pr(created_at=_OLD, updated_at=_NOW))
    assert pr.age_days >= 3
