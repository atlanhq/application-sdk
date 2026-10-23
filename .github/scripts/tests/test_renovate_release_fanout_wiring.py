"""Guard: the SDK and conformance release fan-outs coalesce into one (FND-4330).

The two bump PRs merge seconds apart and each publish workflow has its own
fan-out job. They only fan out once, after both releases are visible, if:

* both jobs share one cancel-in-progress concurrency group, so the later one
  cancels the earlier one while it is still waiting; and
* both wait for the same full set of first-party versions read from `main`,
  so the survivor waits for its sibling's release as well as its own.

If either job drifts (its own group, or waiting only on its own package), the
fleet gets two sweeps again, the first one before the second package exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
_FILES = ["tag-and-publish.yaml", "conformance-publish.yml"]
_PYPROJECTS = [
    "pyproject.toml",
    "packages/api/pyproject.toml",
    "packages/conformance/pyproject.toml",
]


def _fan_out(name: str) -> dict:
    data = yaml.safe_load((_WORKFLOWS / name).read_text(encoding="utf-8"))
    return data["jobs"]["fan-out"]


def _run_lines(job: dict) -> str:
    return "\n".join(str(s.get("run", "")) for s in job["steps"])


@pytest.mark.parametrize("name", _FILES)
def test_fan_out_shares_the_cancelling_group(name):
    concurrency = _fan_out(name)["concurrency"]
    assert concurrency["group"] == "renovate-release-fanout"
    assert concurrency["cancel-in-progress"] is True


@pytest.mark.parametrize("name", _FILES)
def test_fan_out_waits_for_every_first_party_package_on_main(name):
    job = _fan_out(name)
    checkout = next(s for s in job["steps"] if "actions/checkout" in str(s.get("uses")))
    assert checkout["with"]["ref"] == "main"

    wait = next(
        s for s in job["steps"] if "wait_for_pypi_release.py" in str(s.get("run"))
    )
    assert (
        wait.get("continue-on-error") is True
    ), "a wait failure must not skip the dispatch"
    for pyproject in _PYPROJECTS:
        assert f"--pyproject {pyproject}" in wait["run"]


@pytest.mark.parametrize("name", _FILES)
def test_fan_out_dispatches_only_through_the_followup(name):
    job = _fan_out(name)
    runs = _run_lines(job)
    assert "renovate_release_followup.py" in runs
    assert "steps.pypi.outputs.packages" in str(job["steps"][-1].get("env"))
    # A bare `gh workflow run` beside the follow-up would be a second sweep.
    assert "gh workflow run" not in runs


def _steps_without_names(job: dict) -> list[dict]:
    return [{k: v for k, v in s.items() if k != "name"} for s in job["steps"]]


def test_both_fan_outs_run_the_same_steps():
    sdk, conf = (_fan_out(n) for n in _FILES)
    assert _steps_without_names(sdk) == _steps_without_names(conf)
