#!/usr/bin/env python3
"""Reset the CI opt-in on a bump-version PR before its branch moves (FND-3748).

CI on a ``bump-version-*`` PR is opt-in: nothing heavy runs on it until a
reviewer adds the ``gate-check`` label, which means "run this lane's full
release gate" (for an app or the SDK that includes tenant e2e). The bump branch
is force-pushed on every merge to its target, and every changelog entry now
moves it, so the opt-in has to be one-shot: it covers the commit it was given
for, and a new bump commit needs a fresh one.

This script runs *before* the push. The ``synchronize`` event a push fires
carries the PR's labels as they are at that moment, so removing the label after
the push would let the stale opt-in start a full CI run on the new commit.

It also creates the label when the repo lacks it, so a reviewer can add it from
the UI on any repo the release automation runs in.

``check`` answers "is this PR opted in?" from the PR's *live* labels, for a
gate that must not subscribe to ``labeled`` (the SDK Gate: on an ordinary PR an
unrelated label would skip its inline required job, and a skipped required
check reads as a pass over an earlier failure). Adding ``gate-check`` instead
re-runs that gate (``bump-pr-gate-check.yaml``), and the re-run reads the label
here, because a re-run replays the original, label-less event payload.

Both modes fail open towards running CI: a label that cannot be removed or
read costs runner minutes, never a missed gate.

Usage::

    python3 .github/scripts/bump_pr_opt_in.py clear \
        --repo owner/name --base main --head bump-version-main
    python3 .github/scripts/bump_pr_opt_in.py check \
        --repo owner/name --head-ref "$HEAD_REF" --pr "$PR_NUMBER"
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

OPT_IN_LABEL = "gate-check"
# Still honoured while the fleet resyncs onto `gate-check`.
LEGACY_OPT_IN_LABEL = "e2e"
BUMP_BRANCH_PREFIX = "bump-version"
OPT_IN_DESCRIPTION = "Run the full release gate on this bump-version PR's head"
OPT_IN_COLOR = "fbca04"


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def ensure_label_exists(repo: str) -> None:
    """Create the opt-in label if missing. "Already exists" is the common case."""
    run(
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


def pr_labels(repo: str, pr: str) -> set[str] | None:
    """Return the PR's current label names, or None when they can't be read."""
    result = run(["gh", "api", f"repos/{repo}/pulls/{pr}", "--jq", ".labels[].name"])
    if result.returncode != 0:
        print(
            f"::warning::could not read labels of {repo}#{pr}; running CI: "
            f"{result.stderr.strip()}"
        )
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def is_opted_in(repo: str, head_ref: str, pr: str) -> tuple[bool, str]:
    """Return (run CI?, reason). Only a bump-version PR can be not opted in."""
    if not head_ref.startswith(BUMP_BRANCH_PREFIX) or not pr:
        return True, "not a bump-version PR"
    labels = pr_labels(repo, pr)
    if labels is None:
        return True, "labels unreadable; failing open"
    if OPT_IN_LABEL in labels or LEGACY_OPT_IN_LABEL in labels:
        return True, f"bump-version PR opted in ({OPT_IN_LABEL})"
    return False, f"bump-version PR CI is opt-in: add the '{OPT_IN_LABEL}' label"


def latest_run(repo: str, workflow: str, sha: str) -> dict | None:
    """Return the newest run of ``workflow`` on ``sha`` (id + status), or None."""
    result = run(
        [
            "gh",
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow,
            "--commit",
            sha,
            "--limit",
            "1",
            "--json",
            "databaseId,status",
        ]
    )
    if result.returncode != 0:
        raise SystemExit(
            f"::error::could not list {workflow} runs on {sha}: {result.stderr.strip()}"
        )
    runs = json.loads(result.stdout or "[]")
    return runs[0] if runs else None


def rerun_gate(
    repo: str,
    workflow: str,
    sha: str,
    *,
    timeout_s: float = 600,
    poll_s: float = 15,
    sleep=time.sleep,
) -> str:
    """Re-run the newest ``workflow`` run on ``sha`` so it re-reads the opt-in.

    Waits for an in-flight run to finish first: GitHub refuses to re-run a run
    that is still going, and that run already decided "not opted in" before the
    label landed. Loud on failure — a reviewer who added ``gate-check`` and saw
    nothing happen would otherwise be left waiting on a gate that never re-runs.
    """
    waited = 0.0
    while True:
        found = latest_run(repo, workflow, sha)
        if found is None:
            raise SystemExit(
                f"::error::no {workflow} run on {sha} to re-run; push or re-open the PR"
            )
        if found.get("status") == "completed":
            break
        if waited >= timeout_s:
            raise SystemExit(
                f"::error::{workflow} run {found['databaseId']} on {sha} still "
                f"{found.get('status')} after {int(timeout_s)}s; re-run it by hand"
            )
        sleep(poll_s)
        waited += poll_s
    run_id = str(found["databaseId"])
    result = run(["gh", "run", "rerun", run_id, "--repo", repo])
    if result.returncode != 0:
        raise SystemExit(
            f"::error::could not re-run {workflow} run {run_id}: {result.stderr.strip()}"
        )
    print(f"Re-ran {workflow} run {run_id} on {sha} for the '{OPT_IN_LABEL}' opt-in.")
    return run_id


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
    check.add_argument("--pr", default="", help="the PR number; empty off-PR")
    rerun = sub.add_parser("rerun", help="re-run a gate workflow's newest run")
    rerun.add_argument(
        "--workflow", required=True, help="workflow file, e.g. sdk-gate.yaml"
    )
    rerun.add_argument("--sha", required=True, help="the PR head SHA")
    args = parser.parse_args(argv)
    if not args.repo:
        parser.error("--repo is required when $GITHUB_REPOSITORY is unset")
    if args.mode == "clear":
        clear_opt_in(args.repo, args.base, args.head)
        return 0
    if args.mode == "rerun":
        rerun_gate(args.repo, args.workflow, args.sha)
        return 0
    opted_in, reason = is_opted_in(args.repo, args.head_ref, args.pr)
    print(reason)
    emit(opted_in=str(opted_in).lower(), reason=reason)
    return 0


if __name__ == "__main__":
    sys.exit(main())
