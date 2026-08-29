"""Unit tests for the Renovate PR classifier (conformance.renovate.classify)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conformance.renovate.classify import (
    STALE_AFTER_DAYS,
    bounded_lock_refusal_state,
    classify,
)
from conformance.renovate.classify import lock_refusal_reason as extract_refusal_reason
from conformance.renovate.classify import lock_refusal_window as extract_refusal_window
from conformance.renovate.classify import parse_window
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
    head_committed_at: datetime | None = None,
    lock_refusal_window: str = "",
    lock_refusal_reason: str = "",
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
        head_committed_at=head_committed_at,
        lock_refusal_window=lock_refusal_window,
        lock_refusal_reason=lock_refusal_reason,
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


def test_category_conformance_package_via_label() -> None:
    pr = classify(make_pr(labels=["conformance-package-update"]))
    assert pr.category is Category.CONFORMANCE_PACKAGE


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


def test_category_conformance_package_fallback_grouped_branch() -> None:
    pr = classify(make_pr(labels=[], branch="renovate/conformance-package"))
    assert pr.category is Category.CONFORMANCE_PACKAGE


def test_category_conformance_package_fallback_ungrouped_branch() -> None:
    pr = classify(
        make_pr(labels=[], branch="renovate/atlan-application-sdk-conformance-0.x")
    )
    assert pr.category is Category.CONFORMANCE_PACKAGE


def test_category_conformance_package_fallback_title() -> None:
    # Title arm of categorize(): branch and labels carry no conformance signal, so
    # the "conformance package" substring in the title is what classifies it.
    pr = classify(
        make_pr(
            labels=[],
            branch="renovate/all-minor-patch",
            title="Update conformance package to v0.5.0",
        )
    )
    assert pr.category is Category.CONFORMANCE_PACKAGE


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


def test_auto_merge_expected_conformance_package_minor() -> None:
    pr = classify(make_pr(labels=["conformance-package-update", "update:minor"]))
    assert pr.auto_merge_expected is True


def test_auto_merge_expected_conformance_package_major() -> None:
    pr = classify(make_pr(labels=["conformance-package-update", "update:major"]))
    assert pr.auto_merge_expected is False


def test_auto_merge_expected_conformance_package_unknown() -> None:
    # No update-type label and no parseable body table → UNKNOWN → human-reviewed.
    pr = classify(make_pr(labels=["conformance-package-update"], body=""))
    assert pr.update_type is UpdateType.UNKNOWN
    assert pr.auto_merge_expected is False


def test_conformance_package_prelabel_compatibility_composed() -> None:
    # Pre-label-rollout PR: no self-managed labels, so category comes from the
    # branch fallback and update type from the body table — composed through
    # classify() end to end (not as independent units).
    pr = classify(
        make_pr(
            labels=[],
            branch="renovate/conformance-package",
            body="Updates atlan-application-sdk-conformance from 1.2.3 -> 1.3.0",
        )
    )
    assert pr.category is Category.CONFORMANCE_PACKAGE
    assert pr.update_type is UpdateType.MINOR
    assert pr.auto_merge_expected is True


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


# ── Category: sdk-package ────────────────────────────────────────────────────


def test_category_sdk_package_via_label() -> None:
    pr = classify(make_pr(labels=["sdk-package-update"]))
    assert pr.category is Category.SDK_PACKAGE


def test_category_sdk_package_fallback_branch() -> None:
    pr = classify(make_pr(labels=[], branch="renovate/atlan-application-sdk-3.x"))
    assert pr.category is Category.SDK_PACKAGE


def test_category_sdk_package_fallback_title() -> None:
    pr = classify(
        make_pr(
            labels=[],
            branch="renovate/some-branch",
            title="chore(deps): update dependency atlan-application-sdk to v3.27.0",
        )
    )
    assert pr.category is Category.SDK_PACKAGE


def test_category_sdk_does_not_steal_conformance() -> None:
    # "atlan-application-sdk" is a prefix of the conformance package name, so
    # the conformance check must win for both branch and title variants.
    pr = classify(
        make_pr(labels=[], branch="renovate/atlan-application-sdk-conformance-0.x")
    )
    assert pr.category is Category.CONFORMANCE_PACKAGE
    pr = classify(
        make_pr(
            labels=[],
            branch="renovate/some-branch",
            title="Update dependency atlan-application-sdk-conformance to v0.14.0",
        )
    )
    assert pr.category is Category.CONFORMANCE_PACKAGE


def test_auto_merge_expected_sdk_package() -> None:
    # Runtime SDK bumps are a deliberate human merge regardless of update type.
    pr = classify(make_pr(labels=["sdk-package-update", "update:patch"]))
    assert pr.auto_merge_expected is False
    assert pr.blocking_reason is BlockingReason.AWAITING_HUMAN_REVIEW


# ── extract_deps: PR → delivered packages ────────────────────────────────────


_SDK_BODY_TABLE = """\
This PR contains the following updates:

