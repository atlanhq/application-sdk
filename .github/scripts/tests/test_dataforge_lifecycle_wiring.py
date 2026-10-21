"""Guards for the DataForge wake/pause job wiring in tests-reusable.yaml (FND-1992).

The claims carrying the most risk live in YAML, not in the Python the unit tests
cover: whether the legs wait for the wake (ordering, but NOT a hard gate so a
hermetic-fallback connector still falls back), whether pause runs on any outcome
yet stays OUT of the verdict-bearing jobs, and whether the whole thing is off by
default. GitHub Actions' own job-result semantics can't be exercised without a
runner, so these are deliberately YAML-shape assertions.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _bump_skip import without_bump_skip  # noqa: E402

_WORKFLOW = (
    Path(__file__).resolve().parents[3] / ".github/workflows/tests-reusable.yaml"
)
_TWINGATE_ACTION = (
    Path(__file__).resolve().parents[3] / ".github/actions/twingate-connect/action.yml"
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
    return " ".join(without_bump_skip(jobs[name]["if"]).split())


def test_both_jobs_gated_on_opt_in_and_resource_mode(jobs: dict) -> None:
    for job in ("wake-dataforge-source", "pause-dataforge-source"):
        cond = _job_if(jobs, job)
        assert "inputs.dataforge-lifecycle" in cond, job
        assert "inputs.dataforge-mode == 'resource'" in cond, job


# `inputs.x`, `needs.j.result`, `needs.j.outputs.k` — the only context reads the
# wake condition makes. Anything else it grows must be added to the contexts below
# rather than silently defaulting, so _evaluate raises on an unknown token.
_CONTEXT_READ = re.compile(
    r"inputs\.[A-Za-z0-9_-]+"
    r"|needs\.[A-Za-z0-9_-]+\.result"
    r"|needs\.[A-Za-z0-9_-]+\.outputs\.[A-Za-z0-9_-]+"
)


def _evaluate(cond: str, context: dict[str, object]) -> bool:
    """Evaluate a GitHub Actions job `if:` against a context.

    Substring assertions cannot catch the bug this guards: an unguarded
    `needs.<job>.outputs.count != '0'` clause is present in the condition either
    way, and is constant-TRUE when the job was skipped (skipped ⇒ outputs are '',
    and '' != '0'). Only evaluating the whole expression against a skipped-job
    context distinguishes the two. `&&`/`||` share Python's and/or precedence, so
    the translation is faithful for the boolean-and-comparison subset used here.
    """

    def _value(match: re.Match[str]) -> str:
        key = match.group(0)
        if key not in context:
            raise AssertionError(f"condition reads {key}; add it to the test context")
        return repr(context[key])

    expr = _CONTEXT_READ.sub(_value, cond.replace("always()", "True"))
    expr = expr.replace("&&", " and ").replace("||", " or ")
    return bool(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307 — our own file


def _wake_context(**overrides: object) -> dict[str, object]:
    """An opted-in resource-mode connector whose e2e legs will run."""
    base: dict[str, object] = {
        "inputs.dataforge-lifecycle": True,
        "inputs.dataforge-datasource": "teradata",
        "inputs.dataforge-mode": "resource",
        "needs.detect-integration.result": "success",
        "needs.detect-integration.outputs.count": "3",
        "needs.discover-e2e.result": "success",
        "needs.discover-e2e.outputs.count": "2",
        "inputs.run-e2e": "",
    }
    return base | overrides


def test_wake_only_when_a_source_is_consumed(jobs: dict) -> None:
    # Chris gap: don't wake a paid instance for a run that reads it nowhere.
    cond = _job_if(jobs, "wake-dataforge-source")

    assert _evaluate(cond, _wake_context()), "a run that consumes the source"
    # Either consumer on its own is enough.
    assert _evaluate(
        cond,
        _wake_context(
            **{
                "needs.detect-integration.outputs.count": "0",
            }
        ),
    ), "e2e legs alone consume the source"
    assert _evaluate(
        cond,
        _wake_context(
            **{
                "needs.discover-e2e.result": "skipped",
                "needs.discover-e2e.outputs.count": "",
            }
        ),
    ), "the integration tier alone consumes the source"

    # The regression this exists for: a unit-only PR. detect-integration ran and
    # found nothing; discover-e2e was SKIPPED for want of the `e2e` label, so its
    # outputs are '' — and an unguarded `'' != '0'` reads as "e2e will run".
    assert not _evaluate(
        cond,
        _wake_context(
            **{
                "needs.detect-integration.outputs.count": "0",
                "needs.discover-e2e.result": "skipped",
                "needs.discover-e2e.outputs.count": "",
            }
        ),
    ), "a unit-only run must not wake the pin"

    # And the opt-in still dominates a run that would otherwise consume it.
    assert not _evaluate(cond, _wake_context(**{"inputs.dataforge-lifecycle": False}))
    assert not _evaluate(cond, _wake_context(**{"inputs.dataforge-mode": "managed"}))
    assert not _evaluate(cond, _wake_context(**{"inputs.dataforge-datasource": ""}))


def test_an_e2e_only_run_wakes_for_its_legs_alone(jobs: dict) -> None:
    """FND-3839: a `/e2e` comment runs e2e only, so detect-integration is
    SKIPPED on purpose. The wake must still happen for the e2e legs — and a
    skipped detection's empty count must not count as integration suites."""
    cond = _job_if(jobs, "wake-dataforge-source")
    e2e_only = {
        "inputs.run-e2e": "only",
        "needs.detect-integration.result": "skipped",
        "needs.detect-integration.outputs.count": "",
    }
    assert _evaluate(cond, _wake_context(**e2e_only)), "e2e legs need the source"
    assert not _evaluate(
        cond,
        _wake_context(
            **e2e_only,
            **{
                "needs.discover-e2e.result": "skipped",
                "needs.discover-e2e.outputs.count": "",
            },
        ),
    ), "a stood-down e2e-only run reads the source nowhere"
    # A skipped detection outside e2e-only still means "do not wake".
    assert not _evaluate(
        cond,
        _wake_context(
            **{
                "needs.detect-integration.result": "skipped",
                "needs.detect-integration.outputs.count": "",
            }
        ),
    )


