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
    """The steps of a job, or of a parsed composite action (``runs.steps``)."""
    if "runs" in job_or_action:
        return list(job_or_action["runs"].get("steps") or [])
    return list(job_or_action.get("steps") or [])


def step_named(job_or_action: Doc, name: str) -> Doc:
    """The one step called ``name`` in a job or composite action."""
    matches = [s for s in steps(job_or_action) if s.get("name") == name]
    assert len(matches) == 1, (
        f"expected one step named {name!r}, found {len(matches)}; steps are "
        f"{[s.get('name') for s in steps(job_or_action)]}"
    )
    return matches[0]
