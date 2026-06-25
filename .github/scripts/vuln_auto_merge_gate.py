#!/usr/bin/env python3
"""Auto-approve + auto-merge gate for vuln-triage PRs.

This is the **deterministic safety boundary** for the zero-human-touch CVE
flow. The vuln-triage rover opens two — and only two — shapes of PR, each
labelled ``vuln-auto-merge``:

  * **allowlist PR** — touches ONLY ``.security/base-allowlist.json``
  * **bump PR**      — touches ONLY a subset of ``pyproject.toml``,
                       ``uv.lock``, ``requirements.txt``

A PR is approved (as ``atlan-ci``, satisfying code-owner review) and put on
GitHub auto-merge (``gh pr merge --auto --squash``) iff ALL hold:

  1. author is one of the trusted rover identities,
  2. PR is open and not a draft,
  3. its HEAD SHA still matches the SHA whose checks just completed (race guard),
  4. it carries the ``vuln-auto-merge`` label, and
  5. every changed file matches exactly one of the two shapes above.

GitHub itself enforces "merge only when required checks pass" via auto-merge,
so this gate never merges a red PR. A bump PR that ALSO touches source files
(e.g. changelog-mandated code edits) fails the path-allowlist (5) and is
therefore NOT auto-merged — it falls back to human / @sdk-review review. That
is intended, not a bug: the rover's prose is never trusted to bound what it
touches; this file is.

Loop / conditional logic lives here (a tested script) rather than inlined in
the workflow YAML, per docs/standards/ci.md.

Environment:
    GH_TOKEN                 PAT owned by atlan-ci (repo read + PR write).
    REPO                     owner/name (github.repository).
    EVENT_NAME               'workflow_run' or 'workflow_dispatch'.
    RUN_SHA                  workflow_run head_sha (empty for dispatch).
    DISPATCH_PR              PR number (workflow_dispatch manual testing).

Optional:
    VULN_AUTOMERGE_LABEL     default 'vuln-auto-merge'.
    VULN_AUTOMERGE_AUTHORS   comma-separated trusted PR authors;
                             default 'atlan-ci,mothership-ai[bot]'.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

ALLOWLIST_FILE = ".security/base-allowlist.json"
# Root-level dependency manifests for the SDK. Matched exactly (no subpaths) so
# a stray same-named file deeper in the tree can never ride this gate.
BUMP_FILES = {"pyproject.toml", "uv.lock", "requirements.txt"}

APPROVAL_SIGNATURE = "**Vuln auto-merge:**"
DEFAULT_LABEL = "vuln-auto-merge"
# Trusted PR authors:
#   - mothership-ai[bot] — the rover's allowlist + bump PRs. NOT a code owner, so
#     these need an atlan-ci approval to satisfy require_code_owner_review.
#   - atlan-ci — the reconcile removal PR (opened via ORG_PAT_GITHUB). atlan-ci is
#     a main-ruleset BYPASS actor, so it needs NO approval — and GitHub forbids a
#     PR author approving their own PR, so we must NOT try to self-approve it.
DEFAULT_AUTHORS = "atlan-ci,mothership-ai[bot]"
# The login the gate's GH_TOKEN (ORG_PAT_GITHUB) acts as. A PR authored by this
# identity is bypass-merged without an approval (self-approval is rejected by
# GitHub).
DEFAULT_APPROVER = "atlan-ci"

Runner = Callable[..., subprocess.CompletedProcess]


# ---------------------------------------------------------------------------
# Pure decision logic (the safety boundary — exhaustively unit-tested)
# ---------------------------------------------------------------------------


def classify_shape(filenames: list[str]) -> str | None:
    """Return 'allowlist', 'bump', or None for the given changed files.

    'allowlist' → exactly {.security/base-allowlist.json}
    'bump'      → non-empty subset of {pyproject.toml, uv.lock, requirements.txt}
    None        → anything else (mixed, extra, or empty) → no auto-merge.
    """
    files = {f for f in filenames if f}
    if not files:
        return None
    if files == {ALLOWLIST_FILE}:
        return "allowlist"
    if files <= BUMP_FILES:
        return "bump"
    return None


def evaluate(
    *,
    author: str,
    state: str,
    draft: bool,
    head_sha: str,
    eval_sha: str,
    labels: list[str],
    filenames: list[str],
    label_name: str,
    trusted_authors: set[str],
) -> tuple[bool, str, str | None]:
    """Decide whether to approve + auto-merge a PR.

    Returns (ok, reason, shape). ``ok`` True means approve + enable auto-merge.
    """
    if author not in trusted_authors:
        return False, f"author '{author}' is not a trusted vuln-triage identity", None
    if state != "open" or draft:
        return False, f"PR state='{state}' draft={draft}", None
    if head_sha != eval_sha:
        return False, f"HEAD moved ({eval_sha} → {head_sha})", None
    if label_name not in labels:
        return False, f"missing '{label_name}' label", None
    shape = classify_shape(filenames)
    if shape is None:
        return False, "changed files do not match an allowlist/bump shape", None
    return True, f"matches {shape} shape", shape


# ---------------------------------------------------------------------------
# gh I/O helpers
# ---------------------------------------------------------------------------


def _gh_text(args: list[str], runner: Runner) -> str | None:
    """Run a gh command and return its stripped stdout (None on failure/empty).

    Use for ``--jq`` queries that emit RAW values — gh prints jq strings
    unquoted and emits one line per result, which is not valid JSON.
    """
    result = runner(["gh", *args], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        return None
    out = (result.stdout or "").strip()
    return out or None


def _gh_json(args: list[str], runner: Runner) -> Any:
    """Run a gh command whose ``--jq`` emits a single JSON object/array."""
    out = _gh_text(args, runner)
    if out is None:
        return None
    return json.loads(out)


def resolve_pr_numbers(
    repo: str, event_name: str, run_sha: str, dispatch_pr: str, runner: Runner
) -> tuple[list[str], str]:
    """Return (pr_numbers, eval_sha) for the triggering event."""
    if event_name == "workflow_dispatch":
        sha = _gh_text(
            ["api", f"repos/{repo}/pulls/{dispatch_pr}", "--jq", ".head.sha"], runner
        )
        return ([dispatch_pr] if dispatch_pr else []), (sha or "")
    nums = _gh_text(
        [
            "api",
            f"repos/{repo}/commits/{run_sha}/pulls",
            "--jq",
            '.[] | select(.state == "open") | .number',
        ],
        runner,
    )
    if not nums:
        return [], run_sha
    return nums.split(), run_sha


def already_approved(repo: str, pr: str, runner: Runner) -> bool:
    n = _gh_text(
        [
            "api",
            f"repos/{repo}/pulls/{pr}/reviews",
            "--paginate",
            "--jq",
            '[.[] | select(.user.login == "atlan-ci" and .state == "APPROVED" '
            f'and ((.body // "") | startswith("{APPROVAL_SIGNATURE}")))] | length',
        ],
        runner,
    )
    try:
        return int(n or "0") > 0
    except ValueError:
        return False


def approve(repo: str, pr: str, shape: str, runner: Runner) -> None:
    body = (
        f"{APPROVAL_SIGNATURE} {shape} PR from the vuln-triage rover.\n\n"
        "Automated code-owner approval by `atlan-ci`. The PR is on GitHub "
        "auto-merge and will squash-merge only once all required checks pass. "
        "Dismissed on any new push (`dismiss_stale_reviews_on_push`)."
    )
    runner(
        ["gh", "pr", "review", pr, "--repo", repo, "--approve", "--body", body],
        check=True,
    )


def enable_automerge(repo: str, pr: str, runner: Runner, *, check: bool = True) -> None:
    runner(
        ["gh", "pr", "merge", pr, "--repo", repo, "--auto", "--squash"],
        check=check,
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def process_pr(
    repo: str,
    pr: str,
    eval_sha: str,
    label_name: str,
    trusted_authors: set[str],
    approver_login: str,
    runner: Runner,
) -> bool:
    """Evaluate one PR; approve (if needed) + auto-merge if it passes.

    Returns True if a new approval was posted.
    """
    meta = _gh_json(
        [
            "api",
            f"repos/{repo}/pulls/{pr}",
            "--jq",
            "{author: .user.login, state: .state, draft: .draft, head_sha: .head.sha, "
            "labels: [.labels[].name]}",
        ],
        runner,
    )
    if not meta:
        print(f"PR #{pr}: could not fetch metadata — skipping.")
        return False

    files_raw = _gh_text(
        ["api", f"repos/{repo}/pulls/{pr}/files", "--paginate", "--jq", ".[].filename"],
        runner,
    )
    filenames = files_raw.splitlines() if files_raw else []

    author = meta.get("author", "")
    ok, reason, shape = evaluate(
        author=author,
        state=meta.get("state", ""),
        draft=bool(meta.get("draft")),
        head_sha=meta.get("head_sha", ""),
        eval_sha=eval_sha,
        labels=meta.get("labels", []) or [],
        filenames=filenames,
        label_name=label_name,
        trusted_authors=trusted_authors,
    )
    if not ok:
        print(f"PR #{pr}: skip — {reason}.")
        return False

    # A PR authored by the approver identity (atlan-ci) cannot be self-approved
    # (GitHub 422). atlan-ci is a main-ruleset bypass actor, so just enable
    # auto-merge — no approval needed.
    if author == approver_login:
        print(f"PR #{pr}: authored by {author} (bypass actor) — enabling auto-merge.")
        enable_automerge(repo, pr, runner, check=False)
        return False

    if already_approved(repo, pr, runner):
        print(f"PR #{pr}: already approved by atlan-ci — ensuring auto-merge only.")
        enable_automerge(repo, pr, runner, check=False)
        return False

    print(f"PR #{pr}: {reason} — approving + enabling auto-merge.")
    approve(repo, pr, shape or "", runner)
    enable_automerge(repo, pr, runner)
    print(f"✅ PR #{pr}: approved as atlan-ci and queued for auto-merge ({shape}).")
    return True


def main(runner: Runner = subprocess.run) -> int:
    repo = os.environ["REPO"]
    event_name = os.environ.get("EVENT_NAME", "workflow_run")
    run_sha = os.environ.get("RUN_SHA", "")
    dispatch_pr = os.environ.get("DISPATCH_PR", "")
    label_name = os.environ.get("VULN_AUTOMERGE_LABEL", DEFAULT_LABEL)
    approver_login = os.environ.get("VULN_AUTOMERGE_APPROVER", DEFAULT_APPROVER)
    trusted_authors = {
        a.strip()
        for a in os.environ.get("VULN_AUTOMERGE_AUTHORS", DEFAULT_AUTHORS).split(",")
        if a.strip()
    }

    pr_numbers, eval_sha = resolve_pr_numbers(
        repo, event_name, run_sha, dispatch_pr, runner
    )
    if not pr_numbers:
        print(f"No open PRs to evaluate for SHA {eval_sha or '(none)'}.")
        return 0

    for pr in pr_numbers:
        print(f"--- Evaluating PR #{pr} ---")
        process_pr(
            repo, pr, eval_sha, label_name, trusted_authors, approver_login, runner
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
