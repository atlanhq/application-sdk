"""Tests for .github/scripts/carry_artifact_status.py.

This script writes a commit status the fleet's approval gate reads as a merge
precondition, so the property that matters is not "does it publish" but "can it
ever publish something it did not read". Every test below is aimed at that: the
only input that produces a POST is an explicit `success` on the parent.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import carry_artifact_status as carry

REPO = "atlanhq/application-sdk"
HEAD = "a" * 40
PARENT = "b" * 40


def status_payload(*statuses: dict) -> str:
    return json.dumps({"state": "success", "statuses": list(statuses)})


class Gh:
    """Stub for `gh` and `git`, recording every call.

    `statuses` maps sha -> the JSON body `gh api .../status` returns; a sha that
    is absent makes the call fail, which is the "cannot read" path.
    """

    def __init__(self, statuses: dict[str, str], head: str = HEAD, parent=PARENT):
        self.statuses = statuses
        self.head = head
        self.parent = parent
        self.calls: list[list[str]] = []
        self.posted: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(command)
        if command[0] == "git":
            if command[1:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, self.head, "")
            if command[1:] == ["rev-parse", "HEAD^"]:
                if self.parent is None:
                    return subprocess.CompletedProcess(command, 128, "", "no parent")
                return subprocess.CompletedProcess(command, 0, self.parent, "")
            raise AssertionError(f"unexpected git call: {command}")
        if "-X" in command and "POST" in command:
            self.posted.append(command)
            return subprocess.CompletedProcess(command, 0, "{}", "")
        sha = command[-1].split("/")[-2]
        if sha not in self.statuses:
            return subprocess.CompletedProcess(command, 1, "", "HTTP 404")
        return subprocess.CompletedProcess(command, 0, self.statuses[sha], "")


@pytest.fixture
def stub(monkeypatch):
    """Install the `gh`/`git` stub and make the absence poll instantaneous.

    Patching the module's `sleep` seam rather than `time.sleep`: several of these
    cases exercise the exhausted-poll path, which at the real interval would add
    two minutes each.
    """
    slept: list[float] = []
    monkeypatch.setattr(carry, "sleep", slept.append)

    def install(gh: Gh) -> Gh:
        monkeypatch.setattr(carry, "run", gh)
        gh.slept = slept
        return gh

    return install


ARTIFACT_OK = {"context": "renovate/artifacts", "state": "success"}
ARTIFACT_BAD = {"context": "renovate/artifacts", "state": "failure"}
OTHER = {"context": "sdk-review", "state": "pending"}


class TestCarriesOnlySuccess:
    def test_a_successful_parent_is_carried_onto_head(self, stub):
        gh = stub(
            Gh(
                {
                    HEAD: status_payload(OTHER),
                    PARENT: status_payload(ARTIFACT_OK, OTHER),
                }
            )
        )
        assert carry.main(["--repo", REPO]) == 0
        assert len(gh.posted) == 1
        posted = " ".join(gh.posted[0])
        assert f"repos/{REPO}/statuses/{HEAD}" in posted
        assert "context=renovate/artifacts" in posted
        assert "state=success" in posted
        # Provenance is part of the contract: a reader must be able to tell this
        # status was carried, and from which commit.
        assert f"description=Carried from {PARENT[:7]}" in posted

    @pytest.mark.parametrize("state", ["failure", "pending", "error", "success "])
    def test_any_non_success_parent_publishes_nothing(self, stub, state):
        # `failure` is the live case: it is what Mend reports on this repo when
        # the preset declares a post-upgrade command it cannot run.
        parent = status_payload({"context": "renovate/artifacts", "state": state})
        gh = stub(Gh({HEAD: status_payload(OTHER), PARENT: parent}))
        assert carry.main(["--repo", REPO]) == 0
        assert gh.posted == [], f"published despite a parent state of {state!r}"

    def test_a_parent_without_the_context_publishes_nothing(self, stub):
        gh = stub(Gh({HEAD: status_payload(OTHER), PARENT: status_payload(OTHER)}))
        assert carry.main(["--repo", REPO]) == 0
        assert gh.posted == []

    def test_an_unreadable_parent_publishes_nothing(self, stub):
        # PARENT absent from the stub's map => the API call fails. "Cannot tell"
        # must never become "fine".
        gh = stub(Gh({HEAD: status_payload(OTHER)}))
        assert carry.main(["--repo", REPO]) == 0
        assert gh.posted == []

    def test_a_malformed_parent_payload_publishes_nothing(self, stub):
        gh = stub(Gh({HEAD: status_payload(OTHER), PARENT: "not json"}))
        assert carry.main(["--repo", REPO]) == 0
        assert gh.posted == []


class TestAbsencePolling:
    """Renovate pushes the branch, then sets its statuses. Without a wait, a
    healthy PR loses its approval to a few seconds of ordering."""

    def test_a_parent_stamped_late_is_still_carried(self, stub, monkeypatch):
        gh = stub(Gh({HEAD: status_payload(OTHER), PARENT: status_payload(OTHER)}))
        reads = {"n": 0}
        real = Gh.__call__

        def late(command):
            if command[0] == "gh" and "-X" not in command and PARENT in command[-1]:
                reads["n"] += 1
                if reads["n"] >= 3:
                    gh.statuses[PARENT] = status_payload(ARTIFACT_OK)
            return real(gh, command)

        monkeypatch.setattr(carry, "run", late)
        assert carry.main(["--repo", REPO]) == 0
        assert len(gh.posted) == 1
        assert gh.slept, "the poll must actually wait between reads"

    def test_a_present_failure_is_not_waited_out(self, stub):
        # Only ABSENCE is a race. A published `failure` is a verdict, and polling
        # it would stall the job for two minutes to reach the same answer.
        gh = stub(
            Gh({HEAD: status_payload(OTHER), PARENT: status_payload(ARTIFACT_BAD)})
        )
        assert carry.main(["--repo", REPO]) == 0
        assert gh.posted == []
        assert gh.slept == []

    def test_an_exhausted_poll_publishes_nothing(self, stub):
        gh = stub(Gh({HEAD: status_payload(OTHER), PARENT: status_payload(OTHER)}))
        assert carry.main(["--repo", REPO, "--poll-attempts", "3"]) == 0
        assert gh.posted == []
        assert len(gh.slept) == 2, "n attempts sleep n-1 times, never after the last"


class TestNoOpPaths:
    def test_a_head_that_already_has_the_context_is_left_alone(self, stub):
        """The bound found nothing to change, so HEAD is still Renovate's commit.

        Re-posting would overwrite a status Renovate owns with one we invented —
        and would do so without ever reading a parent.
        """
        gh = stub(Gh({HEAD: status_payload(ARTIFACT_OK)}))
        assert carry.main(["--repo", REPO]) == 0
        assert gh.posted == []
        assert not any("HEAD^" in " ".join(c) for c in gh.calls)

    def test_a_head_that_already_failed_is_not_overwritten_with_success(self, stub):
        # The dangerous shape of the case above: HEAD carries Renovate's own
        # failure and must keep it.
        gh = stub(Gh({HEAD: status_payload(ARTIFACT_BAD)}))
        assert carry.main(["--repo", REPO]) == 0
        assert gh.posted == []

    def test_a_parentless_head_publishes_nothing(self, stub):
        gh = stub(Gh({HEAD: status_payload(OTHER)}, parent=None))
        assert carry.main(["--repo", REPO]) == 0
        assert gh.posted == []


class TestArguments:
    def test_a_missing_repo_fails_rather_than_guessing(self, stub, monkeypatch):
        monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
        stub(Gh({}))
        assert carry.main([]) == 1

    def test_the_repo_defaults_to_the_actions_environment(self, stub, monkeypatch):
        monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
        gh = stub(
            Gh({HEAD: status_payload(OTHER), PARENT: status_payload(ARTIFACT_OK)})
        )
        assert carry.main([]) == 0
        assert len(gh.posted) == 1

    def test_the_target_url_is_recorded_when_given(self, stub):
        gh = stub(
            Gh({HEAD: status_payload(OTHER), PARENT: status_payload(ARTIFACT_OK)})
        )
        assert (
            carry.main(["--repo", REPO, "--target-url", "https://example/run/1"]) == 0
        )
        assert "target_url=https://example/run/1" in " ".join(gh.posted[0])

    def test_a_failed_post_is_reported_as_a_failure(self, stub, monkeypatch):
        gh = stub(
            Gh({HEAD: status_payload(OTHER), PARENT: status_payload(ARTIFACT_OK)})
        )

        def refuse(command):
            if "-X" in command:
                return subprocess.CompletedProcess(command, 1, "", "HTTP 403")
            return Gh.__call__(gh, command)

        monkeypatch.setattr(carry, "run", refuse)
        assert carry.main(["--repo", REPO]) == 1
