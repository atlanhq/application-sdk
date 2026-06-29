"""
Renovate fleet dashboard scanner.

Reads raw GitHub API PR data (from `gh pr list --json ...` output files),
classifies each PR, builds per-repo and fleet reports, and writes JSON +
append-only history JSONL to an output directory.

CLI entry point: conformance renovate-scan
  --input DIR    directory containing per-repo JSON files named <slug>.json
                 (each file is the stdout of `gh pr list --json ... > <slug>.json`)
  --merged DIR   directory with merged-PR JSON files (for auto-merge counts), optional
  --out DIR      output directory (default: /tmp/renovate-dashboard)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from conformance.renovate.classify import classify
from conformance.renovate.models import (
    AutoMergeStats,
    BlockingReason,
    ChecksState,
    FleetRenovateReport,
    RenovatePR,
    RepoRenovateReport,
    RepoRenovateSummary,
)

# Stable signature written by renovate-auto-approve-reusable.yml.
_AUTO_APPROVE_SIGNATURE = "**Renovate auto-approval:**"
_WINDOW_DAYS = 30


_CHECKS_FAILING: frozenset[str] = frozenset(
    {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}
)
_CHECKS_PENDING: frozenset[str] = frozenset(
    {"PENDING", "IN_PROGRESS", "QUEUED", "WAITING", "REQUESTED"}
)


def _parse_checks_state(rollup: list[dict]) -> ChecksState:
    """Reduce statusCheckRollup to ChecksState.

    gh pr list --json statusCheckRollup returns a mix of two item shapes:
    - StatusContext (commit status): non-null ``state`` field
      (SUCCESS | FAILURE | PENDING | ERROR | EXPECTED).
    - CheckRun (Actions workflow step): ``state`` is null; use ``conclusion``
      when the run is completed (success | failure | timed_out | cancelled |
      action_required | neutral | skipped), or ``status`` while it is still
      running (queued | in_progress | waiting | requested).

    Empirically the vast majority of items on a Renovate PR are CheckRun with
    state=null, so reading only ``state`` misclassifies failing/pending checks
    as GREEN.
    """
    if not rollup:
        return ChecksState.UNKNOWN

    states: set[str] = set()
    for c in rollup:
        raw_state = c.get("state")
        if raw_state:
            # StatusContext item.
            states.add(raw_state.upper())
        else:
            # CheckRun item: conclusion is set when completed, status otherwise.
            conclusion = c.get("conclusion") or ""
            status = c.get("status") or ""
            states.add((conclusion or status).upper())

    states.discard("")

    if not states:
        return ChecksState.UNKNOWN
    if states & _CHECKS_FAILING:
        return ChecksState.FAILING
    if states & _CHECKS_PENDING:
        return ChecksState.PENDING
    return ChecksState.GREEN


def _parse_pr(raw: dict) -> Optional[RenovatePR]:
    """Parse a single item from `gh pr list --json` output."""
    try:
        created = datetime.fromisoformat(raw["createdAt"].replace("Z", "+00:00"))
        updated = datetime.fromisoformat(raw["updatedAt"].replace("Z", "+00:00"))
        labels = [lb["name"] for lb in raw.get("labels", [])]
        files = [f["path"] for f in raw.get("files", [])]
        rollup = raw.get("statusCheckRollup") or []
        return RenovatePR(
            number=raw["number"],
            url=raw["url"],
            title=raw["title"],
            branch=raw.get("headRefName", ""),
            labels=labels,
            mergeable=raw.get("mergeable", "UNKNOWN"),
            review_decision=raw.get("reviewDecision") or "",
            checks_state=_parse_checks_state(rollup),
            files=files,
            created_at=created,
            updated_at=updated,
            is_draft=raw.get("isDraft", False),
            body=raw.get("body") or "",
        )
    except (KeyError, ValueError) as exc:
        print(f"Warning: skipping malformed PR record: {exc}", file=sys.stderr)
        return None


def _auto_merge_stats(merged_raw: list[dict]) -> AutoMergeStats:
    """
    Count auto-merged vs human-merged PRs.

    A PR counts as auto-merged if any review from 'atlan-ci' with state
    'APPROVED' starts with the stable auto-approval signature.
    """
    auto_merged = 0
    human_merged = 0
    for pr in merged_raw:
        reviews = pr.get("reviews", [])
        was_auto = any(
            r.get("author", {}).get("login") == "atlan-ci"
            and r.get("state") == "APPROVED"
            and (r.get("body") or "").startswith(_AUTO_APPROVE_SIGNATURE)
            for r in reviews
        )
        if was_auto:
            auto_merged += 1
        else:
            human_merged += 1
    total = auto_merged + human_merged
    return AutoMergeStats(
        window_days=_WINDOW_DAYS,
        auto_merged=auto_merged,
        human_merged=human_merged,
        total_merged=total,
    )


def _build_repo_report(
    repo: str,
    open_prs_raw: list[dict],
    merged_raw: Optional[list[dict]],
) -> RepoRenovateReport:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    classified: list[RenovatePR] = []
    for raw in open_prs_raw:
        pr = _parse_pr(raw)
        if pr is not None and not pr.is_draft:
            classified.append(classify(pr))

    by_category: dict[str, int] = {}
    by_blocking: dict[str, int] = {}
    needs_human = 0
    stuck = 0
    for pr in classified:
        by_category[pr.category.value] = by_category.get(pr.category.value, 0) + 1
        by_blocking[pr.blocking_reason.value] = (
            by_blocking.get(pr.blocking_reason.value, 0) + 1
        )
        if not pr.auto_merge_expected:
            needs_human += 1
        elif pr.blocking_reason != BlockingReason.AWAITING_HUMAN_REVIEW:
            stuck += 1

    summary = RepoRenovateSummary(
        open_total=len(classified),
        needs_human=needs_human,
        auto_merge_eligible_but_stuck=stuck,
        by_category=by_category,
        by_blocking_reason=by_blocking,
    )

    auto_stats: Optional[AutoMergeStats] = None
    if merged_raw is not None:
        auto_stats = _auto_merge_stats(merged_raw)

    return RepoRenovateReport(
        repo=repo,
        collected_at=now,
        open_prs=classified,
        summary=summary,
        auto_merged=auto_stats,
    )


def _pr_to_dict(pr: RenovatePR) -> dict:
    return {
        "number": pr.number,
        "url": pr.url,
        "title": pr.title,
        "branch": pr.branch,
        "labels": pr.labels,
        "category": pr.category.value,
        "updateType": pr.update_type.value,
        "autoMergeExpected": pr.auto_merge_expected,
        "blockingReason": pr.blocking_reason.value,
        "mergeable": pr.mergeable,
        "checksState": pr.checks_state.value,
        "ageDays": pr.age_days,
        "createdAt": pr.created_at.isoformat(),
        "updatedAt": pr.updated_at.isoformat(),
    }


def _report_to_dict(report: RepoRenovateReport) -> dict:
    return {
        "repo": report.repo,
        "collectedAt": report.collected_at,
        "openPRs": [_pr_to_dict(p) for p in report.open_prs],
        "summary": {
            "open": report.summary.open_total,
            "needsHuman": report.summary.needs_human,
            "autoMergeEligibleButStuck": report.summary.auto_merge_eligible_but_stuck,
            "byCategory": report.summary.by_category,
            "byBlockingReason": report.summary.by_blocking_reason,
        },
        "autoMerged": {
            "windowDays": report.auto_merged.window_days,
            "autoMerged": report.auto_merged.auto_merged,
            "humanMerged": report.auto_merged.human_merged,
            "totalMerged": report.auto_merged.total_merged,
        }
        if report.auto_merged
        else None,
    }


def _fleet_to_dict(fleet: FleetRenovateReport) -> dict:
    return {
        "collectedAt": fleet.collected_at,
        "fleetSize": fleet.fleet_size,
        "reposWithOpenPRs": fleet.repos_with_open_prs,
        "totalOpenPRs": fleet.total_open_prs,
        "byCategory": fleet.by_category,
        "byBlockingReason": fleet.by_blocking_reason,
        "autoMergedInWindow": {
            "windowDays": fleet.auto_merged_in_window.window_days,
            "autoMerged": fleet.auto_merged_in_window.auto_merged,
            "humanMerged": fleet.auto_merged_in_window.human_merged,
            "totalMerged": fleet.auto_merged_in_window.total_merged,
        }
        if fleet.auto_merged_in_window
        else None,
    }


def _build_fleet(reports: list[RepoRenovateReport]) -> FleetRenovateReport:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    by_cat: dict[str, int] = {}
    by_block: dict[str, int] = {}
    total_open = 0
    repos_with_open = 0
    total_auto = 0
    total_human = 0

    for r in reports:
        total_open += r.summary.open_total
        if r.summary.open_total > 0:
            repos_with_open += 1
        for k, v in r.summary.by_category.items():
            by_cat[k] = by_cat.get(k, 0) + v
        for k, v in r.summary.by_blocking_reason.items():
            by_block[k] = by_block.get(k, 0) + v
        if r.auto_merged:
            total_auto += r.auto_merged.auto_merged
            total_human += r.auto_merged.human_merged

    fleet_auto = (
        AutoMergeStats(
            window_days=_WINDOW_DAYS,
            auto_merged=total_auto,
            human_merged=total_human,
            total_merged=total_auto + total_human,
        )
        if any(r.auto_merged for r in reports)
        else None
    )

    return FleetRenovateReport(
        collected_at=now,
        fleet_size=len(reports),
        repos_with_open_prs=repos_with_open,
        total_open_prs=total_open,
        by_category=by_cat,
        by_blocking_reason=by_block,
        auto_merged_in_window=fleet_auto,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="conformance renovate-scan",
        description="Build Renovate fleet dashboard JSON from gh pr list output files.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="Directory containing open-PR JSON files named <slug>.json",
    )
    parser.add_argument(
        "--merged",
        default="",
        help="Directory containing merged-PR JSON files named <slug>.json (optional)",
    )
    parser.add_argument(
        "--out",
        default="/tmp/renovate-dashboard",
        help="Output directory (default: /tmp/renovate-dashboard)",
    )
    args = parser.parse_args(argv)

    input_dir = Path(args.input)
    merged_dir = Path(args.merged) if args.merged else None
    out_dir = Path(args.out)
    (out_dir / "repos").mkdir(parents=True, exist_ok=True)

    reports: list[RepoRenovateReport] = []

    for open_file in sorted(input_dir.glob("*.json")):
        slug = open_file.stem
        repo = slug.replace("_", "/", 1)  # atlanhq_my-app → atlanhq/my-app

        try:
            open_prs_raw: list[dict] = json.loads(open_file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Warning: skipping {open_file}: {exc}", file=sys.stderr)
            continue

        merged_raw: Optional[list[dict]] = None
        if merged_dir:
            merged_file = merged_dir / open_file.name
            if merged_file.exists():
                try:
                    merged_raw = json.loads(merged_file.read_text())
                except (json.JSONDecodeError, OSError):
                    pass

        report = _build_repo_report(repo, open_prs_raw, merged_raw)
        reports.append(report)

        # Per-repo output.
        repo_path = out_dir / "repos" / f"{slug}.json"
        repo_path.write_text(json.dumps(_report_to_dict(report), indent=2))

        # Append-only history.
        history_entry = {
            "date": report.collected_at[:10],
            "repo": report.repo,
            "openTotal": report.summary.open_total,
            "needsHuman": report.summary.needs_human,
            "autoMergeEligibleButStuck": report.summary.auto_merge_eligible_but_stuck,
        }
        history_path = out_dir / f"history_{slug}.jsonl"
        with history_path.open("a") as fh:
            fh.write(json.dumps(history_entry) + "\n")

        print(
            f"{repo}: {report.summary.open_total} open "
            f"({report.summary.needs_human} human, "
            f"{report.summary.auto_merge_eligible_but_stuck} stuck)",
            file=sys.stderr,
        )

    # Fleet aggregate.
    fleet = _build_fleet(reports)
    (out_dir / "fleet.json").write_text(json.dumps(_fleet_to_dict(fleet), indent=2))

    # Fleet history.
    fleet_history = {
        "date": fleet.collected_at[:10],
        "fleetSize": fleet.fleet_size,
        "reposWithOpenPRs": fleet.repos_with_open_prs,
        "totalOpenPRs": fleet.total_open_prs,
    }
    with (out_dir / "history_fleet.jsonl").open("a") as fh:
        fh.write(json.dumps(fleet_history) + "\n")

    print(
        f"Fleet: {fleet.fleet_size} repos, {fleet.total_open_prs} open PRs, "
        f"{fleet.repos_with_open_prs} repos with open PRs",
        file=sys.stderr,
    )
    return 0
