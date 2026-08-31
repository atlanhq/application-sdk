#!/usr/bin/env python3
"""Open a release/bump PR, or bring an already-open one back in sync.

Every release automation in this repo pushes to a *fixed* branch
(``bump-version-main``, ``release/conformance-vX``, …) and force-pushes fresh
content onto it on each re-fire. The PR that tracks that branch is opened once
and then reused. The bug this script exists to remove: the reuse path only
logged "PR already exists, skipping creation", so the branch content moved on
while the PR's title and body stayed frozen at whatever the *first* run wrote.

Concretely, on atlanhq/atlan-oracle-app#258 the title and body said
"0.3.1 → 0.3.2" while the diff bumped ``pyproject.toml`` to ``0.4.0``: the
first run computed a patch bump, a later merge added a ``feat:`` commit that
turned it into a minor bump, and only the files were updated. The tag itself is
derived from ``pyproject.toml`` by tag-and-release.yaml, never from the title,
so nothing mis-shipped — but the human approving the release PR was reading a
version number that no longer matched what merging it would release.

So: upsert, don't create-or-skip. Create when there is no open PR for the
branch; otherwise edit the existing one back into agreement with the branch.

Also ensures the release label on *both* paths. Previously the label was only
applied at creation, and ``tag-and-release.yaml`` gates on
``contains(labels.*.name, 'release')`` — an open bump PR that lost its label
would merge with no tag and no GitHub Release.

Usage::

    python3 .github/scripts/upsert_release_pr.py \
        --repo owner/name \
        --base main \
        --head bump-version-main \
        --title "Bump version to 1.2.3" \
        --body-file /tmp/body.md \
        --label release

Prints ``pr_number=<n>`` and ``pr_action=<created|updated|unchanged>`` to
$GITHUB_OUTPUT (or stdout when unset), mirroring create_check_run.py.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    """Single seam so tests can stub the `gh` CLI."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("check", False)
    return subprocess.run(cmd, **kwargs)


def _gh(cmd: list[str], what: str) -> str:
    result = run(cmd)
    if result.returncode != 0:
        # Loud on failure by design. The push has already landed by the time we
        # run, so a silent failure here leaves exactly the stale-title state
        # this script exists to prevent — but green. A red job is the signal.
        raise SystemExit(f"::error::failed to {what}: {result.stderr.strip()}")
    return result.stdout


def find_open_pr(repo: str, base: str, head: str) -> dict | None:
    """Return the open PR for ``head`` -> ``base``, or None.

    ``--head`` is matched without an ``owner:`` prefix because these branches
    always live in the same repo (the automation pushes to origin), which is
    what ``gh pr list`` assumes for a bare branch name.
    """
    stdout = _gh(
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
            "number,title,body,labels",
        ],
        f"list open PRs for {head} -> {base} on {repo}",
    )
    prs = json.loads(stdout or "[]")
    return prs[0] if prs else None


def ensure_label_exists(repo: str, label: str) -> None:
    """Create the label if the repo doesn't have it yet.

    Best-effort: an "already exists" failure is the common case and is not an
    error. A genuinely broken token surfaces on the create/edit call instead.
    """
    run(
        [
            "gh",
            "label",
            "create",
            label,
            "--repo",
            repo,
            "--color",
            "0e8a16",
            "--description",
            "Release version-bump PR",
        ]
    )


def create_pr(
    repo: str, base: str, head: str, title: str, body_file: str, label: str | None
) -> int:
    cmd = [
        "gh",
        "pr",
        "create",
        "--repo",
        repo,
        "--base",
        base,
        "--head",
        head,
        "--title",
        title,
        "--body-file",
        body_file,
    ]
    if label:
        ensure_label_exists(repo, label)
        cmd += ["--label", label]
    _gh(cmd, f"create PR for {head} -> {base} on {repo}")
    pr = find_open_pr(repo, base, head)
    if pr is None:
        raise SystemExit(
            f"::error::created a PR for {head} -> {base} on {repo} but could not read it back"
        )
    return int(pr["number"])


def sync_pr(
    repo: str, pr: dict, title: str, body: str, body_file: str, label: str | None
) -> str:
    """Bring an open PR's title/body/label back in line with the branch.

    Returns "updated" if anything was written, "unchanged" otherwise. The
    no-op path matters: ``gh pr edit`` fires a ``pull_request: edited`` webhook,
    and there is no reason to emit one on every merge to main when the bump is
    already described correctly.
    """
    number = str(pr["number"])
    changed = False

    if pr.get("title") != title or _normalize(pr.get("body")) != _normalize(body):
        _gh(
            [
                "gh",
                "pr",
                "edit",
                number,
                "--repo",
                repo,
                "--title",
                title,
                "--body-file",
                body_file,
            ],
            f"update title/body on {repo}#{number}",
        )
        changed = True

    if label and label not in {lbl.get("name") for lbl in pr.get("labels") or []}:
        ensure_label_exists(repo, label)
        _gh(
            ["gh", "pr", "edit", number, "--repo", repo, "--add-label", label],
            f"add label '{label}' to {repo}#{number}",
        )
        changed = True

    return "updated" if changed else "unchanged"


def _normalize(body: str | None) -> str:
    """Compare bodies ignoring line-ending and trailing-whitespace noise.

    GitHub returns CRLF line endings for bodies it stored from a CRLF source,
    so a byte comparison against a locally-generated LF body would report a
    difference on every run and edit forever.
    """
    if not body:
        return ""
    return "\n".join(
        line.rstrip() for line in body.replace("\r\n", "\n").split("\n")
    ).strip()


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
    parser.add_argument("--base", required=True, help="base branch, e.g. main")
    parser.add_argument("--head", required=True, help="bump branch to open the PR from")
    parser.add_argument("--title", required=True, help="PR title")
    parser.add_argument(
        "--body-file", required=True, help="path to a file holding the PR body"
    )
    parser.add_argument(
        "--label",
        default="",
        help="label to ensure on the PR (created if the repo lacks it). Empty to skip.",
    )
    args = parser.parse_args(argv)

    if not args.repo:
        parser.error("--repo is required when $GITHUB_REPOSITORY is unset")

    body = Path(args.body_file).read_text(encoding="utf-8")
    label = args.label or None

    existing = find_open_pr(args.repo, args.base, args.head)
    if existing is None:
        number = create_pr(
            args.repo, args.base, args.head, args.title, args.body_file, label
        )
        print(f"Opened {args.repo}#{number} for {args.head} -> {args.base}.")
        emit(pr_number=number, pr_action="created")
        return 0

    action = sync_pr(args.repo, existing, args.title, body, args.body_file, label)
    number = int(existing["number"])
    if action == "updated":
        print(f"Synced {args.repo}#{number} to match {args.head}: {args.title}")
    else:
        print(f"{args.repo}#{number} already matches {args.head}; nothing to update.")
    emit(pr_number=number, pr_action=action)
    return 0


if __name__ == "__main__":
    sys.exit(main())
