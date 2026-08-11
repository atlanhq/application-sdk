"""
Pure classification logic for Renovate PRs.

Label-first: reads self-managed labels added by the fleet preset.
Falls back to branch/title/body parsing for PRs that predate the label rollout
(Renovate re-labels them on its next run after the preset change ships).

Auto-merge policy mirrors renovate-config/default.json:
  - lock-maintenance:    automerge=true  (uv.lock-only, in-range)
  - github-actions:      automerge=true  (all update types, incl. major)
  - contract-toolkit:    automerge=true  for minor/patch; false for major
  - conformance-package: automerge=true  for minor/patch; false for major
                         (dedicated uv.lock-only PR via update-lockfile;
                         auto-merged even under the soft-mode template's
                         '*' automerge=false rule, which carves it out)
  - python-dep:          automerge=false (edits pyproject.toml constraint → human)

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
    PRDep,
    RenovatePR,
    UpdateType,
)

# Labels emitted by the fleet preset's addLabels rules.
_LABEL_CATEGORY_MAP: dict[str, Category] = {
    "update:lock-maintenance": Category.LOCK_MAINTENANCE,
    "update:github-actions": Category.GITHUB_ACTIONS,
    "contract-toolkit-update": Category.CONTRACT_TOOLKIT,
    "conformance-package-update": Category.CONFORMANCE_PACKAGE,
    "sdk-package-update": Category.SDK_PACKAGE,
}
_LABEL_UPDATE_TYPE_MAP: dict[str, UpdateType] = {
    "update:major": UpdateType.MAJOR,
    "update:minor": UpdateType.MINOR,
    "update:patch": UpdateType.PATCH,
    "update:digest": UpdateType.DIGEST,
    "update:pin": UpdateType.PIN,
}

# The runtime SDK package — matched in branch slugs (renovate/atlan-application-sdk-3.x)
# and titles ("update dependency atlan-application-sdk to v3.27.0"). The negative
# lookahead keeps atlan-application-sdk-conformance from matching.
_SDK_PACKAGE_RE = re.compile(r"atlan-application-sdk(?!-conformance)")

# Allowed file patterns for the auto-approve gate (mirrors renovate-auto-approve-reusable.yml).
_DEP_FILE_RE = re.compile(
    r"^(\.github/.*"
    r"|(.*/)?uv\.lock"
    r"|(.*/)?package-lock\.json"
    r"|(.*/)?requirements\.txt"
    r"|(.*/)?pyproject\.toml)$"
)


# Age backstop for the "genuinely wedged" signal. An auto-merge-eligible, green
# PR still open this many days after creation is treated as stuck regardless of
# the specific mechanism — the auto-approve → auto-merge → queue pipeline runs on
# a cadence of hours, so a full day open means something is broken. Deliberately
# conservative to avoid flagging PRs the pipeline is still legitimately carrying.
STALE_AFTER_DAYS = 1


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
    # groupName "conformance package" → branch slug renovate/conformance-package;
    # ungrouped fallback branch carries the full package name.
    if (
        "conformance-package" in branch
        or "application-sdk-conformance" in branch
        or "conformance package" in title
        or "application-sdk-conformance" in title
    ):
        return Category.CONFORMANCE_PACKAGE
    # SDK check must come AFTER conformance: "atlan-application-sdk" is a
    # prefix of "atlan-application-sdk-conformance", so the negative lookahead
    # alone isn't enough for grouped-branch slugs that drop the suffix.
    if _SDK_PACKAGE_RE.search(branch) or _SDK_PACKAGE_RE.search(title):
        return Category.SDK_PACKAGE

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


# Renovate PR body version-table row: first cell is the package (usually a
# markdown link, sometimes bare text), and the Change cell renders the version
# move as `old` -> `new`. Cell-internal parentheses (changelog links, "(source)")
# and extra columns vary by manager, so match loosely: name from the first cell,
# then the first backtick-quoted arrow pair anywhere on the same row.
_BODY_TABLE_NAME_RE = re.compile(
    r"^\|\s*(?:\[(?P<linked>[^\]]+)\]|(?P<bare>[^|\[\]]+?))\s*(?:\(|\|)"
)
_BODY_TABLE_CHANGE_RE = re.compile(r"`(?P<from>[^`]+)`\s*(?:->|→)\s*`(?P<to>[^`]+)`")

# Title fallback: "chore(deps): update dependency atlan-application-sdk to v3.27.0",
# "chore(deps): update app-contract-toolkit to v0.18.1",
# "chore(deps): update anthropics/claude-code-action action to v1.0.190". Grouped
# titles without a trailing version ("update non-critical python dependencies")
# deliberately don't match — there is no version to report.
_TITLE_DEP_RE = re.compile(
    r"update (?:dependency )?(?P<name>[A-Za-z0-9._/@-]+)(?: action)? to v?(?P<to>\d[\w.+-]*)",
    re.IGNORECASE,
)


def extract_deps(pr: RenovatePR) -> tuple[PRDep, ...]:
    """Which packages this PR delivers, as (name, from, to) triples.

    Body version-table first (authoritative — grouped PRs list every package
    there and their titles carry no version), title parse as fallback for
    bodies that are empty or unrecognisable. Lock-file-maintenance PRs have no
    table and yield () — they deliver in-range bumps invisibly, which is
    exactly why downstream freshness tooling needs the explicit deps on every
    other category.
    """
    deps: list[PRDep] = []
    seen: set[tuple[str, str]] = set()
    for line in pr.body.splitlines():
        name_m = _BODY_TABLE_NAME_RE.match(line.strip())
        if not name_m:
            continue
        name = (name_m.group("linked") or name_m.group("bare") or "").strip()
        # Skip the header row and separator rows.
        if (
            not name
            or name.lower() in {"package", "dependency", "update"}
            or set(name) <= {"-", ":"}
        ):
            continue
        change_m = _BODY_TABLE_CHANGE_RE.search(line)
        if not change_m:
            continue
        key = (name, change_m.group("to").strip())
        if key in seen:
            continue
        seen.add(key)
        deps.append(
            PRDep(
                name=name,
                from_version=change_m.group("from").strip(),
                to_version=change_m.group("to").strip(),
            )
        )
    if deps:
        return tuple(deps)

    title_m = _TITLE_DEP_RE.search(pr.title)
    if title_m:
        return (
            PRDep(
                name=title_m.group("name"),
                from_version="",
                to_version=title_m.group("to"),
            ),
        )
    return ()


def auto_merge_expected(category: Category, update_type: UpdateType) -> bool:
    """
    Mirror renovate-config/default.json auto-merge policy.

    lock-maintenance → always auto (uv.lock refresh, in-range)
    github-actions   → always auto (incl. major; validated by CI gate)
    contract-toolkit → auto for minor/patch; human for major
    sdk-package      → always human (runtime SDK bumps are a deliberate merge)
    python-dep       → always human (edits pyproject.toml constraint → out-of-range)
    """
    if category == Category.LOCK_MAINTENANCE:
        return True
    if category == Category.GITHUB_ACTIONS:
        return True
    if category == Category.CONTRACT_TOOLKIT:
        return update_type not in (UpdateType.MAJOR, UpdateType.UNKNOWN)
    if category == Category.CONFORMANCE_PACKAGE:
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
    age_days: int = 0,
) -> BlockingReason:
    """
    Why has this open PR not merged?

    Mirrors the conditions in renovate-auto-approve-reusable.yml, then adds two
    signals for the silent-stuck case a green/approved/mergeable PR can fall into
    when GitHub-native auto-merge is never armed (see PR #2828 postmortem).

    ``age_days`` is threaded in explicitly rather than read off ``pr`` because
    classify() computes it after the model is first constructed.
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

    # Eligible, non-conflicting, dep-only. Both auto-merge signals below describe
    # a PR whose every gate is *green* yet nothing is driving it to merge, so they
    # only fire on GREEN. FAILING/PENDING are already handled above; UNKNOWN
    # (checks rollup couldn't be determined) must not masquerade as green-but-
    # parked — it falls through to AWAITING_APPROVAL as it did before these
    # signals existed. Is anything actually driving a green PR to merge?
    if pr.checks_state == ChecksState.GREEN:
        approved = pr.review_decision == "APPROVED"

        # Precise signal: approval is in and every gate is green, yet auto-merge
        # was never armed. With a required merge queue nothing will ever merge it
        # — the dangerous "looks healthy, parked forever" case. Detected
        # immediately (no age threshold) because there is nothing left to wait for.
        if approved and not pr.auto_merge_enabled:
            return BlockingReason.AUTOMERGE_NOT_ARMED

        # Age backstop: green + eligible but still open past the staleness
        # threshold. Not gated on approval or armed-state so it also catches a
        # down approval workflow and wedged merge queues — any stuck mode,
        # including ones not modelled above.
        if age_days >= STALE_AFTER_DAYS:
            return BlockingReason.AUTOMERGE_STALE

    # Recently eligible (or checks state not yet determinable); expected to merge
    # imminently (approval pending or freshly armed and awaiting the queue).
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

    now = datetime.now(timezone.utc)
    # pr.created_at may be tz-aware or naive; normalise.
    created = pr.created_at
    if created.tzinfo is None:
        from datetime import timezone as _tz

        created = created.replace(tzinfo=_tz.utc)
    age = max(0, (now - created).days)

    # age feeds the staleness backstop, so it must be computed before classifying.
    br = blocking_reason(pr, cat, ut, age)

    # dataclass is frozen=True; use replace pattern.
    import dataclasses

    return dataclasses.replace(
        pr,
        category=cat,
        update_type=ut,
        auto_merge_expected=ame,
        blocking_reason=br,
        age_days=age,
        deps=extract_deps(pr),
    )
