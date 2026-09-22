"""Guards for the DataForge wake/pause job wiring in tests-reusable.yaml (FND-1992).

The claims carrying the most risk live in YAML, not in the Python the unit tests
cover: whether the legs wait for the wake (ordering, but NOT a hard gate so a
hermetic-fallback connector still falls back), whether pause runs on any outcome
yet stays OUT of the verdict-bearing jobs, and whether the whole thing is off by
default. GitHub Actions' own job-result semantics can't be exercised without a
runner, so these are deliberately YAML-shape assertions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github/workflows/tests-reusable.yaml"
)


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:
    return workflow["jobs"]


@pytest.fixture(scope="module")
def inputs(workflow: dict) -> dict:
    # `on:` round-trips as the YAML 1.1 boolean True under PyYAML.
    on = workflow.get("on") or workflow.get(True)
    return on["workflow_call"]["inputs"]


def test_both_jobs_exist(jobs: dict) -> None:
    assert "wake-dataforge-source" in jobs
    assert "pause-dataforge-source" in jobs


def test_off_by_default(inputs: dict) -> None:
    # The opt-in gate (Chris review #4): must default false so merging this does
    # not switch wake/pause on for every resource-mode connector before its
    # binding carries resource:lifecycle.
    assert inputs["dataforge-lifecycle"]["default"] is False


def _job_if(jobs: dict, name: str) -> str:
    return " ".join(str(jobs[name]["if"]).split())


def test_both_jobs_gated_on_opt_in_and_resource_mode(jobs: dict) -> None:
    for job in ("wake-dataforge-source", "pause-dataforge-source"):
        cond = _job_if(jobs, job)
        assert "inputs.dataforge-lifecycle" in cond, job
        assert "inputs.dataforge-mode == 'resource'" in cond, job


def test_wake_only_when_a_source_is_consumed(jobs: dict) -> None:
    # Chris gap: don't wake a paid instance for a run that reads it nowhere.
    cond = _job_if(jobs, "wake-dataforge-source")
    assert "needs.detect-integration.outputs.count != '0'" in cond
    assert "needs.discover-e2e.outputs.count != '0'" in cond


def test_legs_need_wake_for_ordering_only(jobs: dict) -> None:
    # wake is in each leg's `needs` (so the source is up before the fetch)…
    assert "wake-dataforge-source" in jobs["integration"]["needs"]
    assert "wake-dataforge-source" in jobs["e2e"]["needs"]
    # …but NOT gated in their `if:` — a wake failure must not skip the leg, so a
    # hermetic-fallback connector can still fall back.
    assert "wake-dataforge-source" not in _job_if(jobs, "integration")
    assert "wake-dataforge-source" not in _job_if(jobs, "e2e")


def test_pause_runs_on_any_outcome_after_all_consumers(jobs: dict) -> None:
    pause = jobs["pause-dataforge-source"]
    assert set(pause["needs"]) == {"wake-dataforge-source", "integration", "e2e"}
    cond = _job_if(jobs, "pause-dataforge-source")
    assert cond.startswith("always()")
    # Skips cleanly when wake didn't run (no opt-in / nothing to pause).
    assert "needs.wake-dataforge-source.result != 'skipped'" in cond


def test_pause_kept_out_of_the_verdict_jobs(jobs: dict) -> None:
    # A pause hiccup must not red an otherwise-green run: pause is in no
    # gate/report job's needs.
    for verdict_job in ("report-to-sdk",):
        if verdict_job in jobs:
            assert "pause-dataforge-source" not in jobs[verdict_job].get("needs", [])
    # And no job depends on pause at all.
    for name, job in jobs.items():
        assert "pause-dataforge-source" not in (job.get("needs") or []), name


def test_both_jobs_connect_the_vpn_before_calling(jobs: dict) -> None:
    # Chris blocker #1: api.dataforge.atlan.dev is VPN-gated, so each job must
    # check out + globalprotect-connect before the lifecycle script runs.
    for job in ("wake-dataforge-source", "pause-dataforge-source"):
        steps = jobs[job]["steps"]
        uses = [s.get("uses", "") for s in steps]
        assert any("globalprotect-connect" in u for u in uses), job
        assert any("actions/checkout" in u for u in uses), job
        # the script call comes last, and never puts the resource id on argv
        run_steps = [s.get("run", "") for s in steps if "run" in s]
        assert any("dataforge_source_lifecycle.py --mode" in r for r in run_steps), job
        assert not any("--resource-id" in r for r in run_steps), job


def test_docker_resubnet_is_not_copy_pasted_back(workflow: dict, jobs: dict) -> None:
    """The 172.17/16 re-subnet lives in globalprotect-connect, not at call sites.

    Atlan's internal ELBs share Docker's default bridge subnet, so it has to move
    before the tunnel routes 172.17/16 via tun0. That was four byte-identical
    inline steps — the subnet literals have to agree across all of them or the
    one that drifted silently loses container networking. Asserting the literal
    is absent stops a fifth copy being pasted in rather than the input being set.
    """
    assert "default-address-pools" not in _WORKFLOW.read_text(encoding="utf-8")

    # Every dataforge VPN call site opts in, so folding it in changed nothing.
    for name, job in jobs.items():
        for step in job.get("steps") or []:
            if "globalprotect-connect" not in step.get("uses", ""):
                continue
            with_ = step.get("with") or {}
            if with_.get("mothership-url", "").endswith("dataforge.atlan.dev"):
                assert with_.get("resubnet-docker") == "true", name
