"""Tests for .github/scripts/bump_pr_opt_in.py (FND-3748, FND-3839).

Covers the three modes the bump-PR CI opt-in rests on:

* ``clear`` — removes a legacy ``gate-check`` label from the open bump PR
  before a push, and fails open (warns, never raises) so a broken label call
  can't stop a release;
* ``check`` — answers from the head commit's ``gate-check`` status (posted by
  a ``/gate-check`` comment), ``true`` off a bump PR and whenever the status
  can't be read;
* ``e2e-requested`` — answers from the ``e2e-request`` status (or, on
  ``bump-version-main``, the ``gate-check`` opt-in), and fails CLOSED: an
  unreadable request does not justify a live-tenant suite.
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

BUMP = "bump-version-main"
SHA = "a96a27b0000000000000000000000000000000aa"


class TestCommitStatus:
    def test_reads_the_context_from_the_head_commit(self, gh) -> None:
        fake = gh({("gh", "api"): [(0, "success\n")]})
        assert bump_pr_opt_in.commit_status("o/r", SHA, "gate-check") == "success"
        (call,) = fake.calls
        assert f"repos/o/r/commits/{SHA}/status?per_page=100" in call
        assert '.statuses[] | select(.context == "gate-check") | .state' in call

    def test_absent_is_empty_and_unreadable_is_none(self, gh) -> None:
        """The two must differ: `check` fails open only on unreadable."""
        gh({("gh", "api"): [(0, ""), (1, "")]})
        assert bump_pr_opt_in.commit_status("o/r", SHA, "gate-check") == ""
        assert bump_pr_opt_in.commit_status("o/r", SHA, "gate-check") is None


class TestCheck:
    @pytest.mark.parametrize(
        ("head_ref", "sha"), [("", ""), ("feat/x", SHA), (BUMP, "")]
    )
    def test_not_a_bump_pr_always_runs(self, gh, head_ref: str, sha: str) -> None:
        fake = gh({})
        assert bump_pr_opt_in.is_opted_in("o/r", head_ref, sha)[0] is True
        assert fake.calls == []

    @pytest.mark.parametrize(
        ("state", "opted_in"),
        [
            ("success\n", True),
            # `/gate-check` posts `pending` while its dispatched runs go.
            ("pending\n", True),
            ("failure\n", False),
            ("error\n", False),
            ("", False),
        ],
    )
    def test_bump_pr_reads_the_head_commit_status(
        self, gh, state: str, opted_in: bool
    ) -> None:
        gh({("gh", "api"): [(0, state)]})
        ok, reason = bump_pr_opt_in.is_opted_in("o/r", "bump-version-conformance", SHA)
        assert ok is opted_in
        if not opted_in:
            assert "/gate-check" in reason

    def test_unreadable_status_fails_open(self, gh) -> None:
        gh({("gh", "api"): [(1, "")]})
        ok, reason = bump_pr_opt_in.is_opted_in("o/r", BUMP, SHA)
        assert ok is True
        assert "failing open" in reason

    def test_main_emits_outputs(
        self, gh, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        gh({("gh", "api"): [(0, "")]})
        out = tmp_path / "out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        rc = bump_pr_opt_in.main(
            ["--repo", "o/r", "check", "--head-ref", BUMP, "--sha", SHA]
        )
        assert rc == 0
        assert "opted_in=false" in out.read_text().splitlines()


# ── e2e-requested ─────────────────────────────────────────────────────────────


def _statuses(**states: str | None):
    """A `gh api` stub answering per context; None means unreadable."""

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        query = cmd[cmd.index("--jq") + 1]
        context = query.split('"')[1]
        state = states.get(context.replace("-", "_"), "")
        if state is None:
            return subprocess.CompletedProcess(cmd, 1, "", "boom")
        return subprocess.CompletedProcess(cmd, 0, f"{state}\n" if state else "", "")

    return run


class TestE2eRequested:
    @pytest.mark.parametrize(
        ("fork", "author"), [(True, "a-human"), (False, "dependabot[bot]")]
    )
    def test_fork_and_dependabot_never_run_e2e(
        self, monkeypatch: pytest.MonkeyPatch, fork: bool, author: str
    ) -> None:
        """No secrets there — even a recorded request cannot change that."""
        monkeypatch.setattr(bump_pr_opt_in, "run", _statuses(e2e_request="success"))
        ok, _ = bump_pr_opt_in.is_e2e_requested(
            "o/r", "feat/x", SHA, fork=fork, author=author
        )
        assert ok is False

    def test_no_sha_is_false(self, gh) -> None:
        fake = gh({})
        assert (
            bump_pr_opt_in.is_e2e_requested(
                "o/r", "feat/x", "", fork=False, author="a"
            )[0]
            is False
        )
        assert fake.calls == []

    @pytest.mark.parametrize(
        ("state", "requested"),
        [("success", True), ("pending", False), ("failure", False), ("", False)],
    )
    def test_the_e2e_request_status(
        self, monkeypatch: pytest.MonkeyPatch, state: str, requested: bool
    ) -> None:
        monkeypatch.setattr(bump_pr_opt_in, "run", _statuses(e2e_request=state))
        ok, _ = bump_pr_opt_in.is_e2e_requested(
            "o/r", "feat/x", SHA, fork=False, author="a"
        )
        assert ok is requested

    @pytest.mark.parametrize(
        ("head_ref", "gate_check", "requested"),
        [
            (BUMP, "success", True),
            (BUMP, "pending", True),
            (BUMP, "failure", False),
            (BUMP, "", False),
            # The conformance / contract-toolkit lanes never run tenant e2e.
            ("bump-version-conformance", "success", False),
            ("feat/x", "success", False),
        ],
    )
    def test_gate_check_implies_e2e_only_on_the_sdk_bump_branch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        head_ref: str,
        gate_check: str,
        requested: bool,
    ) -> None:
        monkeypatch.setattr(bump_pr_opt_in, "run", _statuses(gate_check=gate_check))
        ok, _ = bump_pr_opt_in.is_e2e_requested(
            "o/r", head_ref, SHA, fork=False, author="a"
        )
        assert ok is requested

    @pytest.mark.parametrize("head_ref", [BUMP, "feat/x"])
    def test_unreadable_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch, head_ref: str
    ) -> None:
        monkeypatch.setattr(
            bump_pr_opt_in, "run", _statuses(e2e_request=None, gate_check=None)
        )
        ok, _ = bump_pr_opt_in.is_e2e_requested(
            "o/r", head_ref, SHA, fork=False, author="a"
        )
        assert ok is False

    @pytest.mark.parametrize(
        ("fork", "expected"), [("false", "true"), ("TRUE", "false")]
    )
    def test_main_emits_outputs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        fork: str,
        expected: str,
    ) -> None:
        monkeypatch.setattr(bump_pr_opt_in, "run", _statuses(e2e_request="success"))
        out = tmp_path / "out"
        monkeypatch.setenv("GITHUB_OUTPUT", str(out))
        rc = bump_pr_opt_in.main(
            [
                "--repo",
                "o/r",
                "e2e-requested",
                "--head-ref",
                "feat/x",
                "--sha",
                SHA,
                "--fork",
                fork,
                "--author",
                "a-human",
            ]
        )
        assert rc == 0
        assert f"requested={expected}" in out.read_text().splitlines()