def test_evaluator_would_catch_the_unguarded_count(jobs: dict) -> None:
    """Red-green proof for the guard above: the old expression must FAIL it.

    Without this, `test_wake_only_when_a_source_is_consumed` passing says nothing
    about whether the evaluator can tell the two expressions apart.
    """
    unguarded = (
        "always() && inputs.dataforge-lifecycle && "
        "inputs.dataforge-datasource != '' && inputs.dataforge-mode == 'resource' && "
        "needs.detect-integration.result == 'success' && "
        "(needs.detect-integration.outputs.count != '0' || "
        "needs.discover-e2e.outputs.count != '0')"
    )
    unit_only = _wake_context(
        **{
            "needs.detect-integration.outputs.count": "0",
            "needs.discover-e2e.result": "skipped",
            "needs.discover-e2e.outputs.count": "",
        }
    )
    assert _evaluate(unguarded, unit_only), "the bug: skipped discover reads as 'e2e'"
    assert not _evaluate(_job_if(jobs, "wake-dataforge-source"), unit_only)


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


def test_both_jobs_try_twingate_before_globalprotect(
    workflow: dict, jobs: dict
) -> None:
    on = workflow.get("on") or workflow.get(True)
    assert "TWINGATE_SHARED_KEY" in on["workflow_call"]["secrets"]
    for job in ("wake-dataforge-source", "pause-dataforge-source"):
        steps = jobs[job]["steps"]
        uses = [s.get("uses", "") for s in steps]
        tg = next(i for i, u in enumerate(uses) if "twingate-connect" in u)
        gp = next(i for i, u in enumerate(uses) if "globalprotect-connect" in u)
        assert tg < gp, job

        twingate, globalprotect = steps[tg], steps[gp]
        cleanup = steps[gp - 1]
        pin_gate = "env.DATAFORGE_RESOURCE_ID != '' && env.E2E_SOURCE_ENV_JSON == ''"
        for step in (twingate, cleanup, globalprotect):
            assert step["if"].startswith(pin_gate), (job, step["name"])
        assert "env.HAS_TWINGATE_KEY == 'true'" in twingate["if"], job
        assert jobs[job]["env"]["HAS_TWINGATE_KEY"] == (
            "${{ secrets.TWINGATE_SHARED_KEY != '' }}"
        )
        assert "steps.twingate.outcome == 'failure'" in cleanup["if"], job
        assert "steps.twingate.outcome == 'cancelled'" in cleanup["if"], job
        assert "twingate stop" in cleanup["run"], job
        assert cleanup["continue-on-error"] is True, job
        assert "/etc/resolv.conf" in cleanup["run"], job
        assert "twingate.list" in cleanup["run"], job
        # docs/standards/ci.md: no branching in inlined shell.
        assert cleanup["run"].startswith("set -euo pipefail\n"), job
        assert not re.search(
            r"^\s*(if|for|while|case)\b", cleanup["run"], re.MULTILINE
        ), job
        assert (
            f"steps.{twingate['id']}.outputs.connected != 'true'" in globalprotect["if"]
        ), job

        assert twingate["continue-on-error"] is True, job
        assert twingate["with"]["service-key"] == "${{ secrets.TWINGATE_SHARED_KEY }}"
        assert twingate["with"]["mothership-url"] == "https://api.dataforge.atlan.dev"
        assert "TWINGATE_SHARED_KEY" not in (jobs[job].get("env") or {}), job
        assert twingate["timeout-minutes"] < jobs[job]["timeout-minutes"], job

        checkout = next(s for s in steps if "actions/checkout" in s.get("uses", ""))
        assert ".github/actions/twingate-connect" in checkout["with"]["sparse-checkout"]


