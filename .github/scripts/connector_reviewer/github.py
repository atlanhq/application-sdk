"""The GitHub REST calls connector-reviewer makes — nothing more.

Everything about the PR comes in as data through these calls (the diff, the
head-side text of changed files); the job never checks out or runs PR code.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

from .typed import EMPTY, objects

API = "https://api.github.com"
STATUS_CONTEXT = "connector-reviewer"
DEFAULT_BOT_LOGIN = "atlan-app-fleet[bot]"


def bot_login() -> str:
    """The one identity whose comments connector-reviewer trusts as its own (state, summary).

    connector-reviewer posts with a fleet App token, so this is the App's bot. It is
    deliberately NOT `github-actions[bot]`: any same-repo PR can add a
    workflow that comments as github-actions[bot] and forge connector-reviewer's state
    (reset its spend ledger, close findings); only the App's key can post as
    the App."""
    return os.environ.get("CONNECTOR_REVIEWER_BOT_LOGIN") or DEFAULT_BOT_LOGIN


class GitHubError(RuntimeError):
    pass


COMMAND = "/review"


def safe_body(body: str) -> str:
    """`body` as the reviewer may post it: never opening with the `/review`
    command. The reviewer posts as an App on the trusted list, so a comment of
    its own that started with the command would ask for a review of itself.
    No template starts that way; this holds the line if one ever does."""
    if body.lstrip().lower().startswith(COMMAND):
        return "connector-reviewer: " + body.lstrip()
    return body


class GitHub:
    def __init__(
        self, repo: str, token: str | None = None, transport: Any = None
    ) -> None:
        self.repo = repo
        self.token = token or os.environ.get("GITHUB_TOKEN", "")
        self._transport = transport or self._http

    def _http(
        self,
        method: str,
        path: str,
        body: Any = None,
        accept: str = "application/vnd.github+json",
    ) -> tuple[int, str]:
        url = path if path.startswith("http") else f"{API}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", accept)
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")

    def _call(
        self,
        method: str,
        path: str,
        body: Any = None,
        accept: str = "application/vnd.github+json",
    ) -> Any:
        status, text = self._transport(method, path, body, accept)
        if status >= 300:
            raise GitHubError(f"{method} {path}: HTTP {status}: {text[:300]}")
        if accept.endswith("diff"):
            return text
        return json.loads(text) if text else None

    # ---- reads ---------------------------------------------------------
    def pr(self, number: int) -> dict[str, Any]:
        return self._call("GET", f"/repos/{self.repo}/pulls/{number}")

    def diff(self, base: str, head: str) -> str:
        """base...head (merge-base) diff, the same range the PR shows."""
        return self._call(
            "GET",
            f"/repos/{self.repo}/compare/{base}...{head}",
            accept="application/vnd.github.diff",
        )

    def compare_status(self, base: str, head: str) -> str:
        """ "ahead" when `head` strictly descends from `base` — the only case in
        which an incremental base..head review is sound. Anything else (a
        force-push, a rebase) means a full review."""
        try:
            data = self._call(
                "GET", f"/repos/{self.repo}/compare/{base}...{head}?per_page=1"
            )
        except GitHubError:
            return "unknown"
        return str((data or EMPTY).get("status") or "unknown")

    def merge_base(self, base: str, head: str) -> str:
        """The merge base of `base...head` (what the PR's diff is against), or ""."""
        try:
            data = self._call(
                "GET", f"/repos/{self.repo}/compare/{base}...{head}?per_page=1"
            )
        except GitHubError:
            return ""
        return str(((data or EMPTY).get("merge_base_commit") or EMPTY).get("sha") or "")

    def file_at(self, path: str, ref: str) -> str | None:
        q = urllib.parse.quote(path)
        try:
            data = self._call("GET", f"/repos/{self.repo}/contents/{q}?ref={ref}")
        except GitHubError:
            return None
        if not isinstance(data, dict):
            return None
        blob = cast("dict[str, Any]", data)
        if blob.get("encoding") == "base64":
            return base64.b64decode(blob["content"]).decode("utf-8", "replace")
        return None

    def pr_commit_authors(self, number: int) -> list[str]:
        """The GitHub login of each commit's author, in order; "" for a commit
        GitHub could not attribute to an account. GitHub lists at most 250
        commits of a pull request, so compare the length with the pull
        request's own `commits` count before trusting "every commit"."""
        out: list[str] = []
        page = 1
        while True:
            batch: list[dict[str, Any]] = (
                self._call(
                    "GET",
                    f"/repos/{self.repo}/pulls/{number}/commits?per_page=100&page={page}",
                )
                or []
            )
            out += [str((c.get("author") or EMPTY).get("login") or "") for c in batch]
            if len(batch) < 100:
                return out
            page += 1

    def pr_commit_messages(self, number: int) -> list[str]:
        """The message of each of the pull request's commits, at most the first
        100 (one request): read for a "This reverts commit …" line."""
        batch = self._call(
            "GET", f"/repos/{self.repo}/pulls/{number}/commits?per_page=100"
        )
        return [
            str((c.get("commit") or EMPTY).get("message") or "") for c in objects(batch)
        ]

    def recent_commits(
        self, path: str, ref: str, since: str, limit: int
    ) -> list[dict[str, Any]]:
        """The newest commits reachable from `ref` that touched `path` since
        `since` (ISO 8601), at most `limit` (one request)."""
        q = urllib.parse.urlencode(
            {"sha": ref, "path": path, "since": since, "per_page": max(1, limit)}
        )
        return objects(self._call("GET", f"/repos/{self.repo}/commits?{q}"))

    def commit_patches(self, sha: str) -> dict[str, str]:
        """Each file a commit changed, with its patch text (path -> patch). A
        file GitHub gives no patch for (binary, too large) is left out."""
        data = self._call("GET", f"/repos/{self.repo}/commits/{sha}")
        return {
            str(f.get("filename")): str(f.get("patch"))
            for f in objects((data or EMPTY).get("files"))
            if f.get("filename") and isinstance(f.get("patch"), str)
        }

    def can_push(self, login: str) -> bool:
        """Whether `login` holds Write, Maintain or Admin on this repository,
        read live from the collaborator-permission API.

        `user.permissions.push` is true exactly for those roles and for a
        custom role built on Write; Read and Triage are false. A reply without
        the field reads as no access. A failed call raises GitHubError and the
        caller refuses: authorisation never fails open."""
        q = urllib.parse.quote(login, safe="")
        data = self._call("GET", f"/repos/{self.repo}/collaborators/{q}/permission")
        perms = ((data or EMPTY).get("user") or EMPTY).get("permissions") or EMPTY
        return perms.get("push") is True

    def repo_visibility(self, owner_repo: str) -> str | None:
        """Another repository's `visibility` (public | private | internal) as the
        GitHub API reports it to connector-reviewer's token, or None when the lookup fails (a
        404/403 on a repo the token cannot see, a 5xx, or a network error: DNS,
        a reset or a timeout surface from urlopen as OSError). Never raises."""
        q = urllib.parse.quote(owner_repo, safe="/")
        try:
            data = self._call("GET", f"/repos/{q}")
        except (GitHubError, ValueError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        got = cast("dict[str, object]", data).get("visibility")
        return str(got) if got else None

    def status_of(self, sha: str, context: str) -> dict[str, Any] | None:
        """The latest commit status `context` has on `sha` (`state`,
        `target_url`, `description`, …), or None when it has none. Read from
        the combined status, which holds the newest status per context."""
        data = self._call(
            "GET", f"/repos/{self.repo}/commits/{sha}/status?per_page=100"
        )
        for s in objects((data or EMPTY).get("statuses")):
            if s.get("context") == context:
                return s
        return None

    def issue_comments(self, number: int) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            batch = self._call(
                "GET",
                f"/repos/{self.repo}/issues/{number}/comments?per_page=100&page={page}",
            )
            out.extend(batch)
            if len(batch) < 100:
                return out
            page += 1

    def workflow_runs(self, workflow_file: str) -> list[dict[str, Any]]:
        """Recent runs of one workflow, newest first (one page is enough: a
        run this call must see is at most minutes old)."""
        data = self._call(
            "GET",
            f"/repos/{self.repo}/actions/workflows/{workflow_file}/runs?per_page=50",
        )
        return list((data or EMPTY).get("workflow_runs") or [])

    def run_jobs(self, run_id: int) -> list[dict[str, Any]]:
        """Jobs of a run's latest attempt (connector-reviewer has one job, so one page)."""
        data = self._call(
            "GET", f"/repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=100"
        )
        return list((data or EMPTY).get("jobs") or [])

    # ---- writes --------------------------------------------------------
    def set_status(
        self, sha: str, state: str, description: str, target_url: str = ""
    ) -> None:
        """The `connector-reviewer` commit status on `sha`: success | failure | error | pending.
        This is the green/red signal (and what branch protection can require)."""
        body = {
            "state": state,
            "context": STATUS_CONTEXT,
            "description": description[:140],
        }
        if target_url:
            body["target_url"] = target_url
        self._call("POST", f"/repos/{self.repo}/statuses/{sha}", body)

    def react(self, comment_id: int, content: str) -> None:
        """A reaction on the `/review` comment: 👀 started, 🚀 posted, 😕 failed/dropped.
        Best-effort — a reaction that fails must never fail the review."""
        try:
            self._call(
                "POST",
                f"/repos/{self.repo}/issues/comments/{comment_id}/reactions",
                {"content": content},
            )
        except GitHubError:
            pass

    def comment(self, number: int, body: str) -> int:
        """Posts a PR comment; returns its id (0 if GitHub did not say)."""
        out = self._call(
            "POST",
            f"/repos/{self.repo}/issues/{number}/comments",
            {"body": safe_body(body)},
        )
        return int((out or EMPTY).get("id") or 0)

    def edit_comment(self, comment_id: int, body: str) -> None:
        self._call(
            "PATCH",
            f"/repos/{self.repo}/issues/comments/{comment_id}",
            {"body": safe_body(body)},
        )

    def delete_comment(self, comment_id: int) -> None:
        self._call("DELETE", f"/repos/{self.repo}/issues/comments/{comment_id}")

    def upsert_comment(self, number: int, marker: str, body: str) -> str:
        """One sticky comment per PR, edited in place — never a new one per round.
        Returns the comment's URL (the `connector-reviewer` status links to it)."""
        for c in self.issue_comments(number):
            if (
                marker in (c.get("body") or "")
                and (c.get("user") or EMPTY).get("login") == bot_login()
            ):
                out = self._call(
                    "PATCH",
                    f"/repos/{self.repo}/issues/comments/{c['id']}",
                    {"body": safe_body(body)},
                )
                return str((out or EMPTY).get("html_url") or c.get("html_url") or "")
        out = self._call(
            "POST",
            f"/repos/{self.repo}/issues/{number}/comments",
            {"body": safe_body(body)},
        )
        return str((out or EMPTY).get("html_url") or "")
