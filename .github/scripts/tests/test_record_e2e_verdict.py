"""Tests for .github/scripts/record_e2e_verdict.py (FND-3650).

The Release Gate reads the combined status, which shows only the newest `e2e`
row, so an older attempt finishing after a newer one started must not write.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import record_e2e_verdict  # noqa: E402
from record_e2e_verdict import main, record  # noqa: E402

REPO = "atlanhq/atlan-example-app"
SHA = "a96a27b0000000000000000000000000000000aa"
SELF = 38060382427
URL = f"https://github.com/{REPO}/actions/runs/{SELF}"


def _rows(*attempts: tuple[int, str]) -> str:
    rows = [
        {
            "id": len(attempts) - i,
            "context": "e2e",
            "state": state,
            "target_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
        }
        for i, (run_id, state) in enumerate(attempts)
    ]
    return json.dumps([rows])


def _stub(listing: str, posts: list, *, post_ok: bool = True):
    def run(args: list) -> str:
        if args[1] == "-X":
            posts.append(args)
            return '{"id": 1}' if post_ok else ""
        return listing

    return run


def _record(run) -> bool:
    return record(REPO, SHA, SELF, "success", "End-to-end suite success", URL, run=run)


def test_records_when_this_run_is_the_newest_attempt() -> None:
    posts: list = []
    assert _record(_stub(_rows((SELF, "pending")), posts))
    assert len(posts) == 1
    post = posts[0]
    assert f"repos/{REPO}/statuses/{SHA}" in post
    assert "state=success" in post and "context=e2e" in post
    assert f"target_url={URL}" in post


def test_skips_when_a_newer_attempt_has_posted() -> None:
    posts: list = []
    listing = _rows((SELF + 5, "pending"), (SELF, "pending"))
    assert not _record(_stub(listing, posts))
    assert posts == []


def test_records_anyway_when_the_attempts_are_unreadable() -> None:
    """Leaving this run's `pending` behind is worse than a rare stale row."""
    posts: list = []
    assert _record(_stub("", posts))
    assert len(posts) == 1


def test_a_rejected_post_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    posts: list = []
    assert not _record(_stub(_rows(), posts, post_ok=False))
    assert "could not record" in capsys.readouterr().err


def test_main_always_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record_e2e_verdict, "_run_gh", lambda args: "")
    args = ["--repo", REPO, "--head-sha", SHA, "--run-id", str(SELF)]
    args += ["--state", "error", "--description", "x", "--target-url", URL]
    assert main(args) == 0
