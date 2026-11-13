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

# GitHub's cap when a job sets none. A cap at or above it bounds nothing.
DEFAULT_MINUTES = 360
# The one expression form accepted: a reusable workflow's own input, e.g.
# ``${{ inputs.timeout-minutes }}``. Its declared default is checked too.
_INPUT_EXPRESSION = re.compile(r"^\$\{\{\s*inputs\.([A-Za-z0-9_-]+)\s*\}\}$")
# A ``steps:`` key at any indentation in an unrendered template. The templates
# only call reusables, so any ``steps:`` there is one this test cannot check.
_ANY_STEPS = re.compile(r"^\s+steps\s*:", re.M)


def _jobs(workflow: object) -> dict[str, Any]:
    """``jobs:`` of a parsed workflow, or empty when it has none."""
    if not isinstance(workflow, dict):
        return {}
    jobs = cast("dict[str, object]", workflow).get("jobs")
    return cast("dict[str, Any]", jobs) if isinstance(jobs, dict) else {}


def _is_cap(value: object) -> bool:
    """A literal minute count that bounds a job below GitHub's default."""
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 0 < value < DEFAULT_MINUTES
    )


def _field(mapping: object, key: object) -> object:
    """``mapping[key]`` when *mapping* is a dict that has it, else None."""
    if not isinstance(mapping, dict):
        return None
    return cast("dict[object, object]", mapping).get(key)


def _call_inputs(workflow: dict[Any, Any]) -> dict[str, Any]:
    """``on.workflow_call.inputs``; PyYAML reads a bare ``on:`` key as True."""
    on = _field(workflow, "on") or _field(workflow, True)
    inputs = _field(_field(on, "workflow_call"), "inputs")
    return cast("dict[str, Any]", inputs) if isinstance(inputs, dict) else {}


def missing_timeouts(workflow: dict[Any, Any]) -> list[str]:
    """Ids of jobs in *workflow* that run steps without a usable timeout.

    Usable: a literal below 360, or ``${{ inputs.<name> }}`` where *name* is a
    ``workflow_call`` input whose default is a literal below 360.
    """
    bad: list[str] = []
    for job_id, job in _jobs(workflow).items():
        if not isinstance(job, dict) or "steps" not in job:
            continue
        timeout = cast("dict[str, object]", job).get("timeout-minutes")
        if isinstance(timeout, str):
            match = _INPUT_EXPRESSION.match(timeout.strip())
            spec = _call_inputs(workflow).get(match.group(1)) if match else None
            timeout = _field(spec, "default")
        if not _is_cap(timeout):
            bad.append(job_id)
    return bad


def oversized_input_timeouts(workflow: dict[str, Any]) -> list[str]:
    """Ids of jobs that pass a reusable a ``timeout-minutes`` of 360 or more."""
    bad: list[str] = []
    for job_id, job in _jobs(workflow).items():
        if not isinstance(job, dict) or "uses" not in job:
            continue
        with_ = cast("dict[str, object]", job).get("with")
        if not isinstance(with_, dict):
            continue
        value = cast("dict[str, object]", with_).get("timeout-minutes")
        if isinstance(value, int) and not _is_cap(value):
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
        f"{path.name}: jobs without a timeout-minutes below the 360 default: "
        f"{missing_timeouts(workflow)}"
    )


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda p: p.name)
def test_no_caller_raises_an_input_timeout_to_the_default(path: Path) -> None:
    # The input-backed form is bounded by its default only until a caller
    # passes a larger value; in-repo callers may not.
    workflow = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    assert not oversized_input_timeouts(workflow), path.name


@pytest.mark.parametrize("path", sorted(TEMPLATES.glob("*.y*ml")), ids=lambda p: p.name)
def test_templates_do_not_hide_an_uncapped_step_job(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    try:
        workflow = yaml.safe_load(text)
    except yaml.YAMLError:
        # Unrendered: the templates only call reusables, whose jobs are checked
        # above. One that grows its own steps has to be checked when rendered.
        assert not _ANY_STEPS.search(text), (
            f"{path.name}: a templated job runs steps; check its timeout-minutes"
        )
        return
    assert not missing_timeouts(workflow or {}), path.name


_CALL = {"workflow_call": {"inputs": {"t": {"type": "number", "default": 60}}}}
_CALL_BIG = {"workflow_call": {"inputs": {"t": {"type": "number", "default": 360}}}}
_FROM_INPUT = "${{ inputs.t }}"


@pytest.mark.parametrize(
    ("job", "on", "flagged"),
    [
        ({"runs-on": "ubuntu-latest", "steps": [{"run": "true"}]}, None, True),
        ({"steps": [], "timeout-minutes": 0}, None, True),
        ({"steps": [], "timeout-minutes": True}, None, True),
        ({"steps": [], "timeout-minutes": "30"}, None, True),
        ({"steps": [], "timeout-minutes": 360}, None, True),
        ({"steps": [], "timeout-minutes": 359}, None, False),
        ({"steps": [], "timeout-minutes": 30}, None, False),
        ({"steps": [], "timeout-minutes": "${{ 360 }}"}, None, True),
        ({"steps": [], "timeout-minutes": "${{ 30 }}"}, None, True),
        ({"steps": [], "timeout-minutes": _FROM_INPUT}, None, True),
        ({"steps": [], "timeout-minutes": "${{ inputs.other }}"}, _CALL, True),
        ({"steps": [], "timeout-minutes": _FROM_INPUT}, _CALL_BIG, True),
        ({"steps": [], "timeout-minutes": _FROM_INPUT}, _CALL, False),
        ({"uses": "./.github/workflows/x.yaml"}, None, False),
    ],
)
def test_missing_timeouts_detects(
    job: dict[str, Any], on: dict[str, Any] | None, flagged: bool
) -> None:
    # PyYAML parses a bare ``on:`` key as True, which is what the helper reads.
    workflow: dict[Any, Any] = {"jobs": {"j": job}}
    if on is not None:
        workflow[True] = on
    assert missing_timeouts(workflow) == (["j"] if flagged else [])


@pytest.mark.parametrize(("value", "flagged"), [(360, True), (500, True), (90, False)])
def test_oversized_input_timeouts_detects(value: int, flagged: bool) -> None:
    job = {"uses": "./.github/workflows/x.yaml", "with": {"timeout-minutes": value}}
    assert oversized_input_timeouts({"jobs": {"j": job}}) == (["j"] if flagged else [])


@pytest.mark.parametrize(
    "text",
    [
        "jobs:\n  build:\n    steps:\n",
        "jobs:\n  build:\n      runs-on: x\n      steps:\n",
        "jobs:\n\t build:\n\t   steps :\n",
    ],
)
def test_template_steps_detected_at_any_indent(text: str) -> None:
    assert _ANY_STEPS.search(text)
