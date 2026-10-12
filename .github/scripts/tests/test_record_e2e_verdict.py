"""Tests for .github/scripts/record_e2e_verdict.py (FND-3650).

The Release Gate reads the combined status, which shows only the newest `e2e`
row, so an older attempt finishing after a newer one started must not write,
and a write that races a newer attempt's `pending` must be repaired.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import record_e2e_verdict  # noqa: E402
from record_e2e_verdict import main, reconcile, record  # noqa: E402

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


def _record(run, run_id: int = SELF) -> bool:
    url = f"https://github.com/{REPO}/actions/runs/{run_id}"
    return record(
        REPO,
        SHA,
        run_id,
        "success",
        "End-to-end suite success",
        url,
        run=run,
        sleep=lambda _: None,
    )


class _History:
    """A commit's status history that POSTs append to, newest id last."""

    def __init__(self, *attempts: tuple[int, str]) -> None:
        self.rows: list[dict] = []
        self.before_next_post: list = []
        for run_id, state in attempts:
            self.add(run_id, state)

    def add(self, run_id: int, state: str) -> None:
        self.rows.append(
            {
                "id": len(self.rows) + 1,
                "context": "e2e",
                "state": state,
                "description": f"run {run_id}",
                "target_url": f"https://github.com/{REPO}/actions/runs/{run_id}",
            }
        )

    def combined(self) -> tuple[int, str]:
        head = self.rows[-1]
        return int(head["target_url"].rsplit("/", 1)[1]), head["state"]

    def run(self, args: list) -> str:
        if args[1] != "-X":
            return json.dumps([self.rows])
        while self.before_next_post:
            self.before_next_post.pop(0)()
        field = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a}
        self.add(int(field["target_url"].rsplit("/", 1)[1]), field["state"])
        return '{"id": 1}'


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


def test_repairs_a_newer_pending_that_raced_this_write() -> None:
    """A newer attempt posts `pending` after this run's read, before its write."""
    newer = SELF + 5
    history = _History((SELF, "pending"))
    history.before_next_post.append(lambda: history.add(newer, "pending"))
    assert _record(history.run)
    assert history.combined() == (newer, "pending")


def test_the_newest_attempt_repairs_a_stale_row_from_an_older_one() -> None:
    """The older run's late row landed after this run's final verdict."""
    newer = SELF + 5
    history = _History((SELF, "pending"), (newer, "pending"))
    assert _record(history.run, run_id=newer)
    history.add(SELF, "failure")
    assert reconcile(REPO, SHA, history.run, sleep=lambda _: None)
    assert history.combined() == (newer, "success")


def test_reconcile_fails_open_when_the_history_is_unreadable(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert not reconcile(REPO, SHA, lambda args: "", sleep=lambda _: None)
    assert "cannot confirm" in capsys.readouterr().err


def test_a_rejected_post_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    posts: list = []
    assert not _record(_stub(_rows(), posts, post_ok=False))
    assert "could not record" in capsys.readouterr().err


def test_main_always_exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(record_e2e_verdict, "_run_gh", lambda args: "")
    args = ["--repo", REPO, "--head-sha", SHA, "--run-id", str(SELF)]
    args += ["--state", "error", "--description", "x", "--target-url", URL]
    assert main(args) == 0
