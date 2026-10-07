"""Tests for .github/scripts/renovate_rebase_conflicted.py (FND-3481 backstop)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import renovate_rebase_conflicted as backstop  # noqa: E402


def pr(repo: str, mergeable: str = "CONFLICTING", branch: str = "renovate/x") -> dict:
    return {
        "mergeable": mergeable,
        "headRefName": branch,
        "repository": {"nameWithOwner": repo},
    }


class FakeGh:
    """Records gh calls; answers run-listing queries with ``busy`` counts."""

    def __init__(self, busy: dict[str, int] | None = None, fail: bool = False):
        self.busy = busy or {}
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, args, **_kwargs):
        self.calls.append(args)
        if self.fail:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 403")
        out = ""
        if args[1] == "api":
            status = args[2].split("status=")[1].split("&")[0]
            out = str(self.busy.get(status, 0))
        return subprocess.CompletedProcess(args, 0, out, "")

    @property
    def dispatches(self) -> list[list[str]]:
        return [c for c in self.calls if c[1:3] == ["workflow", "run"]]


def run(prs: list[dict], gh: FakeGh, dry_run: bool = False) -> list[str]:
    seen: list[str] = []

    def fetch(token, query, fields):
        seen.append(query)
        return prs

    result = backstop.run(
        org="atlanhq",
        home_repo="atlanhq/application-sdk",
        fleet_token="t",
        dry_run=dry_run,
        fetch=fetch,
        runner=gh,
    )
    assert seen == ["org:atlanhq is:pr is:open author:app/atlan-app-fleet"]
    return result


class TestConflictedRepos:
    def test_only_conflicting_renovate_branches_count(self):
        assert backstop.conflicted_repos(
            [
                pr("atlanhq/b-app"),
                pr("atlanhq/a-app"),
                pr("atlanhq/a-app"),
                pr("atlanhq/c-app", mergeable="MERGEABLE"),
                pr("atlanhq/d-app", mergeable="UNKNOWN"),
                pr("atlanhq/e-app", branch="conformance/resync"),
            ]
        ) == ["atlanhq/a-app", "atlanhq/b-app"]

    def test_application_sdk_is_never_a_target(self):
        # Excluded from the runner's own discovery: Mend serves it.
        assert backstop.conflicted_repos([pr("atlanhq/application-sdk")]) == []

    def test_malformed_nodes_are_skipped(self):
        assert backstop.conflicted_repos([{}, None, {"mergeable": "CONFLICTING"}]) == []


class TestRun:
    def test_dispatches_one_scoped_run_for_all_conflicted_repos(self):
        gh = FakeGh()
        assert run([pr("atlanhq/b-app"), pr("atlanhq/a-app")], gh) == [
            "atlanhq/a-app",
            "atlanhq/b-app",
        ]
        assert len(gh.dispatches) == 1
        call = gh.dispatches[0]
        assert call[call.index("--repo") + 1] == "atlanhq/application-sdk"
        field = call[call.index("-f") + 1]
        assert field.startswith("repos=")
        assert json.loads(field.removeprefix("repos=")) == [
            "atlanhq/a-app",
            "atlanhq/b-app",
        ]

    def test_nothing_conflicted_makes_no_api_calls(self):
        gh = FakeGh()
        assert run([pr("atlanhq/a-app", mergeable="MERGEABLE")], gh) == []
        assert gh.calls == []

    @pytest.mark.parametrize("status", ["queued", "in_progress"])
    def test_a_busy_renovate_workflow_suppresses_the_dispatch(self, status):
        gh = FakeGh(busy={status: 1})
        assert run([pr("atlanhq/a-app")], gh) == []
        assert gh.dispatches == []

    def test_dry_run_does_not_dispatch(self):
        gh = FakeGh()
        assert run([pr("atlanhq/a-app")], gh, dry_run=True) == []
        assert gh.dispatches == []

    def test_a_failed_gh_call_fails_the_run(self):
        # Fail loud: a backstop that silently stops dispatching is not one.
        with pytest.raises(RuntimeError):
            run([pr("atlanhq/a-app")], FakeGh(fail=True))


def test_main_requires_both_tokens(monkeypatch):
    monkeypatch.delenv("FLEET_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", "atlanhq/application-sdk")
    assert backstop.main([]) == 1
