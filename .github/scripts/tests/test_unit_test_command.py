"""Tests for .github/scripts/unit_test_command.py."""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import unit_test_command as utc


def _build(
    *,
    python_version: str = "3.12",
    runner_os: str = "Linux",
    coverage: bool = False,
    fail_under: str = "85",
) -> list[str]:
    return utc.build_command(
        python_version=python_version,
        runner_os=runner_os,
        coverage=coverage,
        fail_under=fail_under,
    )


def test_a_plain_leg_runs_the_suite_under_xdist_without_coverage() -> None:
    argv = _build()
    assert argv[:5] == ["uv", "run", "--frozen", "pytest", "--import-mode=importlib"]
    assert "tests/" in argv
    assert argv[argv.index("-n") + 1] == "auto"
    assert not [a for a in argv if a.startswith("--cov")]


def test_the_coverage_leg_measures_and_enforces_the_floor() -> None:
    argv = _build(coverage=True, fail_under="85")
    assert "--cov=application_sdk" in argv
    assert "--cov-report=xml" in argv  # read by the PR coverage comment
    assert "--cov-fail-under=85" in argv


@pytest.mark.parametrize("runner_os", ["Linux", "macOS"])
def test_posix_legs_guard_sockets_and_interrupt_hangs_with_sigalrm(
    runner_os: str,
) -> None:
    argv = _build(runner_os=runner_os)
    assert "--disable-socket" in argv
    assert "--allow-unix-socket" in argv
    assert "--timeout=120" in argv
    assert "--timeout-method=signal" in argv


def test_windows_has_no_socket_guard_and_no_sigalrm() -> None:
    argv = _build(runner_os="Windows")
    assert "--disable-socket" not in argv
    assert "--timeout=120" in argv
    assert "--timeout-method=thread" in argv


@pytest.mark.parametrize("python_version", ["3.14", "3.15"])
def test_314_and_later_drop_the_dev_group_and_its_tests(python_version: str) -> None:
    argv = _build(python_version=python_version)
    assert argv[:5] == ["uv", "run", "--frozen", "--no-group", "dev"]
    assert "--ignore=tests/unit/tools" in argv


@pytest.mark.parametrize("python_version", ["3.11", "3.13"])
def test_earlier_pythons_keep_every_group_and_test(python_version: str) -> None:
    argv = _build(python_version=python_version)
    assert "--no-group" not in argv
    assert not [a for a in argv if a.startswith("--ignore=")]


@pytest.mark.parametrize("python_version", ["2.7", "3", "latest"])
def test_an_unparseable_python_version_is_refused(python_version: str) -> None:
    with pytest.raises(ValueError):
        _build(python_version=python_version)


def test_main_runs_the_built_command_and_returns_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[Sequence[str]] = []

    def _fake_run(argv: Sequence[str]) -> int:
        ran.append(argv)
        return 3

    monkeypatch.setattr(utc, "_run", _fake_run)
    code = utc.main(
        [
            "--python-version",
            "3.12",
            "--runner-os",
            "Linux",
            "--coverage",
            "true",
            "--fail-under",
            "85",
        ]
    )
    assert code == 3
    assert ran == [_build(coverage=True)]


@pytest.mark.parametrize("value", ["True", "1", "yes", ""])
def test_coverage_must_be_the_literal_true_or_false(value: str) -> None:
    with pytest.raises(SystemExit):
        utc.main(
            [
                "--python-version",
                "3.12",
                "--runner-os",
                "Linux",
                "--coverage",
                value,
                "--fail-under",
                "85",
            ]
        )


_ACTION = Path(__file__).parents[2] / "actions" / "unit-tests" / "action.yaml"


def _script_call(action_text: str) -> str:
    import yaml

    steps = yaml.safe_load(action_text)["runs"]["steps"]
    (run,) = [s["run"] for s in steps if "unit_test_command.py" in s.get("run", "")]
    return run


def test_the_action_passes_inputs_to_the_script_through_env_not_run() -> None:
    """GitHub expands ``${{ }}`` before bash parses ``run:``, so an input holding
    a quote would become shell syntax. Inputs reach the script as env vars."""
    assert "${{" not in _script_call(_ACTION.read_text())
