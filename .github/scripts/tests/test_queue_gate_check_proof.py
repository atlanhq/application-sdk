"""A proven release queue entry skips tests-reusable's tiers (FND-4344).

Two halves:

* queue_gate_check_proof.py, with the status read stubbed: it answers
  ``proven=true`` only for an identical tree whose PR head carries a
  ``gate-check`` success from github-actions[bot], and fails safe otherwise;
* tests-reusable.yaml, through the real ``if:`` expressions: a whole
  ``merge_group`` run is simulated job by job, with GitHub's implicit
  ``success()`` on ``needs``, so the test sees exactly which jobs a proven entry
  still runs (only ``queue-proof``), and that every other answer runs the tiers
  as before. Simulating the run rather than checking a list of gated jobs
  catches a job added later that would run unproven work in a proven entry.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import queue_gate_check_proof as proof  # noqa: E402
from _gha_expr import evaluate  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"

HEAD = "a" * 40
REPO = "atlanhq/example-app"
BOT = "github-actions[bot]"


def _statuses(lines: str):
    def read(repo: str, sha: str) -> str:
        assert (repo, sha) == (REPO, HEAD)
        return lines

    return read


def _never_called(repo: str, sha: str) -> str:
    raise AssertionError("statuses must not be read here")


# ── queue_gate_check_proof.py ────────────────────────────────────────────────


def test_bot_success_on_an_identical_tree_is_proven() -> None:
    result = proof.decide("true", HEAD, REPO, _statuses(f"success {BOT}\n"))
    assert result.proven is True


@pytest.mark.parametrize(
    ("lines", "why"),
    [
        ("", "no gate-check status at all"),
        (f"pending {BOT}\n", "the verdict is not in yet"),
        (f"failure {BOT}\n", "the gate failed"),
        (f"error {BOT}\n", "the gate errored"),
        ("success someone-with-write-access\n", "not posted by the workflow token"),
        # Newest first: a later failure from the bot overrides its older pass.
        (f"failure {BOT}\nsuccess {BOT}\n", "the newest bot verdict is a failure"),
        # A newer success from anyone else does not stand in for the bot's.
        (f"success mallory\nfailure {BOT}\n", "only the bot's newest row counts"),
        (f"success {BOT} extra\n", "malformed line"),
    ],
)
def test_anything_but_a_bot_success_is_not_proven(lines: str, why: str) -> None:
    assert proof.decide("true", HEAD, REPO, _statuses(lines)).proven is False, why


def test_newest_bot_success_after_a_failure_is_proven() -> None:
    """A re-run leg that cleared the gate (FND-4331) leaves success newest."""
    lines = f"success {BOT}\nfailure {BOT}\npending {BOT}\n"
    assert proof.decide("true", HEAD, REPO, _statuses(lines)).proven is True


@pytest.mark.parametrize("identical", ["", "false", "TRUE ", "yes"])
def test_a_tree_that_is_not_identical_is_never_proven(identical: str) -> None:
    assert proof.decide(identical, HEAD, REPO, _never_called).proven is False


@pytest.mark.parametrize("pr_head", ["", "HEAD", "a" * 39, f"{HEAD}; rm -rf /"])
def test_a_bad_pr_head_fails_safe(pr_head: str) -> None:
    assert proof.decide("true", pr_head, REPO, _never_called).proven is False


@pytest.mark.parametrize("repo", ["", "atlanhq", "atlanhq/x/y", "atlanhq/x?y=1"])
def test_a_bad_repository_fails_safe(repo: str) -> None:
    assert proof.decide("true", HEAD, repo, _never_called).proven is False


@pytest.mark.parametrize(
    "error",
    [
        subprocess.CalledProcessError(1, ["gh"]),
        subprocess.TimeoutExpired(["gh"], 60),
        FileNotFoundError("gh"),
    ],
)
def test_an_api_failure_fails_safe(error: Exception) -> None:
    def broken(repo: str, sha: str) -> str:
        raise error

    assert proof.decide("true", HEAD, REPO, broken).proven is False


def test_main_writes_the_output(tmp_path: Path) -> None:
    output = tmp_path / "out"
    env = {
        "IDENTICAL": "true",
        "PR_HEAD": HEAD,
        "REPO": REPO,
        "GITHUB_OUTPUT": str(output),
    }
    assert proof.main(env, statuses=_statuses(f"success {BOT}\n")) == 0
    assert output.read_text() == "proven=true\n"


def test_main_fails_safe_on_an_empty_environment(tmp_path: Path) -> None:
    output = tmp_path / "out"
    proof.main({"GITHUB_OUTPUT": str(output)}, statuses=_never_called)
    assert output.read_text() == "proven=false\n"


# ── tests-reusable.yaml, simulated ───────────────────────────────────────────

_STATUS_FN_RE = re.compile(r"\b(always|success|failure|cancelled)\s*\(")


def _workflow() -> dict[str, Any]:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _needs(job: dict[str, Any]) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs)


def _inputs(workflow: dict[str, Any]) -> dict[str, Any]:
    """The reusable's input defaults, with the scaffolded caller's overrides
    (packages/conformance/.../templates/tests.yaml)."""
    # PyYAML reads the bare `on:` key as True.
    declared = (workflow.get("on") or workflow[True])["workflow_call"]["inputs"]
    values = {name: spec.get("default", "") for name, spec in declared.items()}
    values.update({"skip-on-bump-pr": True, "e2e-label-trigger": False})
    return values


def _simulate(
    event: str, proof_result: str, proven: str, outputs: dict[str, dict[str, str]]
) -> dict[str, str]:
    """Each job's result for one run. A job runs when its ``if:`` holds and,
    unless the ``if:`` calls a status function, every need succeeded (GitHub's
    implicit ``success()``). A job that runs succeeds, except ``queue-proof``,
    whose result the scenario picks."""
    workflow = _workflow()
    jobs: dict[str, Any] = workflow["jobs"]
    github = {"event_name": event, "head_ref": "", "ref": "refs/heads/x"}
    if event == "merge_group":
        github["ref"] = "refs/heads/gh-readonly-queue/main/pr-7-" + "b" * 40
    results: dict[str, str] = {}
    outs: dict[str, dict[str, str]] = {}
    pending = list(jobs)
    while pending:
        progressed = False
        for job_id in list(pending):
            job = jobs[job_id]
            needs = _needs(job)
            if any(n not in results for n in needs):
                continue
            condition = str(job.get("if", "true"))
            implicit = not _STATUS_FN_RE.search(condition)
            if implicit and any(results[n] != "success" for n in needs):
                runs = False
            else:
                contexts = {
                    "github": github,
                    "inputs": _inputs(workflow),
                    "needs": {
                        n: {"result": results[n], "outputs": outs.get(n, {})}
                        for n in needs
                    },
                }
                runs = evaluate(condition, contexts)
            if not runs:
                results[job_id] = "skipped"
            elif job_id == "queue-proof":
                results[job_id] = proof_result
                outs[job_id] = {"proven": proven}
            else:
                results[job_id] = "success"
                outs[job_id] = outputs.get(job_id, {})
            pending.remove(job_id)
            progressed = True
        assert progressed, f"needs cycle or unknown job among {pending}"
    return results


def _ran(results: dict[str, str]) -> set[str]:
    return {job for job, result in results.items() if result != "skipped"}


#: What a real queue entry's jobs answer: an integration suite exists, no
#: hosted api member.
_OUTPUTS = {
    "detect-integration": {"count": "3"},
    "detect-api-member": {"present": "false"},
}


def test_a_proven_queue_entry_runs_only_the_proof() -> None:
    results = _simulate("merge_group", "success", "true", _OUTPUTS)
    assert _ran(results) == {"queue-proof"}


@pytest.mark.parametrize(
    ("proof_result", "proven"),
    [
        ("success", "false"),
        # The proof step skipped (tree not identical): no output at all.
        ("success", ""),
        # queue-proof itself failed: its output is empty and its result red.
        ("failure", ""),
    ],
)
def test_an_unproven_queue_entry_runs_the_tiers_as_before(
    proof_result: str, proven: str
) -> None:
    ran = _ran(_simulate("merge_group", proof_result, proven, _OUTPUTS))
    assert {
        "unit",
        "detect-integration",
        "integration",
        "detect-api-member",
        "scorecard",
        "tests-passed",
    } <= ran


@pytest.mark.parametrize("event", ["pull_request", "push", "workflow_dispatch"])
def test_queue_proof_runs_only_on_a_queue_entry(event: str) -> None:
    results = _simulate(event, "success", "true", _OUTPUTS)
    assert results["queue-proof"] == "skipped"
    # Its skip must not take the unit tier or the gate down with it.
    assert results["unit"] == "success"
    assert results["tests-passed"] == "success"


def test_the_gate_keeps_its_static_required_name() -> None:
    jobs = _workflow()["jobs"]
    assert jobs["tests-passed"]["name"] == "Tests Gate"
    assert jobs["unit"]["name"] == "Unit"
    assert jobs["integration"]["name"] == "Integration"


def test_queue_proof_reads_statuses_only_for_an_identical_tree() -> None:
    job = _workflow()["jobs"]["queue-proof"]
    assert job["runs-on"] == "ubuntu-slim"
    assert job["permissions"] == {"contents": "read", "statuses": "read"}
    steps = {step.get("id"): step for step in job["steps"] if step.get("id")}
    assert steps["diff"]["run"] == "python3 _sdk/.github/scripts/queue_tree_diff.py"
    assert steps["proof"]["if"] == "steps.diff.outputs.identical == 'true'"
    # The verdict is read on the very commit whose tree was compared.
    assert steps["proof"]["env"]["PR_HEAD"] == "${{ steps.diff.outputs.pr_head }}"
    assert job["outputs"] == {"proven": "${{ steps.proof.outputs.proven }}"}


def test_queue_proof_diffs_exactly_as_the_other_queue_diff_jobs() -> None:
    """Same checkout and diff step as checks-reusable's queue-diff, so a fix to
    one (FND-4344's head lookup) cannot miss this copy."""
    checks = yaml.safe_load(
        (WORKFLOW.parent / "checks-reusable.yaml").read_text(encoding="utf-8")
    )["jobs"]["queue-diff"]["steps"]
    mine = _workflow()["jobs"]["queue-proof"]["steps"]
    assert mine[0]["uses"] == checks[0]["uses"]
    assert mine[0]["with"] == checks[0]["with"]
    assert mine[2]["env"] == checks[2]["env"]
    assert mine[2]["run"] == checks[2]["run"]
    sparse = mine[1]["with"]["sparse-checkout"].split()
    assert sparse == [
        ".github/scripts/queue_tree_diff.py",
        ".github/scripts/queue_gate_check_proof.py",
    ]
