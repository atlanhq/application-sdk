"""
Pure classification logic for Renovate PRs.

Label-first: reads self-managed labels added by the fleet preset.
Falls back to branch/title/body parsing for PRs that predate the label rollout
(Renovate re-labels them on its next run after the preset change ships).

Auto-merge policy mirrors renovate-config/default.json:
  - lock-maintenance: automerge=true  (uv.lock-only, in-range)
  - github-actions:   automerge=true  (all update types, incl. major)
  - contract-toolkit: automerge=true  for minor/patch; false for major
  - python-dep:       automerge=false (edits pyproject.toml constraint → human)

Blocking-reason mirrors renovate-auto-approve-reusable.yml conditions:
  file allowlist: .github/, uv.lock, package-lock.json, requirements.txt, pyproject.toml
  (at any depth — (.*/)?<name> pattern from the workflow's grep -vxE filter)
"""

from __future__ import annotations

import re

from conformance.renovate.models import (
    BlockingReason,
    Category,
    ChecksState,
    RenovatePR,
    UpdateType,
)

# Labels emitted by the fleet preset's addLabels rules.
_LABEL_CATEGORY_MAP: dict[str, Category] = {
    "update:lock-maintenance": Category.LOCK_MAINTENANCE,
    "update:github-actions": Category.GITHUB_ACTIONS,
    "contract-toolkit-update": Category.CONTRACT_TOOLKIT,
}
_LABEL_UPDATE_TYPE_MAP: dict[str, UpdateType] = {
    "update:major": UpdateType.MAJOR,
    "update:minor": UpdateType.MINOR,
    "update:patch": UpdateType.PATCH,
    "update:digest": UpdateType.DIGEST,
    "update:pin": UpdateType.PIN,
}

# Allowed file patterns for the auto-approve gate (mirrors renovate-auto-approve-reusable.yml).
_DEP_FILE_RE = re.compile(
    r"^(\.github/.*"
    r"|(.*/)?uv\.lock"
    r"|(.*/)?package-lock\.json"
    r"|(.*/)?requirements\.txt"
    r"|(.*/)?pyproject\.toml)$"
)


def _non_dep_files(files: list[str]) -> list[str]:
    return [f for f in files if not _DEP_FILE_RE.match(f)]


def categorize(pr: RenovatePR) -> Category:
    """Derive category from self-managed labels (primary) then branch/title (fallback)."""
    label_set = set(pr.labels)

    # Label-first path.
    for label, cat in _LABEL_CATEGORY_MAP.items():
        if label in label_set:
            return cat

    # Fallback: branch/title parsing for pre-label PRs.
    branch = pr.branch.lower()
    title = pr.title.lower()

    if "lock-file-maintenance" in branch or pr.title.startswith(
        "Lock file maintenance"
    ):
        return Category.LOCK_MAINTENANCE
    if "github-actions" in branch:
        return Category.GITHUB_ACTIONS
    if "app-contract-toolkit" in branch or "app-contract-toolkit" in title:
        return Category.CONTRACT_TOOLKIT

    return Category.PYTHON_DEP


def update_type_from_labels(labels: list[str]) -> UpdateType:
    """Extract the Renovate update-type from self-managed labels."""
    for label in labels:
        if label in _LABEL_UPDATE_TYPE_MAP:
            return _LABEL_UPDATE_TYPE_MAP[label]
    return UpdateType.UNKNOWN


def update_type_from_body(body: str) -> UpdateType:
    """
    Fallback: parse the semver update type from the Renovate PR body table.

    Renovate embeds a 'from -> to' version table like:
        | dep | 1.2.3 | -> | 2.0.0 |
    We extract the numeric majors and classify accordingly.
    """
    # Match patterns like "1.2.3 -> 2.0.0" or "v1.2 -> v2.0"
    pattern = re.compile(r"v?(\d+)\.\d+[^\s]*\s*->\s*v?(\d+)\.\d+", re.MULTILINE)
    for m in pattern.finditer(body):
        from_major, to_major = int(m.group(1)), int(m.group(2))
        if to_major > from_major:
            return UpdateType.MAJOR
        return UpdateType.MINOR  # minor/patch — can't distinguish without 3-part semver
    return UpdateType.UNKNOWN


def derive_update_type(pr: RenovatePR) -> UpdateType:
    """Labels first, body-table fallback."""
    ut = update_type_from_labels(pr.labels)
    if ut != UpdateType.UNKNOWN:
        return ut
    return update_type_from_body(pr.body)


def auto_merge_expected(category: Category, update_type: UpdateType) -> bool:
    """
    Mirror renovate-config/default.json auto-merge policy.

    lock-maintenance → always auto (uv.lock refresh, in-range)
    github-actions   → always auto (incl. major; validated by CI gate)
    contract-toolkit → auto for minor/patch; human for major
    python-dep       → always human (edits pyproject.toml constraint → out-of-range)
    """
    if category == Category.LOCK_MAINTENANCE:
        return True
    if category == Category.GITHUB_ACTIONS:
        return True
    if category == Category.CONTRACT_TOOLKIT:
        return update_type not in (UpdateType.MAJOR, UpdateType.UNKNOWN)
    # python-dep, unknown
    return False


def checks_state(pr: RenovatePR) -> ChecksState:
    """Map the GitHub statusCheckRollup to a simplified state."""
    # The 'checks_state' field on RenovatePR is already parsed in scan.py from
    # statusCheckRollup; this helper is for tests / callers that have raw values.
    return pr.checks_state


def blocking_reason(
    pr: RenovatePR,
    category: Category,
    update_type: UpdateType,
) -> BlockingReason:
    """
    Why has this open PR not merged?

    Mirrors the conditions in renovate-auto-approve-reusable.yml.
    """
    if not auto_merge_expected(category, update_type):
        return BlockingReason.AWAITING_HUMAN_REVIEW

    # For auto-merge-eligible PRs, check gate conditions in priority order.
    if pr.mergeable == "CONFLICTING":
        return BlockingReason.MERGE_CONFLICT

    if _non_dep_files(pr.files):
        return BlockingReason.NON_DEP_FILES

    if pr.checks_state == ChecksState.FAILING:
        return BlockingReason.CHECKS_FAILING
    if pr.checks_state == ChecksState.PENDING:
        return BlockingReason.CHECKS_PENDING

    # All conditions satisfied — atlan-ci approval just hasn't been posted yet.
    return BlockingReason.AWAITING_APPROVAL


def classify(pr: RenovatePR) -> RenovatePR:
    """
    Return a new RenovatePR with category, update_type, auto_merge_expected,
    blocking_reason, and age_days populated.
    """
    from datetime import datetime, timezone

    cat = categorize(pr)
    ut = derive_update_type(pr)
    ame = auto_merge_expected(cat, ut)
    br = blocking_reason(pr, cat, ut)

    now = datetime.now(timezone.utc)
    # pr.created_at may be tz-aware or naive; normalise.
    created = pr.created_at
    if created.tzinfo is None:
        from datetime import timezone as _tz

        created = created.replace(tzinfo=_tz.utc)
    age = max(0, (now - created).days)

    # dataclass is frozen=True; use replace pattern.
    import dataclasses

    return dataclasses.replace(
        pr,
        category=cat,
        update_type=ut,
        auto_merge_expected=ame,
        blocking_reason=br,
        age_days=age,
    )
