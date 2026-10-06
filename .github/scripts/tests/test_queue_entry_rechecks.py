"""Merge-queue entries re-run only what the base can change (FND-3321).

Three reusables carry a `queue-diff` job that runs queue_tree_diff.py on a
`merge_group` event, and skip their expensive job on its answer. Each skip
greens a required context on the strength of the PR's own verdict, so every
gate is evaluated here, through the real expressions, for both directions:

* it skips ONLY on the one answer that means "nothing the check reads
  changed", and runs on every other answer, including the empty output a
  skipped or failed `queue-diff` leaves behind;
* `queue-diff` itself runs only on a queue entry, so no other event can ever
  produce a skip;
* the skipping jobs keep a static `name:` so the skipped check run carries the
  context string the rulesets require.

The three `queue-diff` jobs are also pinned to the same steps, so a fix to one
cannot silently miss the other two.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _gha_expr import evaluate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
SCRIPT = REPO_ROOT / ".github" / "scripts" / "queue_tree_diff.py"

#: reusable -> (output its gates read, the value that means skip, the gated jobs
#: with the context name each must keep).
GATED = {
    "conformance-reusable.yaml": ("identical", "true", {"suite": "Conformance Gate"}),
    "checks-reusable.yaml": ("identical", "true", {"pre-commit": "Pre-commit"}),
    "build-and-scan.yaml": (
        "image_changed",
        "false",
        {"build": "Build Image", "security-gate": "Security Gate"},
    ),
}


def _load(name: str) -> dict[str, Any]:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(GATED))
@pytest.mark.parametrize("answer", ["", "true", "false"])
def test_gated_jobs_skip_only_on_the_skip_answer(name: str, answer: str) -> None:
    output, skip_value, jobs = GATED[name]
    workflow = _load(name)
    for job_id in jobs:
        job = workflow["jobs"][job_id]
        assert "queue-diff" in job["needs"], f"{name}:{job_id} must wait on queue-diff"
        contexts = {"needs": {"queue-diff": {"outputs": {output: answer}}}}
        runs = evaluate(job["if"], contexts)
        assert runs is (
            answer != skip_value
        ), f"{name}:{job_id} with {output}={answer!r}: runs={runs}"


@pytest.mark.parametrize("name", sorted(GATED))
def test_gated_jobs_keep_a_static_name(name: str) -> None:
    _, _, jobs = GATED[name]
    workflow = _load(name)
    for job_id, context in jobs.items():
        assert workflow["jobs"][job_id]["name"] == context


def _queue_diff_contexts(event: str) -> dict[str, Any]:
    return {
        "github": {"event_name": event},
        "inputs": {"event_name": event, "force-all": False, "image": "", "ref": ""},
    }


@pytest.mark.parametrize("name", sorted(GATED))
@pytest.mark.parametrize(
    "event", ["pull_request", "push", "merge_group", "schedule", "workflow_dispatch"]
)
def test_queue_diff_runs_only_on_a_queue_entry(name: str, event: str) -> None:
    gate = _load(name)["jobs"]["queue-diff"]["if"]
    assert evaluate(gate, _queue_diff_contexts(event)) is (event == "merge_group")


@pytest.mark.parametrize(
    ("inputs", "runs"),
    [
        ({"image": "", "ref": ""}, True),
        # build-and-publish-app.yaml's prebuilt-image path.
        ({"image": "ghcr.io/atlanhq/x:abc", "ref": ""}, False),
        # A caller-chosen ref is not the queue commit the diff compares.
        ({"image": "", "ref": "some-branch"}, False),
    ],
)
def test_scan_never_skips_for_a_prebuilt_image_or_explicit_ref(
    inputs: dict[str, str], runs: bool
) -> None:
    gate = _load("build-and-scan.yaml")["jobs"]["queue-diff"]["if"]
    contexts = {"github": {"event_name": "merge_group"}, "inputs": inputs}
    assert evaluate(gate, contexts) is runs


def test_the_three_queue_diff_jobs_are_the_same_job() -> None:
    def shape(job: dict[str, Any]) -> list[dict[str, Any]]:
        return [{k: v for k, v in step.items() if k != "name"} for step in job["steps"]]

    jobs = {name: _load(name)["jobs"]["queue-diff"] for name in GATED}
    reference = shape(jobs["checks-reusable.yaml"])
    for name, job in jobs.items():
        assert shape(job) == reference, f"{name}'s queue-diff steps drifted"
        assert job["runs-on"] == "ubuntu-slim"
        assert job["permissions"] == {"contents": "read"}


def test_queue_diff_runs_the_script_it_fetches() -> None:
    assert SCRIPT.is_file()
    job = _load("checks-reusable.yaml")["jobs"]["queue-diff"]
    fetch = job["steps"][1]
    assert fetch["with"]["sparse-checkout"] == ".github/scripts/queue_tree_diff.py"
    assert fetch["with"]["ref"] == "${{ job.workflow_sha }}"
    assert job["steps"][2]["run"] == "python3 _sdk/.github/scripts/queue_tree_diff.py"
