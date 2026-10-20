"""Tests for .github/scripts/renovate_release_thaw.py — lifting the release
freeze (FND-4175) by re-evaluating held Renovate PRs when a bump PR closes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import renovate_approval_conditions as gate  # noqa: E402
import renovate_release_thaw as thaw  # noqa: E402

REPO = "atlanhq/application-sdk"
BUMP_SHA = "bump999"


def pull(number, ref, sha="x"):
    return {"number": number, "head": {"ref": ref, "sha": sha}}


class FakeGh:
    """Serves the open-PR listing and bump statuses; records dispatches.

    ``pulls=None`` / a ``None`` status simulates a non-2xx response.
    ``dispatch_fail`` names PRs whose dispatch exits non-zero.
    """

    def __init__(self, pulls, status=None, dispatch_fail=()):
        self.pulls = pulls
        self.status = status if status is not None else {"statuses": []}
        self.dispatch_fail = set(dispatch_fail)
        self.dispatched: list[str] = []
        self.calls: list[list[str]] = []

    def __call__(self, cmd, check=False, capture_output=False, text=False):
        self.calls.append(cmd)
        args = cmd[1:]
        if args[:2] == ["workflow", "run"]:
            pr = args[-1].split("=", 1)[1]
            if pr in self.dispatch_fail:
                return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 403")
            self.dispatched.append(pr)
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        payload = self.pulls if "/pulls?state=open" in args[1] else self.status
        if payload is None:
            return subprocess.CompletedProcess(cmd, 1, stdout="{}", stderr="")
        if "/pulls?state=open" in args[1]:
            payload = [payload]  # --paginate --slurp wraps pages
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps(payload), stderr=""
        )

    @property
    def workflows(self) -> set[str]:
        return {c[3] for c in self.calls if c[1:3] == ["workflow", "run"]}


def run(monkeypatch, fake, **env):
    monkeypatch.setenv("REPO", REPO)
    for key in ("RELEASE_FREEZE", "APPROVE_WORKFLOW"):
        monkeypatch.setenv(key, env.get(key, ""))
    return thaw.main(fake)


class TestThaw:
    def test_dispatches_one_evaluation_per_open_renovate_pr(self, monkeypatch):
        fake = FakeGh(
            [
                pull(12, "renovate/lock-file-maintenance"),
                pull(9, "renovate/github-actions"),
                pull(10, "feature/x"),
                pull(11, "bump-version-main", BUMP_SHA),
            ]
        )
        assert run(monkeypatch, fake) == 0
        assert fake.dispatched == ["9", "12"]
        assert fake.workflows == {thaw.DEFAULT_APPROVE_WORKFLOW}

    def test_dispatch_carries_pr_number_only(self, monkeypatch):
        # No head_sha: the gate evaluates the PR's current HEAD.
        fake = FakeGh([pull(9, "renovate/github-actions")])
        run(monkeypatch, fake)
        (call,) = [c for c in fake.calls if c[1:3] == ["workflow", "run"]]
        assert call[-2:] == ["-f", "pr_number=9"]
        assert not any("head_sha" in part for part in call)

    def test_does_not_thaw_while_another_train_is_in_flight(self, monkeypatch, capsys):
        fake = FakeGh(
            [pull(9, "renovate/x"), pull(30, "bump-version-conformance", BUMP_SHA)],
            status={
                "statuses": [{"context": gate.GATE_CHECK_CONTEXT, "state": "pending"}]
            },
        )
        assert run(monkeypatch, fake) == 0
        assert fake.dispatched == []
        assert "#30" in capsys.readouterr().out

    def test_an_open_bump_pr_without_gate_check_does_not_block_the_thaw(
        self, monkeypatch
    ):
        # The next bump PR can open seconds after the release merges.
        fake = FakeGh([pull(9, "renovate/x"), pull(31, "bump-version-main", BUMP_SHA)])
        run(monkeypatch, fake)
        assert fake.dispatched == ["9"]

    def test_override_on_thaws_nothing(self, monkeypatch):
        fake = FakeGh([pull(9, "renovate/x")])
        assert run(monkeypatch, fake, RELEASE_FREEZE="on") == 0
        assert fake.dispatched == []

    def test_override_off_thaws_without_reading_trains(self, monkeypatch):
        fake = FakeGh(
            [pull(9, "renovate/x"), pull(30, "bump-version-main", BUMP_SHA)],
            status=None,  # would fail the step if it were read
        )
        assert run(monkeypatch, fake, RELEASE_FREEZE="off") == 0
        assert fake.dispatched == ["9"]

    def test_a_failed_dispatch_warns_and_continues(self, monkeypatch, capsys):
        fake = FakeGh(
            [pull(9, "renovate/a"), pull(10, "renovate/b")], dispatch_fail={"9"}
        )
        assert run(monkeypatch, fake) == 0
        assert fake.dispatched == ["10"]
        assert "::warning::PR #9" in capsys.readouterr().out

    def test_unreadable_listing_fails_the_step(self, monkeypatch, capsys):
        fake = FakeGh(None)
        assert run(monkeypatch, fake) == 1
        assert fake.dispatched == []
        assert "::error::" in capsys.readouterr().out

    def test_workflow_name_can_be_overridden(self, monkeypatch):
        fake = FakeGh([pull(9, "renovate/x")])
        run(monkeypatch, fake, APPROVE_WORKFLOW="other.yml")
        assert fake.workflows == {"other.yml"}


class TestWiring:
    """The workflow and the caller agree on the names the thaw depends on."""

    ROOT = Path(__file__).resolve().parents[3]

    def test_default_workflow_is_the_sdk_caller(self):
        assert (
            self.ROOT / ".github/workflows" / thaw.DEFAULT_APPROVE_WORKFLOW
        ).is_file()

    @pytest.mark.parametrize(
        "path",
        [
            ".github/workflows/renovate-release-thaw.yml",
            ".github/workflows/renovate-auto-approve-reusable.yml",
        ],
    )
    def test_override_variable_is_wired(self, path):
        assert (
            "RELEASE_FREEZE: ${{ vars.RENOVATE_RELEASE_FREEZE }}"
            in (self.ROOT / path).read_text()
        )

    def test_caller_accepts_a_pr_number_only_dispatch(self):
        text = (
            self.ROOT / ".github/workflows" / thaw.DEFAULT_APPROVE_WORKFLOW
        ).read_text()
        assert "workflow_dispatch:" in text
        assert "pr_number:" in text
