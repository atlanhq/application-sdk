"""Tests for .github/scripts/unit_test_command.py."""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran: list[Sequence[str]] = []

    def _fake_run(argv: Sequence[str]) -> int:
        ran.append(argv)
        return 3

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
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
    assert ran == [
        utc.build_command(
            python_version="3.12",
            runner_os="Linux",
            coverage=True,
            fail_under="85",
            junit_xml=str(tmp_path / "unit-tests-junit.xml"),
        )
    ]


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
    steps = yaml.safe_load(action_text)["runs"]["steps"]
    (run,) = [s["run"] for s in steps if "unit_test_command.py" in s.get("run", "")]
    return run


def test_the_action_passes_inputs_to_the_script_through_env_not_run() -> None:
    """GitHub expands ``${{ }}`` before bash parses ``run:``, so an input holding
    a quote would become shell syntax. Inputs reach the script as env vars."""
    assert "${{" not in _script_call(_ACTION.read_text())


def test_a_leg_writes_junit_only_when_asked() -> None:
    assert not [a for a in _build() if a.startswith("--junitxml")]
    argv = utc.build_command(
        python_version="3.12",
        runner_os="Linux",
        coverage=False,
        fail_under="85",
        junit_xml="/tmp/r/junit.xml",
    )
    assert "--junitxml=/tmp/r/junit.xml" in argv


def _junit(path: Path, cases: dict[str, float]) -> Path:
    body = "".join(
        f'<testcase classname="tests.unit.test_m" name="{name}" time="{secs}"/>'
        for name, secs in cases.items()
    )
    path.write_text(
        f'<testsuites><testsuite name="pytest">{body}</testsuite></testsuites>',
        encoding="utf-8",
    )
    return path


def _report(
    tmp_path: Path, *, runner_os: str, step_seconds: float, cases: dict[str, float]
) -> tuple[list[str], str]:
    emitted: list[str] = []
    summary = tmp_path / "summary.md"
    utc.report_timing(
        runner_os=runner_os,
        step_seconds=step_seconds,
        junit_xml=_junit(tmp_path / "junit.xml", cases),
        emit=emitted.append,
        summary=summary,
    )
    return emitted, summary.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("runner_os", "budget"), [("Windows", 180), ("Linux", 120), ("macOS", 120)]
)
def test_a_step_over_its_budget_is_a_warning(
    tmp_path: Path, runner_os: str, budget: int
) -> None:
    over, _ = _report(tmp_path, runner_os=runner_os, step_seconds=budget + 1, cases={})
    within, _ = _report(tmp_path, runner_os=runner_os, step_seconds=budget, cases={})

    assert over == [
        f"::warning title=Unit tests over budget::the pytest step took "
        f"{budget + 1}s against a {budget}s budget on {runner_os} (FND-4304)"
    ]
    assert within == []


def test_slow_tests_are_annotated_slowest_first_and_capped(tmp_path: Path) -> None:
    cases = {f"test_{i}": 5.0 + i for i in range(1, 13)} | {"test_fast": 5.0}

    emitted, summary = _report(
        tmp_path, runner_os="Linux", step_seconds=60, cases=cases
    )

    assert len(emitted) == utc.MAX_SLOW_TEST_ANNOTATIONS
    assert emitted[0] == (
        "::warning title=Slow unit test::tests.unit.test_m::test_12 took 17.0s "
        "(over 5s)"
    )
    assert emitted[-1].startswith(
        "::warning title=Slow unit test::tests.unit.test_m::test_3 "
    )
    # The summary lists every slow test, not just the annotated ones, and a test
    # at exactly the threshold is not slow.
    assert "`tests.unit.test_m::test_1`" in summary
    assert "test_fast" not in summary
    assert "pytest step: **60s** (budget 120s on Linux)" in summary


def test_a_missing_report_is_said_so_and_still_checks_the_budget(
    tmp_path: Path,
) -> None:
    emitted: list[str] = []
    summary = tmp_path / "summary.md"

    utc.report_timing(
        runner_os="Windows",
        step_seconds=200,
        junit_xml=tmp_path / "absent.xml",
        emit=emitted.append,
        summary=summary,
    )

    assert [e.split("::")[1] for e in emitted] == [
        "warning title=Unit tests over budget"
    ]
    assert "No JUnit report was written" in summary.read_text(encoding="utf-8")


def test_main_reports_after_a_failing_run_and_keeps_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    junit = tmp_path / "unit-tests-junit.xml"
    summary = tmp_path / "summary.md"

    def _fake_run(argv: Sequence[str]) -> int:
        _junit(junit, {"test_slow": 9.5})
        return 1

    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    monkeypatch.setattr(utc, "_run", _fake_run)

    code = utc.main(
        [
            "--python-version",
            "3.12",
            "--runner-os",
            "Linux",
            "--coverage",
            "false",
            "--fail-under",
            "85",
        ]
    )

    assert code == 1
    assert (
        "::warning title=Slow unit test::tests.unit.test_m::test_slow took 9.5s"
        in capsys.readouterr().out
    )
    assert "| 9.5 | `tests.unit.test_m::test_slow` |" in summary.read_text(
        encoding="utf-8"
    )


