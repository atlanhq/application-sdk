"""A failed release run is reported on the merged PR (FND-4716)."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Sequence

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import notify_release_failure as notify  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
RELEASE_WORKFLOWS = ("conformance-release.yml", "contract-toolkit-release.yml")
ENV = {
    "PR_NUMBER": "4770",
    "RUN_URL": "https://github.com/o/r/actions/runs/1",
    "GITHUB_REPOSITORY": "o/r",
}
ARGV = ["--package", "conformance", "--workflow", "conformance-release.yml"]


class FakeGh:
    def __init__(self, code: int = 0) -> None:
        self.code = code
        self.calls: list[tuple[list[str], str]] = []

    def __call__(self, argv: Sequence[str], stdin: str) -> int:
        self.calls.append((list(argv), stdin))
        return self.code


def test_comments_on_the_merged_pr_with_the_run_and_the_redispatch() -> None:
    gh = FakeGh()
    assert notify.main(ARGV, ENV, gh) == 0
    [(argv, body)] = gh.calls
    assert argv[:4] == ["gh", "pr", "comment", "4770"]
    assert argv[argv.index("--repo") + 1] == "o/r"
    assert ENV["RUN_URL"] in body
    assert "gh workflow run conformance-release.yml" in body


def test_a_manual_run_has_no_pr_and_posts_nothing() -> None:
    gh = FakeGh()
    assert notify.main(ARGV, {**ENV, "PR_NUMBER": ""}, gh) == 0
    assert gh.calls == []


def test_a_failed_comment_fails_the_step() -> None:
    assert notify.main(ARGV, ENV, FakeGh(code=1)) == 1


@pytest.mark.parametrize("workflow", RELEASE_WORKFLOWS)
def test_release_workflow_reports_its_own_failure(workflow: str) -> None:
    """A separate job, so a failed token mint or checkout is reported too, and
    only on a merge: a skipped or successful release posts nothing."""
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    job = data["jobs"]["notify-failure"]
    assert job["needs"] == "open-release-pr"
    assert job["if"] == "failure() && github.event_name == 'pull_request'"
    assert job["permissions"] == {"contents": "read", "pull-requests": "write"}
    [run] = [s["run"] for s in job["steps"] if "run" in s]
    argv = shlex.split(run)
    assert argv[1] == ".github/scripts/notify_release_failure.py"
    args = argv[2:]
    assert args[args.index("--workflow") + 1] == workflow
    notify.main(args, {**ENV, "PR_NUMBER": ""}, FakeGh())  # the argv parses
