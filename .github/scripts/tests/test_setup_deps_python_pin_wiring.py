"""Connector test tiers run on the Python the app image ships (FND-4611).

Until FND-4611, ``setup-deps`` and the connector test actions defaulted
``python-version`` to a literal ``"3.11"``, and ``tests-reusable.yaml`` passed
nothing, so every connector's unit and integration tiers ran on 3.11 while the
image ran 3.13. A repo's ``.python-version`` could move them again, because
``uv`` reads it ahead of the interpreter on ``PATH``.

Now ``setup-deps`` reads the SDK ``Dockerfile``'s golden tag
(``container_python_version.py print-version``, CNCT-85) when the caller names
no version, and exports ``UV_PYTHON``, which outranks ``.python-version``.

Step order and input routing are asserted as YAML shape. The two shell steps
are EXECUTED, lifted from the action, so the test cannot pass while the action
drifts.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
ACTIONS = REPO_ROOT / ".github" / "actions"
SETUP_DEPS = ACTIONS / "setup-deps" / "action.yaml"
TESTS_REUSABLE = REPO_ROOT / ".github" / "workflows" / "tests-reusable.yaml"
SCRIPT = REPO_ROOT / ".github" / "scripts" / "container_python_version.py"

RESOLVE_ID = "image-python"
EXPORT_ID = "python-version"
CONNECTOR_ACTIONS = ["connector-unit-tests", "connector-integration-tests"]

Step = dict[str, Any]


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(path: Path) -> list[Step]:
    return _load(path)["runs"]["steps"]


def _index(steps: list[Step], step_id: str) -> int:
    ids = [step.get("id") for step in steps]
    assert step_id in ids, f"missing step id {step_id!r}"
    return ids.index(step_id)


def _golden_version() -> str:
    return subprocess.run(
        ["python3", str(SCRIPT), "print-version"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _run(
    script: str, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    assert "${{" not in script, "expressions must reach the shell through env"
    return subprocess.run(
        ["bash", "-c", script],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": os.environ.get("PATH", ""), **env},
    )


def _sdk_copy(tmp_path: Path, dockerfile: str | None = None) -> Path:
    """An SDK laid out as setup-deps' SDK_ROOT sees it."""
    sdk = tmp_path / "sdk"
    shutil.copytree(REPO_ROOT / ".github" / "scripts", sdk / ".github" / "scripts")
    if dockerfile is None:
        shutil.copy(REPO_ROOT / "Dockerfile", sdk / "Dockerfile")
    else:
        (sdk / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return sdk


# ── setup-deps: shape ─────────────────────────────────────────────────────────


def test_setup_deps_has_no_literal_default() -> None:
    assert _load(SETUP_DEPS)["inputs"]["python-version"]["default"] == ""


def test_resolve_reads_the_sdk_dockerfile_only_when_no_version_is_named() -> None:
    step = _steps(SETUP_DEPS)[_index(_steps(SETUP_DEPS), RESOLVE_ID)]
    assert step["if"] == "inputs.python-version == ''"
    # The SDK's own Dockerfile, beside the action: never the caller's tree.
    assert step["env"]["SDK_ROOT"] == "${{ github.action_path }}/../../.."
    assert "print-version" in step["run"]
    assert '--dockerfile "${SDK_ROOT}/Dockerfile"' in step["run"]


def test_export_falls_back_to_the_resolved_version() -> None:
    step = _steps(SETUP_DEPS)[_index(_steps(SETUP_DEPS), EXPORT_ID)]
    assert step["env"]["VERSION"] == (
        f"${{{{ inputs.python-version || steps.{RESOLVE_ID}.outputs.version }}}}"
    )


def test_pin_precedes_setup_python_and_every_uv_step() -> None:
    steps = _steps(SETUP_DEPS)
    export = _index(steps, EXPORT_ID)
    assert _index(steps, RESOLVE_ID) < export
    early = [
        step.get("name") or step.get("uses")
        for step in steps[:export]
        if "uv " in step.get("run", "")
        or str(step.get("uses", "")).startswith(
            ("actions/setup-python@", "astral-sh/setup-uv@")
        )
    ]
    assert not early, f"runs before the Python pin: {early}"


def test_everything_downstream_reads_the_exported_version() -> None:
    text = SETUP_DEPS.read_text(encoding="utf-8")
    setup_python = next(
        s
        for s in _steps(SETUP_DEPS)
        if str(s.get("uses", "")).startswith("actions/setup-python@")
    )
    assert setup_python["with"]["python-version"] == (
        f"${{{{ steps.{EXPORT_ID}.outputs.version }}}}"
    )
    # Only the two pin steps may read the raw input; anything else would see
    # "" whenever the caller relies on the default.
    assert text.count("inputs.python-version") == 2


# ── setup-deps: executed ──────────────────────────────────────────────────────


def test_resolve_prints_the_golden_version(tmp_path: Path) -> None:
    sdk = _sdk_copy(tmp_path)
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    step = _steps(SETUP_DEPS)[_index(_steps(SETUP_DEPS), RESOLVE_ID)]

    proc = _run(
        step["run"], tmp_path, {"SDK_ROOT": str(sdk), "GITHUB_OUTPUT": str(out)}
    )

    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == f"version={_golden_version()}\n"


def test_resolve_fails_closed_without_a_golden_tag(tmp_path: Path) -> None:
    """An unreadable Dockerfile must fail the step, not let the job fall back to
    whatever interpreter the runner has."""
    sdk = _sdk_copy(tmp_path, dockerfile="FROM python:3.14\n")
    out = tmp_path / "github_output"
    out.write_text("", encoding="utf-8")
    step = _steps(SETUP_DEPS)[_index(_steps(SETUP_DEPS), RESOLVE_ID)]

    proc = _run(
        step["run"], tmp_path, {"SDK_ROOT": str(sdk), "GITHUB_OUTPUT": str(out)}
    )

    assert proc.returncode != 0
    assert out.read_text(encoding="utf-8") == ""


def test_export_writes_uv_python(tmp_path: Path) -> None:
    out, env_file = tmp_path / "github_output", tmp_path / "github_env"
    out.write_text("", encoding="utf-8")
    env_file.write_text("", encoding="utf-8")
    step = _steps(SETUP_DEPS)[_index(_steps(SETUP_DEPS), EXPORT_ID)]

    proc = _run(
        step["run"],
        tmp_path,
        {"VERSION": "3.13", "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(env_file)},
    )

    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == "version=3.13\n"
    assert env_file.read_text(encoding="utf-8") == "UV_PYTHON=3.13\n"


def test_export_fails_closed_on_an_empty_version(tmp_path: Path) -> None:
    """An empty UV_PYTHON would hand the choice back to .python-version / PATH."""
    out, env_file = tmp_path / "github_output", tmp_path / "github_env"
    out.write_text("", encoding="utf-8")
    env_file.write_text("", encoding="utf-8")
    step = _steps(SETUP_DEPS)[_index(_steps(SETUP_DEPS), EXPORT_ID)]

    proc = _run(
        step["run"],
        tmp_path,
        {"VERSION": "", "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(env_file)},
    )

    assert proc.returncode != 0
    assert env_file.read_text(encoding="utf-8") == ""


@pytest.mark.parametrize(
    "version",
    [
        "3.13\nBASH_ENV=.github/ci-hook.sh",
        "3.13\n",
        "3.13 ",
        "3.13.5",
        "latest",
        "3",
    ],
)
def test_export_rejects_anything_but_one_major_minor(
    tmp_path: Path, version: str
) -> None:
    """A newline in a caller-supplied version would add its own line to
    $GITHUB_ENV (BASH_ENV=... runs code in every later bash step). Nothing
    may reach either command file unless the value is exactly major.minor."""
    out, env_file = tmp_path / "github_output", tmp_path / "github_env"
    out.write_text("", encoding="utf-8")
    env_file.write_text("", encoding="utf-8")
    step = _steps(SETUP_DEPS)[_index(_steps(SETUP_DEPS), EXPORT_ID)]

    proc = _run(
        step["run"],
        tmp_path,
        {"VERSION": version, "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(env_file)},
    )

    assert proc.returncode != 0
    assert out.read_text(encoding="utf-8") == ""
    assert env_file.read_text(encoding="utf-8") == ""


# ── connector tiers ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("action", CONNECTOR_ACTIONS)
def test_connector_action_forwards_an_empty_default(action: str) -> None:
    path = ACTIONS / action / "action.yaml"
    assert _load(path)["inputs"]["python-version"]["default"] == ""
    setup = [
        s
        for s in _steps(path)
        if "/.github/actions/setup-deps" in str(s.get("uses", ""))
    ]
    assert len(setup) == 1
    assert setup[0]["with"]["python-version"] == "${{ inputs.python-version }}"


def test_sdr_e2e_pins_its_host_harness_through_setup_deps() -> None:
    """e2e names its version explicitly (expected-python-version, pinned to the
    Dockerfile by test_container_python_version.py); routing it through
    setup-deps is what exports UV_PYTHON for the harness's `uv run`."""
    path = ACTIONS / "sdr-e2e" / "action.yaml"
    setup = [
        s
        for s in _steps(path)
        if "/.github/actions/setup-deps" in str(s.get("uses", ""))
    ]
    assert len(setup) == 1
    assert setup[0]["with"]["python-version"] == "${{ inputs.expected-python-version }}"


def test_tests_reusable_never_overrides_the_python() -> None:
    """No tier of tests-reusable may name a Python: each one must fall through
    to the image's version. A caller-facing input would let an app opt out."""
    # PyYAML (YAML 1.1) reads a bare `on:` key as the boolean True.
    workflow: dict[object, Any] = yaml.safe_load(
        TESTS_REUSABLE.read_text(encoding="utf-8")
    )
    on: dict[str, Any] = workflow.get("on") or workflow[True]
    assert not [n for n in on["workflow_call"]["inputs"] if "python" in n]
    overrides = [
        f"{job}: {step.get('uses')}"
        for job, body in workflow["jobs"].items()
        for step in body.get("steps", [])
        if "python-version" in (step.get("with") or {})
        and "/.github/actions/" in str(step.get("uses", ""))
    ]
    assert not overrides, overrides