| Package | Change | Age | Adoption | Passing | Confidence |
|---|---|---|---|---|---|
| [atlan-application-sdk](https://redirect.github.com/atlanhq/application-sdk) ([changelog](https://example)) | `3.26.0` -> `3.27.0` | ok | ok | ok | ok |
"""

_GROUP_BODY_TABLE = """\
| Package | Change |
|---|---|
| [requests](https://example) | `2.31.0` -> `2.32.1` |
| pydantic | `2.7.0` -> `2.8.0` |
"""


def test_extract_deps_from_body_table_linked() -> None:
    pr = classify(make_pr(body=_SDK_BODY_TABLE))
    assert [(d.name, d.from_version, d.to_version) for d in pr.deps] == [
        ("atlan-application-sdk", "3.26.0", "3.27.0")
    ]


def test_extract_deps_from_body_table_grouped_multiple_rows() -> None:
    # Grouped PRs: every package row is captured; header/separator rows are not.
    pr = classify(make_pr(body=_GROUP_BODY_TABLE))
    assert [(d.name, d.to_version) for d in pr.deps] == [
        ("requests", "2.32.1"),
        ("pydantic", "2.8.0"),
    ]


def test_extract_deps_title_fallback_when_no_table() -> None:
    pr = classify(
        make_pr(
            body="no table here",
            title="chore(deps): update dependency atlan-application-sdk to v3.27.0",
        )
    )
    assert [(d.name, d.from_version, d.to_version) for d in pr.deps] == [
        ("atlan-application-sdk", "", "3.27.0")
    ]


def test_extract_deps_custom_manager_title() -> None:
    # The contract-toolkit custom manager drops the "dependency" word.
    pr = classify(
        make_pr(body="", title="chore(deps): update app-contract-toolkit to v0.18.1")
    )
    assert [(d.name, d.to_version) for d in pr.deps] == [
        ("app-contract-toolkit", "0.18.1")
    ]


def test_extract_deps_lock_maintenance_empty() -> None:
    # Lock refresh PRs carry no version table and no versioned title.
    pr = classify(
        make_pr(
            labels=["update:lock-maintenance"],
            title="Lock file maintenance",
            branch="renovate/lock-file-maintenance",
            body="This PR refreshes the lock file.",
        )
    )
    assert pr.deps == ()


def test_extract_deps_grouped_title_without_version_yields_empty() -> None:
    pr = classify(
        make_pr(body="", title="chore(deps): update non-critical python dependencies")
    )
    assert pr.deps == ()


# Live Renovate bodies render the Change column with a Unicode arrow — captured
# from PR #3117 on this repo (an `update … action to …` github-actions PR). The
# parser must match what Renovate actually emits, not an idealized ASCII table.
_LIVE_BODY_TABLE_UNICODE_ARROW = """\
This PR contains the following updates:

| Package | Type | Update | Change |
|---|---|---|---|
| [atlanhq/application-sdk](https://redirect.github.com/atlanhq/application-sdk) | action | patch | `v3.27.0` → `v3.27.1` |
"""


def test_extract_deps_from_live_body_table_unicode_arrow() -> None:
    pr = classify(make_pr(body=_LIVE_BODY_TABLE_UNICODE_ARROW))
    assert [(d.name, d.from_version, d.to_version) for d in pr.deps] == [
        ("atlanhq/application-sdk", "v3.27.0", "v3.27.1")
    ]


def test_extract_deps_title_fallback_action_form() -> None:
    # Live title of PR #3112: github-actions bumps say "update X action to vY".
    pr = classify(
        make_pr(
            body="no table here",
            title="chore(deps): update anthropics/claude-code-action action to v1.0.190",
        )
    )
    assert [(d.name, d.from_version, d.to_version) for d in pr.deps] == [
        ("anthropics/claude-code-action", "", "1.0.190")
    ]


# ── Bounded-lock refusal: expired vs. ordinary broken build (FND-782) ────────

# The tripwire withhold() writes: baseline versions plus a bare [options] table.
# The absence of an `exclude-newer` key is what separates it from an [options]
# table uv wrote itself.
_TRIPWIRE_LOCK = """\
version = 1
revision = 3
requires-python = ">=3.11"

[options]
exclude-newer-span = "P3D"

[[package]]
name = "boto3"
version = "1.43.67"
"""

# What uv itself records when a repo declares its own [tool.uv] exclude-newer —
# same span key, but always alongside the absolute timestamp and (when per-package
# ceilings apply) the subtable. Four fleet repos carry this permanently, on base
# as well as on every branch, and must never read as a refusal.
_UV_OWN_OPTIONS_LOCK = """\
version = 1
revision = 3

[options]
exclude-newer = "0001-01-01T00:00:00Z" # no effect; back-compat for relative values
exclude-newer-span = "P7D"

[options.exclude-newer-package]
pyatlan = { timestamp = "0001-01-01T00:00:00Z", span = "PT0S" }

[[package]]
name = "boto3"
version = "1.43.67"
"""

_PLAIN_LOCK = """\
version = 1
revision = 3

[[package]]
name = "boto3"
version = "1.43.67"
"""


def _refusal_pr(
    *,
    window: str = "P3D",
    head_age: timedelta = timedelta(days=4),
    files: list[str] | None = None,
    labels: list[str] | None = None,
    checks_state: ChecksState = ChecksState.FAILING,
    reason: str = "",
) -> RenovatePR:
    """A red, uv.lock-only lock-maintenance PR carrying an expired tripwire.

    ``reason`` defaults to unstamped — the pre-FND-909 shape, which is judged
    against the window it names rather than the reaper's grace.
    """
    return make_pr(
        labels=labels if labels is not None else ["update:lock-maintenance"],
        title="Lock file maintenance",
        branch="renovate/lock-file-maintenance",
        checks_state=checks_state,
        files=files if files is not None else ["uv.lock"],
        created_at=_OLD,
        head_committed_at=_NOW - head_age,
        lock_refusal_window=window,
        lock_refusal_reason=reason,
    )


def test_window_parses_days_and_hours() -> None:
    assert parse_window("P3D") == timedelta(days=3)
    assert parse_window("PT12H") == timedelta(hours=12)
    assert parse_window(" P1DT6H ") == timedelta(days=1, hours=6)


def test_window_rejects_calendar_units_and_junk() -> None:
    # Mirrors the driver: months/years are refused rather than approximated.
    assert parse_window("P1M") is None
    assert parse_window("P") is None
    assert parse_window("") is None
    assert parse_window("3 days") is None


def test_refusal_window_read_from_tripwire() -> None:
    assert extract_refusal_window(_TRIPWIRE_LOCK) == "P3D"


def test_refusal_window_empty_for_uv_written_options() -> None:
    # The discriminator that keeps the four self-bounding repos out of the signal.
    assert extract_refusal_window(_UV_OWN_OPTIONS_LOCK) == ""


def test_refusal_window_empty_for_ordinary_lock() -> None:
    assert extract_refusal_window(_PLAIN_LOCK) == ""


def test_refusal_reason_read_from_a_stamped_tripwire() -> None:
    stamped = _TRIPWIRE_LOCK.replace(
        'exclude-newer-span = "P3D"',
        'exclude-newer-span = "P3D"  # refusal: window-empty',
    )
    assert extract_refusal_reason(stamped) == "window-empty"
    # The stamp must not corrupt the window every existing reader already parses.
    assert extract_refusal_window(stamped) == "P3D"


def test_refusal_reason_empty_for_an_unstamped_tripwire() -> None:
    # Pre-FND-909 shape: a tripwire is present, but it names no reason.
    assert extract_refusal_window(_TRIPWIRE_LOCK) == "P3D"
    assert extract_refusal_reason(_TRIPWIRE_LOCK) == ""


def test_refusal_reason_ignores_a_comment_that_is_not_a_stamp() -> None:
    # Only the driver's `refusal:` prefix counts; a stray comment is not a reason.
    lock = _TRIPWIRE_LOCK.replace(
        'exclude-newer-span = "P3D"',
        'exclude-newer-span = "P3D"  # set by hand, do not edit',
    )
    assert extract_refusal_reason(lock) == ""
    assert extract_refusal_window(lock) == "P3D"


def test_refusal_reason_empty_for_uv_written_options() -> None:
    # uv's own table is not a refusal at all, so it names no reason either.
    assert extract_refusal_reason(_UV_OWN_OPTIONS_LOCK) == ""


def test_refusal_window_ignores_a_span_outside_the_options_table() -> None:
    # A later table quoting the same key must not be read as the tripwire.
    lock = _PLAIN_LOCK + '\n[tool.something]\nexclude-newer-span = "P7D"\n'
    assert extract_refusal_window(lock) == ""


def test_blocking_bounded_lock_refusal_expired() -> None:
    # Red, lock-only, tripwire present, head older than the window it names.
    pr = classify(_refusal_pr())
    assert pr.blocking_reason is BlockingReason.BOUNDED_LOCK_REFUSAL_EXPIRED


def test_blocking_bounded_lock_refusal_expired_at_the_window_boundary() -> None:
    # Everything blocking a refusal written at T is >= W old at T + W, so the
    # boundary itself counts as expired.
    pr = classify(_refusal_pr(head_age=timedelta(days=3, seconds=1)))
    assert pr.blocking_reason is BlockingReason.BOUNDED_LOCK_REFUSAL_EXPIRED


def test_blocking_checks_failing_while_the_refusal_is_still_live() -> None:
    # Held today, for a reason that still holds. Nothing to surface yet.
    pr = classify(_refusal_pr(head_age=timedelta(hours=6)))
    assert pr.blocking_reason is BlockingReason.CHECKS_FAILING


def test_blocking_checks_failing_when_the_lock_has_no_tripwire() -> None:
    # An ordinary broken lock-maintenance build — a human owns it.
    pr = classify(_refusal_pr(window=""))
    assert pr.blocking_reason is BlockingReason.CHECKS_FAILING


def test_blocking_checks_failing_when_the_diff_is_more_than_the_lock() -> None:
    pr = classify(_refusal_pr(files=["uv.lock", "pyproject.toml"]))
    assert pr.blocking_reason is BlockingReason.CHECKS_FAILING


def test_blocking_checks_failing_when_head_date_is_unknown() -> None:
    # Input predating headCommittedAt: no clock, so no claim. created_at is not a
    # substitute — Renovate rewrites a lock branch in place.
    pr = classify(
        make_pr(
            labels=["update:lock-maintenance"],
            checks_state=ChecksState.FAILING,
            files=["uv.lock"],
            created_at=_OLD,
            head_committed_at=None,
            lock_refusal_window="P3D",
        )
    )
    assert pr.blocking_reason is BlockingReason.CHECKS_FAILING


def test_blocking_refusal_signal_only_applies_to_lock_maintenance() -> None:
    # Only the lock lane runs the bounded driver, so only it can be carrying a
    # refusal. Assert the category guard rather than relying on the other three
    # conditions to happen to exclude everything else.
    pr = classify(_refusal_pr(labels=["conformance-package-update", "update:patch"]))
    assert pr.blocking_reason is BlockingReason.CHECKS_FAILING


def test_blocking_merge_conflict_still_wins_over_the_refusal_signal() -> None:
    pr = classify(
        make_pr(
            labels=["update:lock-maintenance"],
            checks_state=ChecksState.FAILING,
            files=["uv.lock"],
            mergeable="CONFLICTING",
            head_committed_at=_NOW - timedelta(days=4),
            lock_refusal_window="P3D",
        )
    )
    assert pr.blocking_reason is BlockingReason.MERGE_CONFLICT


def test_expired_refusal_counts_as_auto_merge_eligible_but_stuck() -> None:
    # The dashboard's "stuck" count is derived from blocking_reason, so a new
    # reason must not silently drop out of it.
    pr = classify(_refusal_pr())
    assert pr.auto_merge_expected is True
    assert pr.blocking_reason is not BlockingReason.AWAITING_HUMAN_REVIEW


def test_refusal_expiry_tolerates_a_naive_clock_from_a_caller() -> None:
    # blocking_reason() accepts a caller-supplied `now`. A naive one against an
    # aware head_committed_at would raise on subtraction; both sides get the same
    # UTC coercion classify() applies to created_at.
    naive_now = _NOW.replace(tzinfo=None)
    assert (
        bounded_lock_refusal_state(_refusal_pr(), Category.LOCK_MAINTENANCE, naive_now)
        is BlockingReason.BOUNDED_LOCK_REFUSAL_EXPIRED
    )


# --- the stamped split (FND-909) ------------------------------------------


def test_stamped_self_healing_refusal_expires_on_the_reaper_grace() -> None:
    """Past two fleet passes the reaper should have deleted this branch.

    Well inside the P3D window it names, so the window clock alone would still
    call it live — the point of the stamp is that the window stopped being the
    question once something is supposed to delete the branch regardless.
    """
    pr = classify(_refusal_pr(reason="window-empty", head_age=timedelta(hours=10)))
    assert pr.blocking_reason is BlockingReason.BOUNDED_LOCK_REFUSAL_EXPIRED


def test_stamped_self_healing_refusal_is_not_flagged_within_the_grace() -> None:
    """One missed pass is latency, not an outage. The reaper gets two."""
    pr = classify(_refusal_pr(reason="window-empty", head_age=timedelta(hours=5)))
    assert pr.blocking_reason is BlockingReason.CHECKS_FAILING


def test_standing_fault_is_reported_immediately() -> None:
    """No clock: waiting never clears it, so an age gate would only delay a human."""
    pr = classify(_refusal_pr(reason="rollback", head_age=timedelta(minutes=1)))
    assert pr.blocking_reason is BlockingReason.BOUNDED_LOCK_REFUSAL_STANDING


def test_standing_fault_never_ages_into_the_reaper_signal() -> None:
    """An old wedge is still a human's, not evidence the reaper broke."""
    pr = classify(
        _refusal_pr(reason="unsatisfiable-floor", head_age=timedelta(days=30))
    )
    assert pr.blocking_reason is BlockingReason.BOUNDED_LOCK_REFUSAL_STANDING


def test_standing_fault_is_reported_without_a_head_clock() -> None:
    """The stamp alone is enough; the standing path never reaches the clock."""
    pr = classify(
        make_pr(
            labels=["update:lock-maintenance"],
            title="Lock file maintenance",
            branch="renovate/lock-file-maintenance",
            checks_state=ChecksState.FAILING,
            files=["uv.lock"],
            created_at=_OLD,
            head_committed_at=None,
            lock_refusal_window="P3D",
            lock_refusal_reason="no-packaging",
        )
    )
    assert pr.blocking_reason is BlockingReason.BOUNDED_LOCK_REFUSAL_STANDING


def test_an_unrecognised_reason_is_treated_as_standing() -> None:
    """The safe direction: an unknown stamp gets a human, never the shorter clock.

    A refusal path added to the driver without updating the reader must not
    inherit self-healing by accident — that would have the alarm stay quiet on a
    real wedge.
    """
    pr = classify(_refusal_pr(reason="some-future-reason", head_age=timedelta(days=9)))
    assert pr.blocking_reason is BlockingReason.BOUNDED_LOCK_REFUSAL_STANDING


def test_unstamped_tripwire_keeps_the_window_clock() -> None:
    """Pre-FND-909 locks carry no reason; "none given" must not read as healing.

    Ten hours is past the reaper grace but well inside P3D. A stamped
    self-healing refusal would be flagged here; an unstamped one must not be,
    because nothing claims the reaper was ever going to take it.
    """
    pr = classify(_refusal_pr(reason="", head_age=timedelta(hours=10)))
    assert pr.blocking_reason is BlockingReason.CHECKS_FAILING
