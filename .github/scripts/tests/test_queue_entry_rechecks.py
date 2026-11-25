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

The three `queue-diff` jobs call one reusable, queue-diff-reusable.yaml
(FND-4928), so a fix lands once; the tests below pin each caller to it and pin
the reusable's outputs to the names the gates read.
"""

from __future__ import annotations

from typing import Any

import pytest
from _bump_skip import without_bump_skip
from _gha_expr import evaluate
from _workflows import REPO_ROOT, job, load_workflow

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


@pytest.mark.parametrize("name", sorted(GATED))
@pytest.mark.parametrize("answer", ["", "true", "false"])
def test_gated_jobs_skip_only_on_the_skip_answer(name: str, answer: str) -> None:
    output, skip_value, jobs = GATED[name]
    workflow = load_workflow(name)
    for job_id in jobs:
        job = workflow["jobs"][job_id]
        assert "queue-diff" in job["needs"], f"{name}:{job_id} must wait on queue-diff"
        # build-and-scan's gates also read `scope` (FND-3328); its empty
        # output (skipped on a queue entry's sibling events) means "scan".
        contexts = {
            "needs": {
                "queue-diff": {"outputs": {output: answer}},
                "scope": {"outputs": {"scan": ""}},
            }
        }
        runs = evaluate(without_bump_skip(job["if"]), contexts)
        assert runs is (answer != skip_value), (
            f"{name}:{job_id} with {output}={answer!r}: runs={runs}"
        )


@pytest.mark.parametrize("name", sorted(GATED))
def test_gated_jobs_keep_a_static_name(name: str) -> None:
    _, _, jobs = GATED[name]
    workflow = load_workflow(name)
    for job_id, context in jobs.items():
        assert workflow["jobs"][job_id]["name"] == context


def _queue_diff_contexts(event: str) -> dict[str, Any]:
    return {
        "github": {"event_name": event},
        "inputs": {"event_name": event, "force-all": False, "image": "", "ref": ""},
        "needs": {"scope": {"outputs": {"scan": ""}}},
    }


@pytest.mark.parametrize("name", sorted(GATED))
@pytest.mark.parametrize(
    "event", ["pull_request", "push", "merge_group", "schedule", "workflow_dispatch"]
)
def test_queue_diff_runs_only_on_a_queue_entry(name: str, event: str) -> None:
    gate = job(load_workflow(name), "queue-diff")["if"]
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
    gate = job(load_workflow("build-and-scan.yaml"), "queue-diff")["if"]
    contexts = {
        "github": {"event_name": "merge_group"},
        "inputs": inputs,
        "needs": {"scope": {"outputs": {"scan": ""}}},
    }
    assert evaluate(gate, contexts) is runs


QUEUE_DIFF = "queue-diff-reusable.yaml"


@pytest.mark.parametrize("name", sorted(GATED))
def test_every_queue_diff_job_is_the_one_reusable(name: str) -> None:
    queue_diff = load_workflow(name)["jobs"]["queue-diff"]
    assert queue_diff["uses"] == f"./.github/workflows/{QUEUE_DIFF}"
    # A called workflow can only narrow its caller's grant.
    assert queue_diff["permissions"] == {"contents": "read"}
    assert "steps" not in queue_diff


@pytest.mark.parametrize("name", sorted(GATED))
def test_the_reusable_exports_the_answer_each_gate_reads(name: str) -> None:
    output, _, _ = GATED[name]
    # YAML 1.1 reads a bare `on` as True, so the keys are not all strings.
    reusable: dict[Any, Any] = load_workflow(QUEUE_DIFF)
    exported = reusable[True]["workflow_call"]["outputs"][output]["value"]
    assert exported == f"${{{{ jobs.diff.outputs.{output} }}}}"
    diff = reusable["jobs"]["diff"]
    assert diff["outputs"][output] == f"${{{{ steps.diff.outputs.{output} }}}}"


def test_queue_diff_runs_the_script_it_fetches() -> None:
    assert SCRIPT.is_file()
    job = load_workflow(QUEUE_DIFF)["jobs"]["diff"]
    assert job["runs-on"] == "ubuntu-slim"
    assert job["permissions"] == {"contents": "read"}
    fetch = job["steps"][1]
    assert fetch["with"]["sparse-checkout"] == ".github/scripts/queue_tree_diff.py"
    assert fetch["with"]["ref"] == "${{ job.workflow_sha }}"
    assert job["steps"][2]["run"] == "python3 _sdk/.github/scripts/queue_tree_diff.py"
