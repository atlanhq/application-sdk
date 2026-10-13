#!/usr/bin/env python3
"""The bump-version PR CI opt-in, read from the head commit (FND-3748, FND-3839).

CI on a ``bump-version-*`` PR is opt-in: nothing heavy runs on it until a
reviewer asks for this lane's full release gate (for an app or the SDK that
includes tenant e2e). The bump branch is force-pushed on every merge to its
target, so the opt-in is one-shot: it covers the commit it was given for.

The opt-in is a ``/gate-check`` PR comment (pr_test_command.py), which posts a
``gate-check`` commit status on the head SHA. That makes "one-shot" structural:
a push is a new commit with no status. It used to be a ``gate-check`` label,
which needed a ``labeled`` trigger (and with it a duplicate run of every
workflow on the same commit for every label any bot added) plus a clear step
before every bump push.

``check`` answers "is this PR opted in?" from the head commit's status, for a
gate that must read it live: a re-run replays the original event payload, and
the ``/gate-check`` comment works by re-running the runs that started before it.

``e2e-requested`` answers "was e2e asked for on this commit?" for the SDK's PR
Checks: a ``/e2e`` comment posts an ``e2e-request`` status, and on
``bump-version-main`` the ``gate-check`` opt-in implies e2e.

``clear`` still removes a ``gate-check`` label before a bump push, for connector
repos whose shims predate FND-3839 and still read the label. It goes once the
fleet has resynced.

``check`` fails open towards running CI: a status that cannot be read costs
runner minutes, never a missed gate. ``e2e-requested`` fails closed: e2e is a
live-tenant suite, and an unreadable request does not justify one.

Usage::

    python3 .github/scripts/bump_pr_opt_in.py clear \
        --repo owner/name --base main --head bump-version-main
    python3 .github/scripts/bump_pr_opt_in.py check \
        --repo owner/name --head-ref "$HEAD_REF" --sha "$HEAD_SHA"
    python3 .github/scripts/bump_pr_opt_in.py e2e-requested \
        --repo owner/name --head-ref "$HEAD_REF" --sha "$HEAD_SHA" \
        --fork "$IS_FORK" --author "$PR_AUTHOR"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

OPT_IN_LABEL = "gate-check"
BUMP_BRANCH_PREFIX = "bump-version"
#: Commit-status contexts, shared with pr_test_command.py (pinned by tests).
GATE_CHECK_CONTEXT = "gate-check"
E2E_REQUEST_CONTEXT = "e2e-request"
#: The SDK bump branch whose release gate includes tenant e2e. The
#: conformance / contract-toolkit bump lanes never run tenant e2e.
SDK_E2E_BUMP_BRANCH = "bump-version-main"
OPT_IN_DESCRIPTION = "Run the full release gate on this bump-version PR's head"
OPT_IN_COLOR = "fbca04"
# Per `gh` call. A stalled API request must not hold the bump job (or the
# gate it feeds) open until the job's own timeout; a timeout reads as a
# failed call, which every caller already handles.
GH_TIMEOUT_S = 120


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI. Bounded by GH_TIMEOUT_S."""
    try:
        return subprocess.run(
            cmd, capture_output=True, text=True, check=False, timeout=GH_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            cmd, 124, "", f"timed out after {GH_TIMEOUT_S}s"
        )


def ensure_label_exists(repo: str) -> bool:
    """Create the opt-in label if missing. "Already exists" is the common case.

    Any other failure is surfaced as a warning — without the label a reviewer
    has nothing to add from the UI — but never fails the release push.
    """
    result = run(
        [
            "gh",
            "label",
            "create",
            OPT_IN_LABEL,
            "--repo",
            repo,
            "--color",
            OPT_IN_COLOR,
            "--description",
            OPT_IN_DESCRIPTION,
        ]
    )
    if result.returncode == 0 or "already exists" in result.stderr.lower():
        return True
    print(
        f"::warning::could not create the '{OPT_IN_LABEL}' label on {repo}; "
        f"reviewers must create it to opt in: {result.stderr.strip()}"
    )
    return False


def open_pr_labels(repo: str, base: str, head: str) -> tuple[int, set[str]] | None:
    """Return (number, label names) of the open PR for head -> base, or None."""
    result = run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            repo,
            "--head",
            head,
            "--base",
            base,
            "--state",
            "open",
            "--json",
            "number,labels",
        ]
    )
    if result.returncode != 0:
        print(
            f"::warning::could not list open PRs for {head} -> {base}: "
            f"{result.stderr.strip()}"
        )
        return None
    prs = json.loads(result.stdout or "[]")
    if not prs:
        return None
    labels = {lbl.get("name", "") for lbl in prs[0].get("labels") or []}
    return int(prs[0]["number"]), labels