def test_main_never_reads_a_report_left_by_an_earlier_run(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _junit(tmp_path / "unit-tests-junit.xml", {"test_stale": 50.0})
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(utc, "_run", lambda argv: 0)

    utc.main(
        [
            "--python-version",
            "3.12",
            "--runner-os",
            "Linux",
            "--coverage",
            "false",
            "--fail-under",
            "85",
        ]
    )

    assert "test_stale" not in capsys.readouterr().out


# ── dependency-floor leg (FND-4254) ─────────────────────────────────────────

_WORKFLOW = Path(__file__).parents[2] / "workflows" / "sdk-tests-reusable.yaml"

_MAIN_ARGS = [
    "--python-version",
    "3.11",
    "--runner-os",
    "Linux",
    "--coverage",
    "false",
    "--fail-under",
    "85",
]


def _pyproject(tmp_path: Path, sql: list[str]) -> Path:
    path = tmp_path / "pyproject.toml"
    lines = ",\n".join(f"    {spec!r}".replace("'", '"') for spec in sql)
    path.write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\n'
        f'sql = [\n{lines}\n]\nother = ["pandas>=1.0"]\n'
    )
    return path


def test_floors_are_read_from_the_sql_extra(tmp_path: Path) -> None:
    pyproject = _pyproject(
        tmp_path, ["pandas>=2.2.3,<4.0.0", "pyarrow>=23.0.1,<27.0.0", "duckdb<2"]
    )
    assert utc.dependency_floors(["pandas", "pyarrow"], pyproject) == [
        "pandas==2.2.3",
        "pyarrow==23.0.1",
    ]


def test_floor_names_match_regardless_of_case_and_separator(tmp_path: Path) -> None:
    pyproject = _pyproject(tmp_path, ["duckdb_engine[x]>=0.17.0,<0.18.0"])
    assert utc.dependency_floors(["DuckDB-Engine"], pyproject) == [
        "DuckDB-Engine==0.17.0"
    ]


@pytest.mark.parametrize(
    "sql",
    [
        pytest.param(["pyarrow>=23"], id="absent-from-the-extra"),
        pytest.param(["pandas<4"], id="no-lower-bound"),
    ],
)
def test_a_package_without_a_floor_is_refused(tmp_path: Path, sql: list[str]) -> None:
    """Running the leg at the locked version would pass while testing nothing."""
    with pytest.raises(utc.FloorError, match="pandas"):
        utc.dependency_floors(["pandas"], _pyproject(tmp_path, sql))


def test_floor_pins_are_installed_over_the_frozen_lock() -> None:
    argv = utc.build_command(
        python_version="3.11",
        runner_os="Linux",
        coverage=False,
        fail_under="85",
        floor_pins=["pandas==2.2.3", "pyarrow==23.0.1"],
    )
    assert argv[: argv.index("--import-mode=importlib")] == [
        "uv",
        "run",
        "--frozen",
        "--with",
        "pandas==2.2.3",
        "--with",
        "pyarrow==23.0.1",
        # Not the bare `pytest` script: its shebang is the project venv's
        # python, which never sees the --with overlay, so the leg would run the
        # locked versions. Measured: the bare script imported pandas 3.1.0
        # with --with pandas==2.2.3; `python -m pytest` imported 2.2.3.
        "python",
        "-m",
        "pytest",
    ]


def test_a_leg_without_floors_installs_only_the_lock() -> None:
    assert "--with" not in _build()


def test_main_installs_the_published_floors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ran: list[Sequence[str]] = []
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(utc, "_run", lambda argv: ran.append(argv) or 0)

    assert utc.main([*_MAIN_ARGS, "--dependency-floors", "pandas, pyarrow"]) == 0

    (argv,) = ran
    pins = [argv[i + 1] for i, a in enumerate(argv) if a == "--with"]
    assert pins == utc.dependency_floors(["pandas", "pyarrow"])


def test_main_refuses_a_floor_it_cannot_find_without_running_pytest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran: list[Sequence[str]] = []
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(utc, "_run", lambda argv: ran.append(argv) or 0)

    assert utc.main([*_MAIN_ARGS, "--dependency-floors", "no-such-package"]) == 2
    assert ran == []
    assert "::error title=Dependency floor::" in capsys.readouterr().out


def test_the_floor_job_names_packages_the_sdk_publishes_floors_for() -> None:
    """The workflow's package list must resolve against the real pyproject.toml,
    or the leg would fail at start-up on every PR."""
    job = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["unit-dependency-floors"]
    (step,) = [
        s for s in job["steps"] if s.get("uses") == "./.github/actions/unit-tests"
    ]
    packages = step["with"]["dependency-floors"].split(",")
    assert {"pandas", "pyarrow"} <= set(packages)
    assert len(utc.dependency_floors(packages)) == len(packages)


def test_the_floor_job_runs_on_the_sdk_python_floor() -> None:
    pyproject = tomllib.loads(utc._PYPROJECT.read_text())
    floor = pyproject["project"]["requires-python"].removeprefix(">=")
    job = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["unit-dependency-floors"]
    (step,) = [
        s for s in job["steps"] if s.get("uses") == "./.github/actions/unit-tests"
    ]
    assert step["with"]["python-version"] == floor
