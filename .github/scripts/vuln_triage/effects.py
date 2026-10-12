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
from dataclasses import dataclass
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
) -> Callable[[str], tuple[bool | None, str]]:
    """A Case 2/3 oracle: is the package's newest PyPI release inside the window?

    This is the triage's one input that is not in the scan artifact or the checkout:
    live, public, read-only package metadata. So it returns its evidence with the
    verdict, and the evidence is written to the ticket. A re-run can then be checked
    against what PyPI said at the time. The verdict is None when PyPI cannot be read."""

    def check(package: str) -> tuple[bool | None, str]:
        req = urllib.request.Request(
            f"https://pypi.org/pypi/{package}/json",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
        )
        try:
            with urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            return None, f"PyPI unreadable ({type(e).__name__})"
        uploads = [
            f.get("upload_time_iso_8601", "")
            for files in (data.get("releases") or {}).values()
            for f in files or []
        ]
        uploads = [u for u in uploads if u]
        if not uploads:
            return None, "PyPI lists no uploaded releases"
        newest = datetime.fromisoformat(max(uploads).replace("Z", "+00:00"))
        alive = now - newest <= timedelta(days=stale_after_days)
        return alive, (
            f"PyPI newest release {newest.date().isoformat()} "
            f"(staleness window {stale_after_days}d, checked {now.date().isoformat()})"
        )

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


@dataclass(frozen=True)
class OpenPR:
    url: str
    labels: tuple[str, ...] = ()


def open_pr(repo: str, branch: str, runner: Runner) -> OpenPR | None:
    """The open PR for `branch`, or None. A failed lookup raises: reading it as "no PR"
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
            "url,labels",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=_author_env(),
    )
    return parse_open_pr(out.stdout)


def parse_open_pr(stdout: str | None) -> OpenPR | None:
    prs = json.loads(stdout or "[]")
    if not prs:
        return None
    first = prs[0]
    labels = tuple(lbl.get("name", "") for lbl in first.get("labels") or [])
    return OpenPR(url=first.get("url", ""), labels=labels)


def remote_branch_exists(branch: str, runner: Runner) -> bool:
    out = runner(
        ["git", "ls-remote", "--heads", "origin", branch],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool((out.stdout or "").strip())


def reset_to(base: str, runner: Runner) -> None:
    """A clean tree at the base commit, detached. Each change is committed there and
    pushed as `HEAD:refs/heads/<branch>`, so no local branch is left behind (a dry run
    on a laptop leaves nothing to clean up)."""
    runner(["git", "checkout", "--force", "--detach", base], check=True)


def commit_and_push(
    branch: str, files: list[str], message: str, runner: Runner
) -> None:
    # Only the files the PR shape allows are staged; never `git add -A`.
    runner(["git", "add", "--", *files], check=True)
    runner(["git", "commit", "-m", message], check=True)
    runner(["git", "push", "origin", f"HEAD:refs/heads/{branch}"], check=True)


def create_pr(
    repo: str,
    branch: str,
    title: str,
    body: str,
    labels: list[str],
    runner: Runner,
    draft: bool = False,
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
    if draft:
        cmd.append("--draft")
    out = runner(cmd, check=True, capture_output=True, text=True, env=_author_env())
    return (out.stdout or "").strip().splitlines()[-1] if out.stdout else ""


def close_pr(repo: str, url: str, runner: Runner) -> bool:
    """Close a self-test PR and delete its branch. Returns whether it worked; never
    raises, so one failed close cannot stop the others."""
    out = runner(
        [
            "gh",
            "pr",
            "close",
            url,
            "-R",
            repo,
            "--delete-branch",
            "--comment",
            "Self-test complete; closing (fake CVEs, never merged).",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=_author_env(),
    )
    return out.returncode == 0


def uv_lock_upgrade(packages: list[str], runner: Runner) -> subprocess.CompletedProcess:
    cmd = ["uv", "lock"]
    for p in packages:
        cmd += ["--upgrade-package", p]
    return runner(cmd, check=False, capture_output=True, text=True)
