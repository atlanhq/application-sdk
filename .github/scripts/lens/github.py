"""The GitHub REST calls lens makes — nothing more.

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
from typing import Any

API = "https://api.github.com"
BOT_LOGINS = {"github-actions[bot]"}


class GitHubError(RuntimeError):
    pass


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
            with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - fixed api.github.com
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
        return str((data or {}).get("status") or "unknown")

    def file_at(self, path: str, ref: str) -> str | None:
        q = urllib.parse.quote(path)
        try:
            data = self._call("GET", f"/repos/{self.repo}/contents/{q}?ref={ref}")
        except GitHubError:
            return None
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8", "replace")
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
        return list((data or {}).get("workflow_runs") or [])

    # ---- writes --------------------------------------------------------
    def set_status(
        self, sha: str, state: str, description: str, target_url: str = ""
    ) -> None:
        """The `lens` commit status on `sha`: success | failure | error | pending.
        This is the green/red signal (and what branch protection can require)."""
        body = {"state": state, "context": "lens", "description": description[:140]}
        if target_url:
            body["target_url"] = target_url
        self._call("POST", f"/repos/{self.repo}/statuses/{sha}", body)

    def react(self, comment_id: int, content: str) -> None:
        """A reaction on the `@lens` comment: 👀 started, 🚀 posted, 😕 failed/dropped.
        Best-effort — a reaction that fails must never fail the review."""
        try:
            self._call(
                "POST",
                f"/repos/{self.repo}/issues/comments/{comment_id}/reactions",
                {"content": content},
            )
        except GitHubError:
            pass

    def comment(self, number: int, body: str) -> None:
        self._call(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body}
        )

    def upsert_comment(self, number: int, marker: str, body: str) -> str:
        """One sticky comment per PR, edited in place — never a new one per round.
        Returns the comment's URL (the `lens` status links to it)."""
        for c in self.issue_comments(number):
            if (
                marker in (c.get("body") or "")
                and (c.get("user") or {}).get("login") in BOT_LOGINS
            ):
                out = self._call(
                    "PATCH",
                    f"/repos/{self.repo}/issues/comments/{c['id']}",
                    {"body": body},
                )
                return str((out or {}).get("html_url") or c.get("html_url") or "")
        out = self._call(
            "POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": body}
        )
        return str((out or {}).get("html_url") or "")

    def review(
        self, number: int, head: str, body: str, comments: list[dict[str, Any]]
    ) -> None:
        if not comments:
            return
        self._call(
            "POST",
            f"/repos/{self.repo}/pulls/{number}/reviews",
            {"commit_id": head, "event": "COMMENT", "body": body, "comments": comments},
        )
