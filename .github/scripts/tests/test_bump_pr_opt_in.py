"""Tests for .github/scripts/bump_pr_opt_in.py (FND-3748).

Covers the three modes the bump-PR CI opt-in rests on:

* ``clear`` — removes ``gate-check`` from the open bump PR before a push, and
  fails open (warns, never raises) so a broken label call can't stop a release;
* ``check`` — answers from the PR's *live* labels, ``true`` off a bump PR and
  whenever the labels can't be read;
* ``rerun`` — waits out an in-flight gate run, then re-runs it, and is loud
  when there is nothing to re-run.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import bump_pr_opt_in


class FakeGh:
    """Stand-in for the `gh` CLI, answering by argv prefix."""

    def __init__(self, responses: dict[tuple[str, ...], list[tuple[int, str]]]):
        self.responses = responses
        self.calls: list[list[str]] = []

    def __call__(self, cmd: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(cmd)
        for prefix, queue in self.responses.items():
            if tuple(cmd[: len(prefix)]) == prefix:
                code, out = queue.pop(0) if len(queue) > 1 else queue[0]
                return subprocess.CompletedProcess(
                    cmd, code, out, "boom" if code else ""
                )
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch):
    def install(responses: dict[tuple[str, ...], list[tuple[int, str]]]) -> FakeGh:
        fake = FakeGh(responses)
        monkeypatch.setattr(bump_pr_opt_in, "run", fake)
        return fake

    return install


def _pr(labels: list[str]) -> str:
    return json.dumps([{"number": 7, "labels": [{"name": n} for n in labels]}])


# ── clear ─────────────────────────────────────────────────────────────────────


class TestClear:
    def test_removes_the_opt_in_and_ensures_the_label_exists(self, gh) -> None:
        fake = gh({("gh", "pr", "list"): [(0, _pr(["release", "gate-check"]))]})
        assert bump_pr_opt_in.clear_opt_in("o/r", "main", "bump-version-main") == (
            "removed"
        )
        assert fake.calls[0][:4] == ["gh", "label", "create", "gate-check"]
        assert [
            "gh",
            "pr",
            "edit",
            "7",
            "--repo",
            "o/r",
            "--remove-label",
            "gate-check",
        ] in fake.calls

    def test_no_edit_when_the_label_is_absent(self, gh) -> None:
        fake = gh({("gh", "pr", "list"): [(0, _pr(["release"]))]})
        assert bump_pr_opt_in.clear_opt_in("o/r", "main", "bump-version-main") == (
            "absent"
        )
        assert not any(c[:3] == ["gh", "pr", "edit"] for c in fake.calls)

    def test_no_open_pr(self, gh) -> None:
        gh({("gh", "pr", "list"): [(0, "[]")]})
        assert (
            bump_pr_opt_in.clear_opt_in("o/r", "main", "bump-version-main")
            == "no-open-pr"
        )

    @pytest.mark.parametrize("failing", [("gh", "pr", "list"), ("gh", "pr", "edit")])
    def test_fails_open(self, gh, failing: tuple[str, ...]) -> None:
        responses = {("gh", "pr", "list"): [(0, _pr(["gate-check"]))]}
        responses[failing] = [(1, "")]
        gh(responses)
        # Never raises: a release push must not be blocked by a label call.
        assert bump_pr_opt_in.clear_opt_in("o/r", "main", "bump-version-main") in {
            "no-open-pr",
            "remove-failed",
        }


class TestEnsureLabel:
    @pytest.mark.parametrize(
        ("code", "stderr", "ok"),
        [
            (0, "", True),
            (1, 'label with name "gate-check" already exists', True),
            (1, "HTTP 403: Resource not accessible by integration", False),
        ],
    )
    def test_only_unexpected_failures_warn(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        code: int,
        stderr: str,
        ok: bool,
    ) -> None:
        monkeypatch.setattr(
            bump_pr_opt_in,
            "run",
            lambda cmd: subprocess.CompletedProcess(cmd, code, "", stderr),
        )
        assert bump_pr_opt_in.ensure_label_exists("o/r") is ok
        assert ("::warning::" in capsys.readouterr().out) is (not ok)


def test_gh_calls_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stalled `gh` call reads as a failed call instead of hanging the job."""

    def hang(cmd, **kwargs):
        assert kwargs["timeout"] == bump_pr_opt_in.GH_TIMEOUT_S
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(bump_pr_opt_in.subprocess, "run", hang)
    result = bump_pr_opt_in.run(["gh", "api", "x"])
    assert result.returncode != 0
    assert "timed out" in result.stderr


