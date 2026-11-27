"""Read GitHub workflow and composite-action YAML for the wiring tests.

A wiring test asserts a property of a ``.github`` YAML file: that a job keeps a
step, that a step passes a flag, that a ``run:`` calls a script. Each one used
to declare its own ``_load`` / ``_job`` / ``_steps`` / ``_step``; these are the
shared versions, and they fail with a message that names the file and what is
missing rather than with a bare ``KeyError`` / ``StopIteration``.

PyYAML reads a workflow's bare ``on:`` key as the boolean ``True``.
``load_workflow`` leaves that as it is; read the triggers with ``triggers``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
ACTIONS = REPO_ROOT / ".github" / "actions"

Doc = dict[str, Any]


def load_workflow(path: Path | str) -> Doc:
    """Parse a workflow or action file.

    A bare name such as ``"tests-reusable.yaml"`` is read from
    ``.github/workflows``; a ``Path`` is read as given.
    """
    target = WORKFLOWS / path if isinstance(path, str) else path
    doc = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert isinstance(doc, dict), f"{target} is not a YAML mapping"
    return doc


def triggers(doc: Doc) -> Doc:
    """The ``on:`` block, under whichever key PyYAML stored it."""
    raw = cast("dict[object, Any]", doc)
    return raw["on"] if "on" in raw else raw[True]


def job(doc: Doc, name: str) -> Doc:
    """The job ``name`` of a parsed workflow."""
    jobs = doc.get("jobs") or {}
    assert name in jobs, f"no job {name!r}; jobs are {sorted(jobs)}"
    found = jobs[name]
    assert isinstance(found, dict), f"job {name!r} is not a mapping"
    return found


def steps(job_or_action: Doc) -> list[Doc]:
    """The steps of a job, or of a parsed composite action (``runs.steps``).

    Fails when there are none. A guard such as "no step writes git config"
    passes vacuously over an empty list, so a job that lost its ``steps:``
    (or a ``uses:`` job, which has none) must not read as compliant.
    """
    container: Doc = job_or_action.get("runs", job_or_action)
    found: Any = container.get("steps")
    assert isinstance(found, list) and found, (
        f"expected a non-empty steps list; keys are {sorted(map(str, job_or_action))}"
    )
    return cast("list[Doc]", found)


def step_named(job_or_action: Doc, name: str) -> Doc:
    """The one step called ``name`` in a job or composite action."""
    matches = [s for s in steps(job_or_action) if s.get("name") == name]
    assert len(matches) == 1, (
        f"expected one step named {name!r}, found {len(matches)}; steps are "
        f"{[s.get('name') for s in steps(job_or_action)]}"
    )
    return matches[0]


#: tests-reusable.yaml is the entry point connectors call; since FND-4676 the
#: tiers it runs live in the three workflows it calls, each at @main.
TESTS_REUSABLE = "tests-reusable.yaml"
TIER_WORKFLOWS = (
    "unit-tests-reusable.yaml",
    "integration-tests-reusable.yaml",
    "e2e-tests-reusable.yaml",
)


def tier_jobs() -> dict[str, Doc]:
    """Every job tests-reusable.yaml runs, wherever it is defined.

    A tier job replaces the tests-reusable.yaml caller job of the same id
    (``unit``, ``integration``, ``e2e``), so a test of "the unit job" reads the
    job that runs the steps rather than the ``uses:`` job that calls it.
    """
    jobs: dict[str, Doc] = dict(load_workflow(TESTS_REUSABLE)["jobs"])
    for name in TIER_WORKFLOWS:
        jobs.update(load_workflow(name)["jobs"])
    return jobs


def tier_job(name: str) -> Doc:
    """The job ``name`` of :func:`tier_jobs`."""
    jobs = tier_jobs()
    assert name in jobs, f"no job {name!r}; jobs are {sorted(jobs)}"
    return jobs[name]


def tier_workflow_of(name: str) -> str:
    """The file (under .github/workflows) whose job ``name`` runs steps."""
    for workflow in (*TIER_WORKFLOWS, TESTS_REUSABLE):
        if name in (load_workflow(workflow).get("jobs") or {}):
            return workflow
    raise AssertionError(f"no job {name!r} in tests-reusable.yaml or its tiers")


def tier_text() -> str:
    """tests-reusable.yaml and its three tier workflows, concatenated."""
    return "\n".join(
        (WORKFLOWS / name).read_text(encoding="utf-8")
        for name in (TESTS_REUSABLE, *TIER_WORKFLOWS)
    )
