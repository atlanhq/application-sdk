"""Build & Scan scans only the bump-version PR in a release-flow repo (FND-3328)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vuln_scan_scope as scope  # noqa: E402

RELEASE_YAML = """\
jobs:
  bump:
    uses: atlanhq/application-sdk/.github/workflows/release-version-bump.yaml@main
"""


def _workflows(tmp_path: Path, *, release_flow: bool) -> Path:
    wf = tmp_path / ".github" / "workflows"
    wf.mkdir(parents=True)
    (wf / "checks.yml").write_text("jobs: {}\n")
    if release_flow:
        (wf / "release.yaml").write_text(RELEASE_YAML)
    return wf


@pytest.mark.parametrize(
    ("event", "head_ref", "scan"),
    [
        ("pull_request", "bump-version-main", True),
        ("pull_request", "renovate/foo-1.x", False),
        ("pull_request", "feat/thing", False),
        ("merge_group", "", False),
        ("push", "", True),
        ("workflow_dispatch", "", True),
    ],
)
def test_release_flow_repo_scans_only_the_bump_pr(
    event: str, head_ref: str, scan: bool
) -> None:
    assert scope.decide(event, head_ref, False, True).scan is scan


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_repo_without_a_release_flow_scans_everything(event: str) -> None:
    # No bump PR will ever come: skipping would leave the image ungated.
    assert scope.decide(event, "feat/thing", False, False).scan is True


@pytest.mark.parametrize("event", ["pull_request", "merge_group"])
def test_scan_every_pr_opts_back_in(event: str) -> None:
    assert scope.decide(event, "feat/thing", True, True).scan is True


def test_release_flow_detected_from_the_bump_workflow_call(tmp_path: Path) -> None:
    assert scope.has_release_flow(_workflows(tmp_path, release_flow=True)) is True


def test_no_release_flow_without_the_bump_workflow_call(tmp_path: Path) -> None:
    assert scope.has_release_flow(_workflows(tmp_path, release_flow=False)) is False


def test_missing_workflows_dir_fails_safe_to_scanning(tmp_path: Path) -> None:
    # A failed base checkout must never turn into a skipped gate.
    assert scope.has_release_flow(tmp_path / "absent") is False


@pytest.mark.parametrize(
    "text",
    [
        "# See release-version-bump.yaml for why this queues.\n",
        "#   uses: atlanhq/application-sdk/.github/workflows/release-version-bump.yaml@main\n",
        "jobs:\n  x:\n    steps:\n      - run: echo release-version-bump.yaml\n",
        "env:\n  NOTE: 'calls release-version-bump.yaml'\n",
    ],
)
def test_a_mention_that_is_not_a_job_call_does_not_count(
    tmp_path: Path, text: str
) -> None:
    """A false "release flow" turns ordinary PR scans OFF, so only a job-level
    `uses:` of the bump workflow may count."""
    wf = _workflows(tmp_path, release_flow=False)
    (wf / "notes.yaml").write_text(text)
    assert scope.has_release_flow(wf) is False


@pytest.mark.parametrize(
    "uses",
    [
        "atlanhq/application-sdk/.github/workflows/release-version-bump.yaml@main",
        '"atlanhq/application-sdk/.github/workflows/release-version-bump.yaml@v3"',
        "./.github/workflows/release-version-bump.yaml",
        "atlanhq/application-sdk/.github/workflows/release-version-bump.yaml@main # pin",
    ],
)
def test_every_job_call_form_counts(tmp_path: Path, uses: str) -> None:
    wf = tmp_path / "wf"
    wf.mkdir()
    (wf / "release.yaml").write_text(f"jobs:\n  bump:\n    uses: {uses}\n")
    assert scope.has_release_flow(wf) is True


def test_this_repos_own_workflows_are_not_a_release_flow() -> None:
    """application-sdk's workflows name the file in comments only."""
    root = Path(__file__).resolve().parents[2] / "workflows"
    assert scope.has_release_flow(root) is False


def test_non_yaml_mention_does_not_count(tmp_path: Path) -> None:
    wf = _workflows(tmp_path, release_flow=False)
    (wf / "README.md").write_text("see release-version-bump.yaml\n")
    assert scope.has_release_flow(wf) is False


def test_main_writes_exactly_true_or_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The workflow gates compare against the literal 'false'.
    output = tmp_path / "out"
    monkeypatch.setenv("EVENT_NAME", "pull_request")
    monkeypatch.setenv("HEAD_REF", "renovate/foo")
    monkeypatch.setenv("SCAN_EVERY_PR", "false")
    monkeypatch.setenv("WORKFLOWS_DIR", str(_workflows(tmp_path, release_flow=True)))
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert scope.main() == 0
    assert output.read_text() == "scan=false\n"
