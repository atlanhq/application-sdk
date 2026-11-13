"""Every workflow job that runs steps declares ``timeout-minutes`` (FND-4906).

Without one, GitHub's default is 360 minutes. A job that waits on another
repo's run, or on a callback that never comes, then holds a runner (and, on
the merge queue, a queue slot) for six hours before anything reports it.
``flag_job_timeouts.py`` warns when a job comes close to its cap; this checks
that a cap is set in the first place. See docs/standards/ci.md ("Every job
sets timeout-minutes").

Jobs that call a reusable workflow (``uses:``) are out of scope: GitHub does
not accept ``timeout-minutes`` there, and the called workflow's own jobs are
checked here instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
# Jinja-templated workflows the conformance bootstrap writes into app repos.
# They do not parse as YAML until rendered.
TEMPLATES = (
    REPO_ROOT / "packages" / "conformance" / "conformance" / "bootstrap" / "templates"
)

# A whole-value expression, e.g. ``${{ inputs.timeout-minutes }}``.
_EXPRESSION = re.compile(r"^\$\{\{.*\}\}$", re.S)
# A ``steps:`` key at job level (jobs.<id>.steps) in an unrendered template.
_JOB_STEPS = re.compile(r"^    steps:", re.M)


def _jobs(workflow: object) -> dict[str, Any]:
    """``jobs:`` of a parsed workflow, or empty when it has none."""
    if not isinstance(workflow, dict):
        return {}
    jobs = cast("dict[str, object]", workflow).get("jobs")
    return cast("dict[str, Any]", jobs) if isinstance(jobs, dict) else {}


def missing_timeouts(workflow: dict[str, Any]) -> list[str]:
    """Ids of jobs in *workflow* that run steps without a usable timeout."""
    bad: list[str] = []
    for job_id, job in _jobs(workflow).items():
        if not isinstance(job, dict) or "steps" not in job:
            continue
        timeout = cast("dict[str, object]", job).get("timeout-minutes")
        if isinstance(timeout, bool):
            bad.append(job_id)
        elif isinstance(timeout, int):
            if timeout <= 0:
                bad.append(job_id)
        elif not (isinstance(timeout, str) and _EXPRESSION.match(timeout.strip())):
            bad.append(job_id)
    return bad


def _workflows() -> Iterator[Path]:
    yield from sorted(WORKFLOWS.glob("*.y*ml"))


WORKFLOW_FILES = list(_workflows())


def test_workflows_found() -> None:
    # Guards the guard: a glob that matched nothing would pass every case below.
    assert len(WORKFLOW_FILES) >= 50


def test_step_jobs_found() -> None:
    count = 0
    for path in WORKFLOW_FILES:
        workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        count += sum(
            1
            for job in _jobs(workflow).values()
            if isinstance(job, dict) and "steps" in job
        )
    assert count >= 100


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_every_step_job_declares_timeout(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert not missing_timeouts(workflow), (
        f"{path.name}: jobs without timeout-minutes (GitHub default is 360): "
        f"{missing_timeouts(workflow)}"
    )


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.y*ml")), ids=lambda p: p.name)
def test_templates_do_not_hide_an_uncapped_step_job(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError:
        # Unrendered: the templates only call reusables, whose jobs are checked
        # above. One that grows its own steps has to be checked when rendered.
        assert not _JOB_STEPS.search(text), (
            f"{path.name}: a templated job runs steps; check its timeout-minutes"
        )
        return
    assert not missing_timeouts(workflow or {}), path.name


@pytest.mark.parametrize(
    ("job", "flagged"),
    [
        ({"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}, True),
        ({"steps": [], "timeout-minutes": 0}, True),
        ({"steps": [], "timeout-minutes": True}, True),
        ({"steps": [], "timeout-minutes": "30"}, True),
        ({"steps": [], "timeout-minutes": 30}, False),
        ({"steps": [], "timeout-minutes": "${{ inputs.timeout-minutes }}"}, False),
        ({"uses": "./.github/workflows/x.yaml"}, False),
    ],
)
def test_missing_timeouts_detects(job: dict[str, Any], flagged: bool) -> None:
    assert missing_timeouts({"jobs": {"j": job}}) == (["j"] if flagged else [])
