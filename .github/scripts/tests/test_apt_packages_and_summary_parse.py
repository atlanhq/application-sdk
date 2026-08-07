"""Guards for two FND-110 remediations in the tests-reusable pipeline.

1. ``apt-packages`` input wiring (hive): connectors whose Python deps build a
   native extension (pykerberos → gssapi/gssapi.h) need system headers BEFORE
   the deps action's ``uv sync``; a reusable-workflow caller cannot inject
   steps, so the reusable must carry the install step in every job that syncs
   on the runner (unit, integration, e2e). YAML-shape assertions, same
   rationale as test_prepare_tenant_wiring.py: the protected behaviour is
   GitHub Actions' own and cannot be exercised without a runner.

2. Pytest summary extraction (dataplex): a repo that forces ``--color=yes``
   in its own pytest config wraps the summary line in ANSI escapes; the
   composites' anchored grep then matched nothing and, under ``set -e``,
   failed the step after a fully green suite. These tests EXECUTE the
   extraction pipeline exactly as it appears in each action's script, against
   plain, colored, and absent summaries.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
REUSABLE = REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"
UNIT_ACTION = REPO_ROOT / ".github" / "actions" / "connector-unit-tests" / "action.yaml"
INTEGRATION_ACTION = (
    REPO_ROOT / ".github" / "actions" / "connector-integration-tests" / "action.yaml"
)

APT_STEP_NAME = "Install system packages (apt-packages)"
# Jobs that run `uv sync` on the RUNNER (the e2e image build is excluded by
# design: the Dockerfile owns its own system deps).
RUNNER_SYNC_JOBS = ["unit", "integration", "e2e"]


@pytest.fixture(scope="module")
def workflow() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(REUSABLE.read_text())


@pytest.fixture(scope="module")
def jobs(workflow: dict) -> dict:  # type: ignore[type-arg]
    return workflow["jobs"]


def test_apt_packages_input_is_declared_and_off_by_default(workflow: dict) -> None:  # type: ignore[type-arg]
    inputs = workflow[True]["workflow_call"]["inputs"]  # `on` parses as True
    assert "apt-packages" in inputs, "apt-packages input missing from tests-reusable"
    assert inputs["apt-packages"].get("default", "") == "", (
        "apt-packages must default to empty — the step must be a no-op for "
        "every existing caller"
    )


def _apt_step(job: dict) -> dict:  # type: ignore[type-arg]
    steps = [s for s in job["steps"] if s.get("name") == APT_STEP_NAME]
    assert steps, f"missing '{APT_STEP_NAME}' step"
    return steps[0]


@pytest.mark.parametrize("job_name", RUNNER_SYNC_JOBS)
def test_every_runner_sync_job_installs_before_deps(jobs: dict, job_name: str) -> None:  # type: ignore[type-arg]
    """The step exists, is gated on the input, and precedes any deps/composite
    step — a post-sync install would be dead code for the failure it fixes."""
    job = jobs[job_name]
    step = _apt_step(job)
    assert "inputs.apt-packages != ''" in step.get(
        "if", ""
    ), f"{job_name}: apt step must self-skip when the input is empty"
    names = [s.get("name") or s.get("uses", "") for s in job["steps"]]
    apt_idx = names.index(APT_STEP_NAME)
    sync_idx = [
        i
        for i, s in enumerate(job["steps"])
        if "connector-" in str(s.get("uses", "")) or "sdr-e2e" in str(s.get("uses", ""))
    ]
    assert sync_idx, f"{job_name}: no deps/composite step found to order against"
    assert apt_idx < min(
        sync_idx
    ), f"{job_name}: apt step must run before the composite that performs uv sync"


@pytest.mark.parametrize("job_name", RUNNER_SYNC_JOBS)
def test_apt_step_routes_the_value_through_env(jobs: dict, job_name: str) -> None:  # type: ignore[type-arg]
    """docs/standards/ci.md: caller-controlled values reach `run:` via env,
    never by direct ``${{ }}`` interpolation into the script text."""
    step = _apt_step(jobs[job_name])
    assert step.get("env", {}).get("APT_PACKAGES") == "${{ inputs.apt-packages }}"
    assert (
        "${{" not in step["run"]
    ), f"{job_name}: apt step interpolates an expression into the shell script"


# ---------------------------------------------------------------------------
# Summary extraction: run the real pipeline lines from each action's script.
# ---------------------------------------------------------------------------

PLAIN = "== 874 passed, 11 warnings in 19.87s ==\n"
COLORED = "\x1b[33m====== \x1b[32m874 passed\x1b[0m\x1b[33m, 11 warnings in 19.87s ======\x1b[0m\n"


def _extraction_lines(action_path: Path, source_file: str) -> str:
    """Pull the ESC=/SUMMARY= pipeline verbatim from the action's run script."""
    doc = yaml.safe_load(action_path.read_text())
    scripts = [
        s.get("run", "") for s in doc["runs"]["steps"] if "SUMMARY=" in s.get("run", "")
    ]
    assert scripts, f"{action_path.name}: no step extracts SUMMARY"
    script = scripts[0]
    lines = [
        line
        for line in script.splitlines()
        if line.strip().startswith(("ESC=", "SUMMARY="))
    ]
    assert len(lines) == 2, (
        f"{action_path.name}: expected the ESC= and SUMMARY= pipeline lines — "
        "if the extraction was rewritten, update this test to execute the new form"
    )
    assert (
        source_file in lines[1]
    ), f"{action_path.name}: SUMMARY line no longer reads {source_file}"
    return "\n".join(lines)


@pytest.mark.parametrize(
    ("action_path", "source_file"),
    [
        (UNIT_ACTION, "/tmp/unit-test-output.txt"),
        (INTEGRATION_ACTION, "results/test-output.txt"),
    ],
    ids=["unit", "integration"],
)
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        (PLAIN, "874 passed, 11 warnings in 19.87s"),
        (COLORED, "874 passed, 11 warnings in 19.87s"),
        ("no summary here\n", "no summary parsed"),
    ],
    ids=["plain", "ansi-colored", "absent"],
)
def test_summary_extraction_survives_color_and_absence(
    tmp_path: Path, action_path: Path, source_file: str, content: str, expected: str
) -> None:
    pipeline = _extraction_lines(action_path, source_file)
    out_file = tmp_path / Path(source_file).name
    out_file.write_text(content)
    script = (
        "set -euo pipefail\n"
        + pipeline.replace(source_file, str(out_file))
        + '\necho "PARSED=${SUMMARY:-no summary parsed}"\n'
    )
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0, (
        f"extraction pipeline failed under set -e (the dataplex regression):\n"
        f"stderr: {proc.stderr}"
    )
    match = re.search(r"^PARSED=(.*)$", proc.stdout, re.M)
    assert match and match.group(1) == expected, proc.stdout