def clear_opt_in(repo: str, base: str, head: str) -> str:
    """Remove the opt-in label from the open bump PR. Returns what happened."""
    ensure_label_exists(repo)
    found = open_pr_labels(repo, base, head)
    if found is None:
        return "no-open-pr"
    number, labels = found
    if OPT_IN_LABEL not in labels:
        return "absent"
    result = run(
        [
            "gh",
            "pr",
            "edit",
            str(number),
            "--repo",
            repo,
            "--remove-label",
            OPT_IN_LABEL,
        ]
    )
    if result.returncode != 0:
        print(
            f"::warning::could not remove '{OPT_IN_LABEL}' from {repo}#{number}; "
            f"the next push runs full CI: {result.stderr.strip()}"
        )
        return "remove-failed"
    print(f"Removed '{OPT_IN_LABEL}' from {repo}#{number} ahead of the bump push.")
    return "removed"


def commit_status(repo: str, sha: str, context: str) -> str | None:
    """The newest state of ``context`` on ``sha``; "" when absent, None when
    the statuses cannot be read."""
    result = run(
        [
            "gh",
            "api",
            f"repos/{repo}/commits/{sha}/status?per_page=100",
            "--paginate",
            "--jq",
            f'.statuses[] | select(.context == "{context}") | .state',
        ]
    )
    if result.returncode != 0:
        print(
            f"::warning::could not read the `{context}` status on {sha}: "
            f"{result.stderr.strip()}"
        )
        return None
    states = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return states[-1] if states else ""


def is_opted_in(repo: str, head_ref: str, sha: str) -> tuple[bool, str]:
    """Return (run CI?, reason). Only a bump-version PR can be not opted in."""
    if not head_ref.startswith(BUMP_BRANCH_PREFIX) or not sha:
        return True, "not a bump-version PR"
    state = commit_status(repo, sha, GATE_CHECK_CONTEXT)
    if state is None:
        return True, "opt-in status unreadable; failing open"
    if state in ("success", "pending"):
        return True, "bump-version PR opted in (/gate-check)"
    return False, "bump-version PR CI is opt-in: comment /gate-check on the PR"


def is_e2e_requested(
    repo: str, head_ref: str, sha: str, *, fork: bool, author: str
) -> tuple[bool, str]:
    """Return (run e2e?, reason) for the SDK's PR Checks."""
    if fork or author == "dependabot[bot]":
        return False, "fork and Dependabot PRs never run e2e (no secrets)"
    if not sha:
        return False, "no head commit"
    if commit_status(repo, sha, E2E_REQUEST_CONTEXT) == "success":
        return True, "e2e requested on this commit (/e2e)"
    if head_ref == SDK_E2E_BUMP_BRANCH:
        if commit_status(repo, sha, GATE_CHECK_CONTEXT) in ("success", "pending"):
            return True, "release gate opted in on this commit (/gate-check)"
    return False, "e2e not requested; comment /e2e on the PR to run it"


def emit(**outputs: object) -> None:
    lines = [f"{key}={value}" for key, value in outputs.items()]
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
    else:
        print("\n".join(lines))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="owner/repo (defaults to $GITHUB_REPOSITORY)",
    )
    sub = parser.add_subparsers(dest="mode", required=True)
    clear = sub.add_parser("clear", help="remove the opt-in before a bump push")
    clear.add_argument("--base", required=True, help="target branch, e.g. main")
    clear.add_argument(
        "--head", required=True, help="bump branch, e.g. bump-version-main"
    )
    check = sub.add_parser("check", help="emit opted_in=<true|false> and reason")
    check.add_argument("--head-ref", default="", help="the PR head ref; empty off-PR")
    check.add_argument("--sha", default="", help="the PR head SHA; empty off-PR")
    e2e = sub.add_parser("e2e-requested", help="emit requested=<true|false>")
    e2e.add_argument("--head-ref", default="")
    e2e.add_argument("--sha", default="")
    e2e.add_argument("--fork", default="false", help="head.repo.fork")
    e2e.add_argument("--author", default="", help="the PR author login")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when $GITHUB_REPOSITORY is unset")
    if args.mode == "clear":
        clear_opt_in(args.repo, args.base, args.head)
        return 0
    if args.mode == "e2e-requested":
        requested, reason = is_e2e_requested(
            args.repo,
            args.head_ref,
            args.sha,
            fork=args.fork.strip().lower() == "true",
            author=args.author,
        )
        print(reason)
        emit(requested=str(requested).lower(), reason=reason)
        return 0
    opted_in, reason = is_opted_in(args.repo, args.head_ref, args.sha)
    print(reason)
    emit(opted_in=str(opted_in).lower(), reason=reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
