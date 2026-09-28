"""Everything vuln-triage does outside its own process: Linear, PyPI, gh, git, uv.

Kept thin and injectable (`Runner`, `urlopen`) so run.py can be tested end to end without
a network.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# The Linear client the scan and the reconcile already use; .github/scripts is on
# sys.path both under `python -m vuln_triage` (run from there) and in the tests.
import security_scan_create_linear as ssl

Runner = Callable[..., subprocess.CompletedProcess]

SCAN_WORKFLOW = "daily-security-scan.yml"
SCAN_ARTIFACT = "security-scan-raw-results"
USER_AGENT = "vuln-triage/1.0 (+https://github.com/atlanhq/application-sdk)"


# --------------------------------------------------------------------------- Linear


def fetch_issue(identifier: str) -> dict[str, Any]:
    data = ssl.gql(
        """
        query Issue($id: String!) {
          issue(id: $id) { id identifier url title description createdAt }
        }
        """,
        {"id": identifier},
    )
    issue = data.get("issue")
    if not issue:
        raise SystemExit(f"::error::Linear issue {identifier} not found")
    return issue


def ticket_cves(description: str | None) -> list[str]:
    """The CVE ids the ticket tracks, from its `<!-- vuln-ids: ... -->` marker."""
    return sorted(ssl.extract_vuln_ids_from_description(description))


def comment(issue_id: str, body: str) -> None:
    ssl.gql(
        """
        mutation Comment($input: CommentCreateInput!) {
          commentCreate(input: $input) { success }
        }
        """,
        {"input": {"issueId": issue_id, "body": body}},
    )


# --------------------------------------------------------------------------- PyPI


def pypi_upstream_check(
    stale_after_days: int,
    now: datetime,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
) -> Callable[[str], bool | None]:
    """A Case 2/3 oracle: is the package's newest PyPI release inside the window?

    Public, read-only package metadata; None when PyPI cannot be read."""

    def check(package: str) -> bool | None:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{package}/json",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            return None
        uploads = [
            f.get("upload_time_iso_8601", "")
            for files in (data.get("releases") or {}).values()
            for f in files or []
        ]
        uploads = [u for u in uploads if u]
        if not uploads:
            return None
        newest = datetime.fromisoformat(max(uploads).replace("Z", "+00:00"))
        return now - newest <= timedelta(days=stale_after_days)

    return check


# --------------------------------------------------------------------------- scan artifacts


def newest_successful_run(runs: list[dict[str, Any]]) -> str:
    ok = [r for r in runs if r.get("conclusion") == "success"]
    if not ok:
        return ""
    return str(max(ok, key=lambda r: r.get("createdAt", ""))["databaseId"])


def download_scan(repo: str, run_id: str, dest: Path, runner: Runner) -> str:
    """Fetch the scan's raw Trivy JSON. Uses the dispatching scan's run when given, so the
    triage reads exactly what produced the ticket and not a later hourly scan; otherwise
    the newest successful scan (manual re-runs)."""
    if not run_id:
        # Not `--status success --limit 1`: observed returning a run days older than the
        # newest successful one. List the recent runs and pick in code instead.
        out = runner(
            [
                "gh",
                "run",
                "list",
                "-R",
                repo,
                "--workflow",
                SCAN_WORKFLOW,
                "--limit",
                "20",
                "--json",
                "databaseId,conclusion,createdAt",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        run_id = newest_successful_run(json.loads(out.stdout or "[]"))
        if not run_id:
            raise SystemExit(f"::error::no successful {SCAN_WORKFLOW} run to read")
    dest.mkdir(parents=True, exist_ok=True)
    runner(
        [
            "gh",
            "run",
            "download",
            run_id,
            "-R",
            repo,
            "-n",
            SCAN_ARTIFACT,
            "-D",
            str(dest),
        ],
        check=True,
    )
    return run_id


# --------------------------------------------------------------------------- git / gh


def _author_env() -> dict[str, str]:
    """The environment for PR calls: the fleet App token (PR_AUTHOR_TOKEN) when set.
    The job's own GITHUB_TOKEN has no pull-requests scope."""
    env = {**os.environ}
    token = os.environ.get("PR_AUTHOR_TOKEN", "")
    if token:
        env["GH_TOKEN"] = token
        env.pop("GITHUB_TOKEN", None)
    return env


def open_pr_url(repo: str, branch: str, runner: Runner) -> str:
    """The open PR for `branch`, or "". A failed lookup raises: reading it as "no PR"
    would open a duplicate under a suffixed branch."""
    out = runner(
        [
            "gh",
            "pr",
            "list",
            "-R",
            repo,
            "--head",
            branch,
            "--state",
            "open",
            "--json",
            "url",
            "--jq",
            '.[0].url // ""',
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_author_env(),
    )
    return (out.stdout or "").strip()


def remote_branch_exists(branch: str, runner: Runner) -> bool:
    out = runner(
        ["git", "ls-remote", "--heads", "origin", branch],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool((out.stdout or "").strip())


def start_branch(branch: str, base: str, runner: Runner) -> None:
    """A fresh branch from the base commit, with a clean tree."""
    runner(["git", "checkout", "--force", "-B", branch, base], check=True)


def commit_and_push(
    branch: str, files: list[str], message: str, runner: Runner
) -> None:
    # Only the files the PR shape allows are staged; never `git add -A`.
    runner(["git", "add", "--", *files], check=True)
    runner(["git", "commit", "-m", message], check=True)
    runner(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], check=True)


def create_pr(
    repo: str, branch: str, title: str, body: str, labels: list[str], runner: Runner
) -> str:
    """Open the PR as the fleet App (PR_AUTHOR_TOKEN), which vuln_auto_merge_gate.py
    trusts. atlan-ci must not author it: it is the approver and cannot approve its own PR.
    """
    cmd = [
        "gh",
        "pr",
        "create",
        "-R",
        repo,
        "--base",
        "main",
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    ]
    for label in labels:
        cmd += ["--label", label]
    out = runner(cmd, check=True, capture_output=True, text=True, env=_author_env())
    return (out.stdout or "").strip().splitlines()[-1] if out.stdout else ""


def uv_lock_upgrade(packages: list[str], runner: Runner) -> subprocess.CompletedProcess:
    cmd = ["uv", "lock"]
    for p in packages:
        cmd += ["--upgrade-package", p]
    return runner(cmd, check=False, capture_output=True, text=True)
