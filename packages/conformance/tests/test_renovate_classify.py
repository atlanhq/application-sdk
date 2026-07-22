"""Unit tests for the Renovate PR classifier (conformance.renovate.classify)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conformance.renovate.classify import STALE_AFTER_DAYS, classify
from conformance.renovate.models import (
    BlockingReason,
    Category,
    ChecksState,
    RenovatePR,
    UpdateType,
)
from conformance.renovate.scan import _parse_checks_state

# Anchor "now" once so age computations are deterministic within a run. _OLD is
# exactly 3 days before _NOW via timedelta so it is correct across month
# boundaries — the previous day-of-month clamp collapsed _OLD onto _NOW on days
# 1–3 of a month (age_days would be 0, not 3).
_NOW = datetime.now(timezone.utc)
_OLD = _NOW - timedelta(days=3)


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
    auto_merge_enabled: bool = False,
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
        auto_merge_enabled=auto_merge_enabled,
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
    # github-actions, all green, no conflict, dep-only, freshly opened and not yet
    # approved → transient wait for the atlan-ci approval, not stuck.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            mergeable="MERGEABLE",
            checks_state=ChecksState.GREEN,
            review_decision="",
            created_at=_NOW,
        )
    )
    assert pr.blocking_reason is BlockingReason.AWAITING_APPROVAL


def test_blocking_awaiting_approval_armed_and_young() -> None:
    # Approved + auto-merge armed + freshly opened → in-flight, expected to merge
    # via the queue imminently. Not stuck.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            review_decision="APPROVED",
            auto_merge_enabled=True,
            created_at=_NOW,
        )
    )
    assert pr.blocking_reason is BlockingReason.AWAITING_APPROVAL


def test_blocking_automerge_not_armed() -> None:
    # The #2828 silent-stuck case: eligible, green, dep-only, code-owner approved,
    # but GitHub-native auto-merge was never enabled → nothing will merge it.
    # Flagged immediately (age 0), no staleness wait.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            review_decision="APPROVED",
            auto_merge_enabled=False,
            created_at=_NOW,
        )
    )
    assert pr.blocking_reason is BlockingReason.AUTOMERGE_NOT_ARMED


def test_blocking_automerge_not_armed_wins_over_stale() -> None:
    # Precise not-armed signal takes priority over the age backstop when both hold.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            review_decision="APPROVED",
            auto_merge_enabled=False,
            created_at=_OLD,
        )
    )
    assert pr.blocking_reason is BlockingReason.AUTOMERGE_NOT_ARMED


def test_blocking_automerge_stale_unapproved_old() -> None:
    # Age backstop: eligible + green but still open past the threshold and never
    # approved → the auto-approval pipeline is likely down. Caught without needing
    # to model the specific failure. (Backstop is not gated on approval.)
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            review_decision="",
            created_at=_OLD,
        )
    )
    assert pr.blocking_reason is BlockingReason.AUTOMERGE_STALE


def test_blocking_automerge_stale_armed_but_wedged() -> None:
    # Armed and approved but still open past the threshold → merge queue wedged.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            review_decision="APPROVED",
            auto_merge_enabled=True,
            created_at=_OLD,
        )
    )
    assert pr.blocking_reason is BlockingReason.AUTOMERGE_STALE


def test_blocking_automerge_stale_at_exact_threshold() -> None:
    # Boundary: age == STALE_AFTER_DAYS must trip the backstop (the `>=` in
    # classify.py). Anchored to STALE_AFTER_DAYS so a future `>=` → `>` regression
    # fails here. Unapproved so the not-armed branch is skipped and the stale
    # backstop is isolated.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            review_decision="",
            created_at=_NOW - timedelta(days=STALE_AFTER_DAYS),
        )
    )
    assert pr.blocking_reason is BlockingReason.AUTOMERGE_STALE


def test_blocking_automerge_not_stale_just_under_threshold() -> None:
    # Boundary: just under a full day (age 0 after `.days` truncation) is NOT
    # stale yet — the freshly-eligible PR is still expected to merge imminently.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            review_decision="",
            created_at=_NOW - timedelta(hours=23),
        )
    )
    assert pr.blocking_reason is BlockingReason.AWAITING_APPROVAL


def test_blocking_unknown_checks_not_flagged_as_automerge() -> None:
    # UNKNOWN checks state is not green: the auto-merge-not-armed / stale signals
    # both assert every gate is green, so an eligible + approved + old PR whose
    # checks rollup can't be determined must NOT be reported as AUTOMERGE_* —
    # it falls through to AWAITING_APPROVAL as it did before these signals existed.
    pr = classify(
        make_pr(
            labels=["update:github-actions"],
            files=[".github/workflows/test.yaml"],
            review_decision="APPROVED",
            auto_merge_enabled=False,
            checks_state=ChecksState.UNKNOWN,
            created_at=_OLD,
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


# ── _parse_checks_state ───────────────────────────────────────────────────────


def test_parse_checks_state_status_context_success() -> None:
    # StatusContext item: non-null state field
    assert _parse_checks_state([{"state": "SUCCESS"}]) is ChecksState.GREEN


def test_parse_checks_state_status_context_failure() -> None:
    # StatusContext item: non-null state field, failing
    assert _parse_checks_state([{"state": "FAILURE"}]) is ChecksState.FAILING


def test_parse_checks_state_checkrun_conclusion_failure() -> None:
    # CheckRun item: state=null, conclusion=failure — the core bug this fixes
    assert (
        _parse_checks_state(
            [{"state": None, "conclusion": "failure", "status": "completed"}]
        )
        is ChecksState.FAILING
    )


def test_parse_checks_state_checkrun_in_progress() -> None:
    # CheckRun item: state=null, no conclusion yet, status=in_progress
    assert (
        _parse_checks_state(
            [{"state": None, "conclusion": None, "status": "in_progress"}]
        )
        is ChecksState.PENDING
    )


def test_parse_checks_state_mixed_all_green() -> None:
    # Real-world mix: StatusContext success + CheckRun success + CheckRun skipped
    rollup = [
        {"state": "SUCCESS"},
        {"state": None, "conclusion": "success", "status": "completed"},
        {"state": None, "conclusion": "skipped", "status": "completed"},
    ]
    assert _parse_checks_state(rollup) is ChecksState.GREEN


def test_parse_checks_state_failing_checkrun_not_classified_green() -> None:
    # The pre-fix bug: CheckRun with state=null and conclusion=failure was
    # previously misclassified as GREEN because only `state` was read.
    rollup = [
        {"state": None, "conclusion": "failure", "status": "completed"},
        {"state": None, "conclusion": "success", "status": "completed"},
    ]
    assert _parse_checks_state(rollup) is ChecksState.FAILING


def test_parse_checks_state_empty_rollup() -> None:
    assert _parse_checks_state([]) is ChecksState.UNKNOWN
