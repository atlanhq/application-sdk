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
  runner lacks; ``apt_packages`` installs them through
  ``install_apt_packages.py``, the script every reusable's package input goes
  through (its behaviour is tested in test_install_apt_packages.py).

The step order and env routing are asserted as YAML shape (the protected
behaviour is GitHub Actions' own). The pin script is EXECUTED, lifted from the
workflow, so a test cannot pass while the workflow drifts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _scoped_mint import assert_scoped_mint  # noqa: E402

#: Parsed YAML: keys and values are whatever the file holds.
Workflow = dict[Any, Any]
Step = dict[str, Any]

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "build-and-publish-app.yaml"

CERTIFY_JOBS = ["certify", "certify-unit"]
PIN_STEP = "Pin Python to the app image's version"
APT_STEP = "Install system packages (apt_packages)"
INSTALL_STEP = "Install app dependencies"


@pytest.fixture(scope="module")
def workflow() -> Workflow:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _steps(workflow: Workflow, job: str) -> list[Step]:
    return workflow["jobs"][job]["steps"]


def _index(steps: list[Step], name: str) -> int:
    names = [step.get("name") for step in steps]
    assert name in names, f"missing step {name!r}"
    return names.index(name)


def _uses_uv(step: Step) -> bool:
    return "uv " in step.get("run", "") or "certify_unit_shards.py" in step.get(
        "run", ""
    )


# ── Python pin ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_python_is_pinned_before_any_uv_command(workflow: Workflow, job: str) -> None:
    steps = _steps(workflow, job)
    pin = _index(steps, PIN_STEP)
    early = [step.get("name") for step in steps[:pin] if _uses_uv(step)]
    assert not early, f"{job}: uv runs before the Python pin: {early}"


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_pin_runs_after_the_sdk_checkout_it_reads(workflow: Workflow, job: str) -> None:
    steps = _steps(workflow, job)
    sdk = [
        i
        for i, step in enumerate(steps)
        if step.get("with", {}).get("repository") == "atlanhq/application-sdk"
    ]
    assert sdk and sdk[0] < _index(steps, PIN_STEP)


def test_sparse_sdk_checkout_includes_the_dockerfile(workflow: Workflow) -> None:
    """certify-unit checks out only part of the SDK; print-version reads the
    golden tag from its root Dockerfile, so the sparse set must include it."""
    checkout = next(
        step
        for step in _steps(workflow, "certify-unit")
        if step.get("with", {}).get("repository") == "atlanhq/application-sdk"
        and step.get("with", {}).get("path") == "sdk"
    )
    patterns = checkout["with"]["sparse-checkout"].split()
    assert "/Dockerfile" in patterns
    assert ".github/scripts" in patterns


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_pin_exports_the_golden_version(
    workflow: Workflow,
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
    workflow: Workflow,
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


def test_apt_packages_input_is_declared_and_off_by_default(workflow: Workflow) -> None:
    inputs = workflow[True]["workflow_call"]["inputs"]  # `on` parses as True
    assert inputs["apt_packages"]["type"] == "string"
    assert inputs["apt_packages"].get("default", "") == "", (
        "apt_packages must default to empty: a no-op for every existing caller"
    )


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_apt_step_precedes_the_app_install(workflow: Workflow, job: str) -> None:
    steps = _steps(workflow, job)
    apt = _index(steps, APT_STEP)
    assert apt < _index(steps, INSTALL_STEP)
    assert steps[apt]["if"] == "inputs.apt_packages != ''"
    assert "continue-on-error" not in steps[apt], (
        "a rejected or failed apt install must fail the job, not fall through "
        "to an install that will fail further from the cause"
    )


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_apt_step_routes_the_value_through_env(workflow: Workflow, job: str) -> None:
    step = _steps(workflow, job)[_index(_steps(workflow, job), APT_STEP)]
    assert step["env"]["APT_PACKAGES"] == "${{ inputs.apt_packages }}"
    assert "${{" not in step["run"]


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_apt_step_installs_through_the_shared_script(
    workflow: Workflow, job: str
) -> None:
    """The step only hands the value to install_apt_packages.py, read from the
    SDK checkout that precedes it, so the package-name check cannot drift from
    the other reusables' (FND-4908)."""
    steps = _steps(workflow, job)
    apt = _index(steps, APT_STEP)
    assert steps[apt]["run"].strip() == (
        "python3 sdk/.github/scripts/install_apt_packages.py --env APT_PACKAGES"
    )
    withs: list[dict[str, Any]] = [step.get("with") or {} for step in steps]
    sdk = next(
        i
        for i, with_ in enumerate(withs)
        if with_.get("repository") == "atlanhq/application-sdk"
        and with_.get("path") == "sdk"
    )
    assert sdk < apt, "the SDK checkout must land before the script runs"
    sparse = str(withs[sdk].get("sparse-checkout", ""))
    assert not sparse or ".github/scripts" in sparse.split(), (
        "the SDK checkout must include .github/scripts"
    )


def _step_with_id(steps: list[Step], step_id: str) -> Step:
    return next(s for s in steps if s.get("id") == step_id)


def test_contract_drift_runs_the_sdk_regeneration_not_poe(workflow: Workflow) -> None:
    """Drift is checked with the SDK's regeneration, never the app's `poe
    generate` (app-defined shell in a job holding write tokens)."""
    step = _step_with_id(_steps(workflow, "certify"), "contract_drift")
    assert step["run"] == (
        "python3 ../sdk/.github/scripts/build_and_publish/certify.py contract-drift"
    )
    for s in _steps(workflow, "certify"):
        assert "poe" not in str(s.get("run", "")).split(), s.get("name")


def test_contract_drift_does_not_wait_on_the_app_install(workflow: Workflow) -> None:
    """Regeneration needs pkl, not the app's dependencies, so a failed app
    install must not also hide drift."""
    steps = _steps(workflow, "certify")
    assert "if" not in _step_with_id(steps, "contract_drift")
    pkl = next(s for s in steps if s.get("name") == "Install PKL")
    assert "if" not in pkl
    assert steps.index(pkl) < steps.index(_step_with_id(steps, "contract_drift"))


# ── GitHub credentials stay out of app code (FND-4739) ────────────────────────
# Both jobs run app code: build backends during the install, then pytest and
# coverage. Only the install may see a GitHub token, and only through its own
# env: no step writes git config, no checkout persists a token in .git/config,
# and the job token cannot write.

APP_TOKEN = "steps.app-token.outputs.token"
DEPS_TOKEN = "steps.git-deps-token.outputs.token"
INSTALL_ENV = {"PRIVATE_GIT_AUTH": "${{ inputs.private_git_auth }}"}
MIRROR_ENV = {
    "GIT_TOKEN": "${{ steps.git-deps-token.outputs.token }}",
    "PRIVATE_GIT_DEPS": "true",
}


def _is_checkout(step: Step) -> bool:
    return str(step.get("uses", "")).startswith("actions/checkout@")


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_job_token_is_read_only(workflow: Workflow, job: str) -> None:
    """The workflow-level grant includes packages/actions/pull-requests write."""
    assert workflow["jobs"][job]["permissions"] == {"contents": "read"}


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_no_checkout_persists_a_token(workflow: Workflow, job: str) -> None:
    checkouts = [s for s in _steps(workflow, job) if _is_checkout(s)]
    assert checkouts
    for step in checkouts:
        assert step["with"].get("persist-credentials") is False, step.get("name")


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_no_step_writes_git_config(workflow: Workflow, job: str) -> None:
    for step in _steps(workflow, job):
        run = str(step.get("run", ""))
        assert "git config" not in run, step.get("name")
        assert "insteadOf" not in run, step.get("name")


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_deps_token_is_the_scoped_mint(workflow: Workflow, job: str) -> None:
    """Scoped to the private repos the app (checked out at app/) names."""
    assert_scoped_mint(
        _steps(workflow, job),
        mint_id="git-deps-token",
        cond="inputs.private_git_auth",
        project_dir="app",
        # Certify reports rather than stops: a failed mint fails the mirror
        # and install, and the verdict blocks publish on that.
        require="false",
    )


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_no_token_leaks_out_of_the_job(workflow: Workflow, job: str) -> None:
    """certify-unit has no other App token; certify's other one is for the
    SDK checkout only (below)."""
    others = [
        s
        for s in _steps(workflow, job)
        if "create-github-app-token" in str(s.get("uses", ""))
        and s.get("id") != "git-deps-token"
    ]
    assert [s.get("id") for s in others] == (["app-token"] if job == "certify" else [])


def test_sdk_checkout_token_is_scoped_to_the_sdk(workflow: Workflow) -> None:
    """certify's other App token only checks out the SDK, so it reaches no
    other repository."""
    mint = next(s for s in _steps(workflow, "certify") if s.get("id") == "app-token")
    assert mint["with"]["repositories"] == "application-sdk"
    assert mint["with"]["permission-contents"] == "read"
    assert not any(s.get("id") == "app-token" for s in _steps(workflow, "certify-unit"))


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_only_the_mirror_step_carries_the_token(workflow: Workflow, job: str) -> None:
    """The mirror step is the token's only route to a `run:` step, and it is
    the App token alone: no ORG_PAT_GITHUB fallback. The install that runs
    uv (and the build backends uv starts) comes after it, with no token, so
    no ancestor of a build backend holds one (lens round 6)."""
    steps = _steps(workflow, job)
    mirror = _step_with_id(steps, "app_mirror")
    install = _step_with_id(steps, "app_install")
    assert steps.index(mirror) < steps.index(install)
    assert mirror["env"] == MIRROR_ENV
    assert mirror["if"] == "inputs.private_git_auth"
    assert mirror["working-directory"] == "app"
    assert mirror["run"] == "python3 ../sdk/.github/scripts/git_auth.py mirror"
    assert install["env"] == INSTALL_ENV
    assert install["run"] == (
        "python3 ../sdk/.github/scripts/build_and_publish/certify.py install-deps"
    )
    for step in steps:
        if step is mirror:
            continue
        text = yaml.safe_dump(step)
        if step.get("id") != "git-deps-token":
            assert DEPS_TOKEN not in text, step.get("name")
        if APP_TOKEN in text or "ORG_PAT_GITHUB" in text:
            # Only a checkout's `token:`, which is not persisted (above).
            assert _is_checkout(step), step.get("name")
            assert "run" not in step


@pytest.mark.parametrize("job", CERTIFY_JOBS)
def test_post_install_uv_never_relocks(workflow: Workflow, job: str) -> None:
    """After the install no step has git auth, so any later `uv run` must be
    --frozen: a re-lock would try to fetch private deps without a token."""
    steps = _steps(workflow, job)
    after = steps[steps.index(_step_with_id(steps, "app_install")) + 1 :]
    for step in after:
        for line in str(step.get("run", "")).splitlines():
            if "uv run" in line:
                assert "uv run --frozen" in line, line
