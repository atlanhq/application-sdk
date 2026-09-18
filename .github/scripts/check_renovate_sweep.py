#!/usr/bin/env python3
"""Guard: turn Renovate's two SILENT sweep failures into a red job.

Renovate exits 0 after failing to enable auto-merge. On 2026-09-17 two
release fan-out sweeps ran 7 minutes apart, the second exhausted the fleet
App's hourly API budget, and every PR the sweep created came out without
auto-merge:

    DEBUG: GitHub-native automerge: fail (repository=atlanhq/atlan-cassandra-dse-app,
                                          branch=renovate/conformance-package)
           "prNumber": 153,
           "errors": [{"type": "RATE_LIMIT", "code": "graphql_rate_limit", ...}]
     INFO: PR created

Nothing downstream can tell that apart from a healthy pass: the PR exists, its
checks go green, and it simply never merges. 38 conformance PRs sat open for
two hours before a human noticed. The budget exhaustion also fails OPEN inside
`rerun_evicted_tests_run.py` (its `gh api` 403s and it skips the repair), so
one exhausted budget silently produces BOTH a PR that cannot merge and a stale
failed `tests / Tests Gate` that blocks it.

Renovate DOES exit non-zero once the budget is gone entirely ("Init: Can't get
App details" → FATAL Authentication failure), so the sweep's tail is already
loud. This guard covers the head: the repos processed while the budget was
draining, which look successful and are not.

Pure functions over the log text; wired into `.github/workflows/renovate.yaml`
and tested in `.github/scripts/tests/test_check_renovate_sweep.py`.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path

# Renovate logs one header line per failure, then the detail as an indented
# block. Both the header and `prNumber` carry the GHA timestamp prefix, so
# every pattern is applied with `re.search`, never anchored at column 0.
_AUTOMERGE_FAIL_RE = re.compile(
    r"GitHub-native automerge: fail\s*\(repository=(?P<repository>[^,)]+)"
    r"(?:,\s*branch=(?P<branch>[^)]+))?\)"
)
_PR_NUMBER_RE = re.compile(r'"prNumber":\s*(?P<pr>\d+)')
_ERROR_CODE_RE = re.compile(r'"code":\s*"(?P<code>[^"]+)"')

# How far past the header to look for the detail block. Renovate emits
# prNumber on the next line and the error codes within the following few;
# a bounded window keeps an unrelated later failure from being attributed
# to this header.
_DETAIL_WINDOW = 12

# Budget exhaustion, in the wordings Renovate and the GitHub API actually
# emit. Matched case-sensitively — these are fixed strings from the platform,
# and a looser match would catch Renovate's own benign "rate limit" DEBUG
# chatter about remaining quota.
_BUDGET_MARKERS: tuple[str, ...] = (
    "API rate limit already exceeded",
    "API rate limit exceeded",
    "graphql_rate_limit",
    "Init: Can't get App details",
)


@dataclass(frozen=True)
class AutomergeFailure:
    """One PR that Renovate created but could not put auto-merge on."""

    repository: str
    branch: str
    pr_number: int | None
    error_code: str | None

    def describe(self) -> str:
        pr = f"#{self.pr_number}" if self.pr_number is not None else "(pr unknown)"
        code = self.error_code or "no error code logged"
        return f"{self.repository}{pr} [{self.branch or 'unknown branch'}]: {code}"


@dataclass(frozen=True)
class BudgetHit:
    """One log line showing the App's API budget was already spent."""

    marker: str
    line: str

    def describe(self) -> str:
        return f"{self.marker}: {self.line.strip()[:200]}"


def find_automerge_failures(log_text: str) -> list[AutomergeFailure]:
    """Every `GitHub-native automerge: fail` block in the log, in order."""
    lines = log_text.splitlines()
    failures: list[AutomergeFailure] = []
    for index, line in enumerate(lines):
        header = _AUTOMERGE_FAIL_RE.search(line)
        if header is None:
            continue
        detail = "\n".join(lines[index + 1 : index + 1 + _DETAIL_WINDOW])
        pr_match = _PR_NUMBER_RE.search(detail)
        code_match = _ERROR_CODE_RE.search(detail)
        failures.append(
            AutomergeFailure(
                repository=header.group("repository").strip(),
                branch=(header.group("branch") or "").strip(),
                pr_number=int(pr_match.group("pr")) if pr_match else None,
                error_code=code_match.group("code") if code_match else None,
            )
        )
    return failures


def find_budget_hits(log_text: str) -> list[BudgetHit]:
    """Every line showing the fleet App's hourly API budget was exhausted.

    Deduplicated by marker: one exhausted budget produces hundreds of these and
    the operator needs to know WHICH wall was hit, not how many times.
    """
    hits: list[BudgetHit] = []
    seen: set[str] = set()
    for line in log_text.splitlines():
        for marker in _BUDGET_MARKERS:
            if marker in line and marker not in seen:
                seen.add(marker)
                hits.append(BudgetHit(marker=marker, line=line))
    return hits


def report(log_text: str, repo: str) -> tuple[int, str]:
    """Exit code and the operator-facing message for one repo's sweep log."""
    failures = find_automerge_failures(log_text)
    hits = find_budget_hits(log_text)
    if not failures and not hits:
        return 0, f"Renovate sweep of {repo} left no silent failures."

    parts: list[str] = [f"Renovate sweep of {repo} completed with silent failures."]
    if hits:
        parts.append("")
        parts.append("GitHub API budget exhausted for the fleet App:")
        parts.extend(f"  - {hit.describe()}" for hit in hits)
        parts.append(
            "  Two full-fleet sweeps inside one rate-limit hour will do this. "
            "The workflow's concurrency group coalesces them; if this fires "
            "anyway, the budget itself is too small for the fleet size."
        )
    if failures:
        parts.append("")
        parts.append("PRs created WITHOUT auto-merge (they will never merge):")
        parts.extend(f"  - {failure.describe()}" for failure in failures)
        parts.append(
            "  Re-dispatch the sweep once the budget resets; Renovate re-enables "
            "auto-merge on an existing PR on its next pass."
        )
    return 1, "\n".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True, type=Path, help="Renovate log file")
    parser.add_argument("--repo", required=True, help="Repository this sweep covered")
    args = parser.parse_args()

    if not args.log.exists():
        # Fail open on a missing log: the Renovate step's own exit code is the
        # primary signal, and this guard must never convert a green sweep into
        # a red one because `tee` did not produce a file.
        print(f"::warning::{args.log} not found — skipping the sweep guard")
        return 0

    code, message = report(args.log.read_text(errors="replace"), args.repo)
    if code == 0:
        print(message)
    else:
        print(f"::error::{message}")
    return code


if __name__ == "__main__":
    sys.exit(main())