def test_test_jobs_try_twingate_before_globalprotect(jobs: dict) -> None:
    for job in ("integration", "e2e"):
        steps = jobs[job]["steps"]
        uses = [s.get("uses", "") for s in steps]
        tg = next(i for i, u in enumerate(uses) if "twingate-connect" in u)
        gp = next(i for i, u in enumerate(uses) if "globalprotect-connect" in u)
        assert tg < gp, job

        twingate, cleanup, globalprotect = steps[tg], steps[gp - 1], steps[gp]
        gate = "inputs.dataforge-datasource != '' && env.E2E_SOURCE_ENV_JSON == ''"
        for step in (twingate, cleanup, globalprotect):
            assert step["if"].startswith(gate), (job, step["name"])
            assert step["continue-on-error"] is True, (job, step["name"])
        assert "env.HAS_TWINGATE_KEY == 'true'" in twingate["if"], job
        assert jobs[job]["env"]["HAS_TWINGATE_KEY"] == (
            "${{ secrets.TWINGATE_SHARED_KEY != '' }}"
        )
        assert "steps.twingate.outcome == 'failure'" in cleanup["if"], job
        assert "steps.twingate.outcome == 'cancelled'" in cleanup["if"], job
        assert "twingate stop" in cleanup["run"], job
        assert cleanup["run"].startswith("set -euo pipefail\n"), job
        assert not re.search(
            r"^\s*(if|for|while|case)\b", cleanup["run"], re.MULTILINE
        ), job
        assert "steps.twingate.outputs.connected != 'true'" in globalprotect["if"], job
        assert globalprotect["with"]["resubnet-docker"] == "true", job
        assert twingate["with"]["resubnet-docker"] == "true", job

        assert (
            twingate["with"]["service-key"] == "${{ secrets.TWINGATE_SHARED_KEY }}"
        ), job
        assert (
            twingate["with"]["mothership-url"] == "https://api.dataforge.atlan.dev"
        ), job
        assert "TWINGATE_SHARED_KEY" not in jobs[job]["env"], job

        checkout = next(
            s
            for s in steps
            if (s.get("with") or {}).get("path") == "application-sdk-scripts"
        )
        assert ".github/actions/twingate-connect" in checkout["with"]["sparse-checkout"]


def test_twingate_inner_limits_fit_the_step_limit(jobs: dict) -> None:
    action = yaml.safe_load(_TWINGATE_ACTION.read_text(encoding="utf-8"))
    script = next(s for s in action["runs"]["steps"] if s.get("id") == "connect")["run"]
    bounded = sum(
        int(n) + int(k or 0)
        for k, n in re.findall(r"timeout (?:-k (\d+) )?(\d+) ", script)
    )
    # A retried curl can spend --max-time on every attempt plus the delays. The
    # probe curl (`-w`) is excluded here: probe_attempts below already counts it.
    for flags in re.findall(r"curl (?![^\n]*-w )([^\n]*--max-time \d+)", script):
        max_time = int(re.search(r"--max-time (\d+)", flags).group(1))
        retries = re.search(r"--retry (\d+)", flags)
        delay = re.search(r"--retry-delay (\d+)", flags)
        n = int(retries.group(1)) if retries else 0
        bounded += max_time * (n + 1) + n * (int(delay.group(1)) if delay else 1)
    probe_attempts = int(action["inputs"]["probe-attempts"]["default"])
    bounded += probe_attempts * (10 + 3)
    assert "timeout " in script and "-k " in script
    for job in (
        "wake-dataforge-source",
        "pause-dataforge-source",
        "integration",
        "e2e",
    ):
        step = next(
            s for s in jobs[job]["steps"] if "twingate-connect" in s.get("uses", "")
        )
        assert bounded < step["timeout-minutes"] * 60, (job, bounded)


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


def test_both_vpn_actions_resubnet_docker_identically() -> None:
    def resubnet(path: Path) -> dict:
        steps = yaml.safe_load(path.read_text(encoding="utf-8"))["runs"]["steps"]
        step = next(s for s in steps if "default-address-pools" in s.get("run", ""))
        assert steps.index(step) == 0, path
        return step

    gp = _WORKFLOW.parents[1] / "actions/globalprotect-connect/action.yml"
    assert resubnet(_TWINGATE_ACTION) == resubnet(gp)
