"""Tests for .github/scripts/e2e_branch_recheck.py (FND-3839).

A dispatched e2e run stands down only when the branch tip is positively a
different commit. Every doubt reads as ``superseded=false``: wrongly standing
down denies a live commit its e2e, wrongly carrying on only costs tenant time.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import e2e_branch_recheck as recheck  # noqa: E402

REPO = "atlanhq/atlan-example-app"
TESTED = "a" * 40
NEWER = "b" * 40


def _gh(code: int = 0, stdout: str = "", calls: list[list[str]] | None = None):
    def run(args: list[str]) -> subprocess.CompletedProcess:
        if calls is not None:
            calls.append(args)
        return subprocess.CompletedProcess(args, code, stdout, "boom" if code else "")

    return run


def _branch(sha: Any) -> str:
    return json.dumps({"commit": {"sha": sha}})


class TestSuperseded:
    @pytest.mark.parametrize("event", ["pull_request", "push", "merge_group"])
    def test_not_a_dispatch_never_asks(self, event: str) -> None:
        calls: list[list[str]] = []
        assert not recheck.superseded(
            REPO, event, "feat/x", TESTED, _gh(stdout=_branch(NEWER), calls=calls)
        )
        assert calls == []

    @pytest.mark.parametrize(("branch", "sha"), [("", TESTED), ("feat/x", "abc1234")])
    def test_no_branch_or_short_sha_is_false(self, branch: str, sha: str) -> None:
        assert not recheck.superseded(
            REPO, "workflow_dispatch", branch, sha, _gh(stdout=_branch(NEWER))
        )

    def test_moved_branch_is_superseded(self) -> None:
        assert recheck.superseded(
            REPO, "workflow_dispatch", "feat/x", TESTED, _gh(stdout=_branch(NEWER))
        )

    def test_same_tip_is_not_superseded(self) -> None:
        assert not recheck.superseded(
            REPO, "workflow_dispatch", "feat/x", TESTED, _gh(stdout=_branch(TESTED))
        )

    @pytest.mark.parametrize(
        "gh",
        [
            _gh(code=1),
            _gh(stdout="not json"),
            _gh(stdout="{}"),
            _gh(stdout=json.dumps({"commit": None})),
            _gh(stdout=_branch(None)),
            _gh(stdout=_branch("b" * 7)),
            _gh(stdout="[]"),
        ],
        ids=[
            "error",
            "garbage",
            "no-commit",
            "null-commit",
            "null-sha",
            "short",
            "list",
        ],
    )
    def test_unreadable_or_malformed_is_false(self, gh) -> None:
        assert not recheck.superseded(REPO, "workflow_dispatch", "feat/x", TESTED, gh)

    def test_branch_name_is_url_quoted(self) -> None:
        """A `/` in the branch must not turn into a different API path."""
        calls: list[list[str]] = []
        recheck.branch_tip(
            REPO, "bump-version/main", _gh(stdout=_branch(TESTED), calls=calls)
        )
        assert calls == [["api", f"repos/{REPO}/branches/bump-version%2Fmain"]]


class TestMain:
    @pytest.mark.parametrize(
        ("tip", "expected"), [(NEWER, "superseded=true"), (TESTED, "superseded=false")]
    )
    def test_prints_the_answer_and_exits_zero(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tip: str,
        expected: str,
    ) -> None:
        def run(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
            assert cmd[0] == "gh" and "timeout" in kwargs
            return subprocess.CompletedProcess(cmd, 0, _branch(tip), "")

        monkeypatch.setattr(recheck, "_RUNNER", run)
        rc = recheck.main(
            [
                "--repo",
                REPO,
                "--event-name",
                "workflow_dispatch",
                "--branch",
                "feat/x",
                "--tested-sha",
                TESTED,
            ]
        )
        assert rc == 0
        assert capsys.readouterr().out.strip() == expected

    def test_timeout_is_false_and_exits_zero(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def hang(cmd: list[str], **kwargs: Any) -> None:
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

        monkeypatch.setattr(recheck, "_RUNNER", hang)
        rc = recheck.main(
            [
                "--repo",
                REPO,
                "--event-name",
                "workflow_dispatch",
                "--branch",
                "feat/x",
                "--tested-sha",
                TESTED,
            ]
        )
        assert rc == 0
        assert capsys.readouterr().out.strip() == "superseded=false"

    def test_branch_defaults_to_empty(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            recheck, "_RUNNER", lambda *a, **k: pytest.fail("must not call gh")
        )
        rc = recheck.main(
            [
                "--repo",
                REPO,
                "--event-name",
                "workflow_dispatch",
                "--tested-sha",
                TESTED,
            ]
        )
        assert rc == 0
        assert capsys.readouterr().out.strip() == "superseded=false"
