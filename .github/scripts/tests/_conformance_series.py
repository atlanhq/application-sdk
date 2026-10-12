"""The conformance series as ``conformance-reusable.yaml`` declares them.

The workflow is one job (FND-3318), not a matrix: each series is a detect step
that ``uses`` the run-conformance-detect action, gated on one named filter of
the job's single ``dorny/paths-filter`` step, followed by its SARIF upload.
There is no ``matrix.include`` table to read any more, so the series table is
derived here from those steps — once — for every test that needs it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFORMANCE = REPO_ROOT / ".github" / "workflows" / "conformance-reusable.yaml"

#: The job's id in the reusable.  The caller's job id plus this job's `name`
#: form the required context (`suite / Conformance Gate`).
JOB_ID = "suite"

DETECT_ACTION = "./.github/actions/run-conformance-detect"
CHANGES_STEP_ID = "changes"


@dataclass(frozen=True)
class Series:
    """One rule series: its letter, slug, filter globs and steps."""

    letter: str
    slug: str
    globs: tuple[str, ...]
    needs_env: bool
    detect: dict[str, Any]


def load_workflow() -> dict[str, Any]:
    return yaml.safe_load(CONFORMANCE.read_text(encoding="utf-8"))


def suite_job(workflow: dict[str, Any] | None = None) -> dict[str, Any]:
    return (workflow or load_workflow())["jobs"][JOB_ID]


def filters(job: dict[str, Any]) -> dict[str, list[str]]:
    """``{filter name: globs}`` from the job's one paths-filter step."""
    step = next(s for s in job["steps"] if s.get("id") == CHANGES_STEP_ID)
    return yaml.safe_load(step["with"]["filters"])


def detect_steps(job: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in job["steps"] if s.get("uses") == DETECT_ACTION]


def series(job: dict[str, Any] | None = None) -> list[Series]:
    """Every series, in the order the job runs them."""
    job = job or suite_job()
    by_slug = filters(job)
    out = []
    for step in detect_steps(job):
        with_ = step["with"]
        out.append(
            Series(
                letter=with_["series"],
                slug=with_["slug"],
                globs=tuple(by_slug.get(with_["slug"], ())),
                needs_env=str(with_.get("needs-env", "")) == "true",
                detect=step,
            )
        )
    return out


def paths_glob(entry: Series) -> str:
    """The series' globs as one picomatch alternative list."""
    if len(entry.globs) == 1:
        return entry.globs[0]
    return "{" + ",".join(entry.globs) + "}"