# ── check ─────────────────────────────────────────────────────────────────────


class TestCheck:
    @pytest.mark.parametrize(
        ("head_ref", "pr"), [("", ""), ("feat/x", "7"), ("bump-version-main", "")]
    )
    def test_not_a_bump_pr_always_runs(self, gh, head_ref: str, pr: str) -> None:
        fake = gh({})
        assert bump_pr_opt_in.is_opted_in("o/r", head_ref, pr)[0] is True
        assert fake.calls == []

    @pytest.mark.parametrize(
        ("labels", "opted_in"),
        [
            ("release\ngate-check\n", True),
            ("release\ne2e\n", True),
            ("release\n", False),
            ("", False),
        ],
    )
    def test_bump_pr_reads_live_labels(self, gh, labels: str, opted_in: bool) -> None:
        gh({("gh", "api"): [(0, labels)]})
        ok, reason = bump_pr_opt_in.is_opted_in("o/r", "bump-version-conformance", "7")
        assert ok is opted_in
        if not opted_in:
            assert "'gate-check'" in reason

    def test_unreadable_labels_fail_open(self, gh) -> None:
        gh({("gh", "api"): [(1, "")]})
        assert bump_pr_opt_in.is_opted_in("o/r", "bump-version-main", "7")[0] is True

    def test_main_emits_outputs(
        self, gh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gh({("gh", "api"): [(0, "release\n")]})
        out = tmp_path / "out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        rc = bump_pr_opt_in.main(
            ["--repo", "o/r", "check", "--head-ref", "bump-version-main", "--pr", "7"]
        )
        assert rc == 0
        assert "opted_in=false" in out.read_text().splitlines()


# ── rerun ─────────────────────────────────────────────────────────────────────


def _runs(status: str) -> str:
    return json.dumps([{"databaseId": 42, "status": status}])


class TestRerun:
    def test_reruns_a_completed_run(self, gh) -> None:
        fake = gh({("gh", "run", "list"): [(0, _runs("completed"))]})
        assert bump_pr_opt_in.rerun_gate("o/r", "sdk-gate.yaml", "abc") == "42"
        assert ["gh", "run", "rerun", "42", "--repo", "o/r"] in fake.calls

    def test_waits_for_an_in_flight_run(self, gh) -> None:
        fake = gh(
            {
                ("gh", "run", "list"): [
                    (0, _runs("in_progress")),
                    (0, _runs("queued")),
                    (0, _runs("completed")),
                ]
            }
        )
        sleeps: list[float] = []
        bump_pr_opt_in.rerun_gate(
            "o/r", "sdk-gate.yaml", "abc", poll_s=5, sleep=sleeps.append
        )
        assert sleeps == [5, 5]
        assert fake.calls[-1][:3] == ["gh", "run", "rerun"]

    def test_times_out_loudly(self, gh) -> None:
        gh({("gh", "run", "list"): [(0, _runs("in_progress"))]})
        with pytest.raises(SystemExit, match="still in_progress"):
            bump_pr_opt_in.rerun_gate(
                "o/r",
                "sdk-gate.yaml",
                "abc",
                timeout_s=10,
                poll_s=5,
                sleep=lambda _: None,
            )

    def test_nothing_to_rerun_is_loud(self, gh) -> None:
        gh({("gh", "run", "list"): [(0, "[]")]})
        with pytest.raises(SystemExit, match="no sdk-gate.yaml run"):
            bump_pr_opt_in.rerun_gate("o/r", "sdk-gate.yaml", "abc")

    def test_rerun_failure_is_loud(self, gh) -> None:
        gh(
            {
                ("gh", "run", "list"): [(0, _runs("completed"))],
                ("gh", "run", "rerun"): [(1, "")],
            }
        )
        with pytest.raises(SystemExit, match="could not re-run"):
            bump_pr_opt_in.rerun_gate("o/r", "sdk-gate.yaml", "abc")
