"""Tests for .github/scripts/require_renovate_artifacts.py.

The job runs in Tests Gate for every app repo at once, so a regression reds the
whole fleet. The pass-through for non-Renovate refs must not touch the API.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

import require_renovate_artifacts as gate

REPO = "atlanhq/atlan-example-app"
SHA = "a" * 40
BRANCH = "renovate/atlan-platform"
_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"


def payload(state: str | None) -> str:
    statuses = [{"context": "renovate/artifacts", "state": state}] if state else []
    statuses.append({"context": "renovate/stability-days", "state": "success"})
    return json.dumps({"state": "success", "statuses": statuses})


class Gh:
    """Returns each queued response in turn; the last one repeats."""

    def __init__(self, *responses: subprocess.CompletedProcess[str]):
        self.responses = list(responses)
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if len(self.responses) > 1:
            return self.responses.pop(0)
        return self.responses[0]


def ok(state: str | None) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, payload(state), "")


def failed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 1, "", "HTTP 404")


@pytest.fixture
def stub(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(gate, "sleep", slept.append)

    def install(gh: Gh) -> Gh:
        monkeypatch.setattr(gate, "run", gh)
        gh.slept = slept
        return gh

    return install


@pytest.mark.parametrize(
    "head_ref",
    [
        "",
        "main",
        "sachipatankar/fnd-2981",
        "gh-readonly-queue/main/pr-1-abc",
        "bot/conformance-resync",
        "renovate",
    ],
)
def test_non_renovate_ref_passes_without_api_call(stub, head_ref):
    gh = stub(Gh(failed()))
    assert gate.main(["--head-ref", head_ref, "--repo", REPO, "--sha", SHA]) == 0
    assert gh.calls == []
    assert gh.slept == []


def test_success_passes(stub):
    gh = stub(Gh(ok("success")))
    assert gate.main(["--head-ref", BRANCH, "--repo", REPO, "--sha", SHA]) == 0
    assert gh.calls == [["gh", "api", f"repos/{REPO}/commits/{SHA}/status"]]


@pytest.mark.parametrize("state", ["failure", "error"])
def test_verdict_fails_without_waiting(stub, state):
    gh = stub(Gh(ok(state)))
    assert gate.main(["--head-ref", BRANCH, "--repo", REPO, "--sha", SHA]) == 1
    assert len(gh.calls) == 1
    assert gh.slept == []


@pytest.mark.parametrize("late", [ok(None), ok("pending"), failed()])
def test_waits_for_a_late_status(stub, late):
    gh = stub(Gh(late, late, ok("success")))
    assert (
        gate.main(
            ["--head-ref", BRANCH, "--repo", REPO, "--sha", SHA, "--poll-attempts", "5"]
        )
        == 0
    )
    assert len(gh.calls) == 3
    assert len(gh.slept) == 2


@pytest.mark.parametrize("stuck", [ok(None), ok("pending"), failed()])
def test_still_not_success_after_the_poll_fails(stub, stuck):
    gh = stub(Gh(stuck))
    assert (
        gate.main(
            ["--head-ref", BRANCH, "--repo", REPO, "--sha", SHA, "--poll-attempts", "4"]
        )
        == 1
    )
    assert len(gh.calls) == 4
    assert len(gh.slept) == 3


def test_late_failure_fails(stub):
    gh = stub(Gh(ok(None), ok("failure")))
    assert gate.main(["--head-ref", BRANCH, "--repo", REPO, "--sha", SHA]) == 1
    assert len(gh.calls) == 2


def test_lock_file_maintenance_branch_is_checked(stub):
    gh = stub(Gh(ok("success")))
    assert (
        gate.main(
            [
                "--head-ref",
                "renovate/lock-file-maintenance",
                "--repo",
                REPO,
                "--sha",
                SHA,
            ]
        )
        == 0
    )
    assert len(gh.calls) == 1


def test_renovate_ref_without_sha_fails(stub):
    gh = stub(Gh(ok("success")))
    assert gate.main(["--head-ref", BRANCH, "--repo", REPO, "--sha", ""]) == 1
    assert gh.calls == []


def _timeout_minutes(workflow: str, job: str) -> int:
    jobs = yaml.safe_load((_WORKFLOWS / workflow).read_text())["jobs"]
    return int(jobs[job]["timeout-minutes"])


def test_default_wait_outlasts_the_lock_cooldown_carry_forward():
    cooldown = _timeout_minutes("renovate-lock-cooldown.yaml", "bound") * 60
    assert gate.POLL_ATTEMPTS * gate.POLL_INTERVAL_SECONDS >= cooldown


def test_job_timeout_outlasts_the_wait():
    wait = gate.POLL_ATTEMPTS * gate.POLL_INTERVAL_SECONDS
    assert _timeout_minutes("tests-reusable.yaml", "renovate-artifacts") * 60 > wait
