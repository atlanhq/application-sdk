"""The conformance wheel gate is wired before anything ships (FND-4176).

The gate itself was run red-green against real wheels when it was written: a
wheel carrying the 0.51.0 cycle fails the import smoke, and a wheel with its
SARIF schema stripped imports cleanly but fails the test slice. Those runs
need ``uv`` and the network, so they are not repeated here. This pins what a
later edit could silently undo: the gate's position in both workflows, the
slice it runs, and that a failing step fails the gate (``main`` driven over a
stubbed ``subprocess.run``).
"""

from __future__ import annotations

import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Sequence

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import conformance_wheel_gate as gate  # noqa: E402

WORKFLOWS = Path(__file__).resolve().parents[2] / "workflows"
GATE_CMD = "python .github/scripts/conformance_wheel_gate.py"


def _step_runs(workflow: str, job: str) -> list[str]:
    data = yaml.safe_load((WORKFLOWS / workflow).read_text())
    return [step.get("run", "") for step in data["jobs"][job]["steps"]]


def _index(runs: list[str], needle: str) -> int:
    matches = [i for i, run in enumerate(runs) if needle in run]
    assert len(matches) == 1, f"{needle!r} in {len(matches)} steps"
    return matches[0]


def test_release_runs_gate_after_build_before_push() -> None:
    runs = _step_runs("conformance-release.yml", "open-release-pr")
    build = _index(runs, "uv build")
    smoke = _index(runs, GATE_CMD)
    push = _index(runs, "git push")
    assert build < smoke < push


def test_publish_runs_gate_between_build_and_publish() -> None:
    runs = _step_runs("conformance-publish.yml", "publish")
    build = _index(runs, "uv build")
    smoke = _index(runs, GATE_CMD)
    publish = _index(runs, "uv publish")
    assert build < smoke < publish


@pytest.mark.parametrize("name", gate.SLICE)
def test_slice_files_exist(name: str) -> None:
    """A renamed test file would otherwise fail the release, not the PR."""
    assert (gate.PACKAGE_DIR / "tests" / name).is_file()


def _step(argv: Sequence[str]) -> str:
    if argv[:2] == ["uv", "export"]:
        return "export"
    if argv[:2] == ["uv", "venv"]:
        return "venv"
    if argv[:3] == ["uv", "pip", "install"]:
        return "install-local" if "--no-deps" in argv else "install"
    if argv[:3] == ["uv", "pip", "check"]:
        return "check"
    if str(gate.SMOKE) in argv:
        return "smoke"
    if "pytest" in argv:
        return "pytest"
    raise AssertionError(f"unexpected command {argv}")


class _FakeRun:
    """Stands in for subprocess.run: records each step, returns its set code."""

    def __init__(self, codes: dict[str, int]) -> None:
        self.codes = codes
        self.calls: list[tuple[str, list[str], Path]] = []

    def __call__(
        self, argv: Sequence[str], cwd: Path, check: bool = False
    ) -> subprocess.CompletedProcess[bytes]:
        step = _step(list(argv))
        self.calls.append((step, list(argv), Path(cwd)))
        code = self.codes.get(step, 0)
        if check and code:
            raise subprocess.CalledProcessError(code, argv)
        return subprocess.CompletedProcess(argv, code)

    @property
    def steps(self) -> list[str]:
        return [step for step, _, _ in self.calls]


@pytest.fixture
def wheel_dir(tmp_path: Path) -> Path:
    (tmp_path / "atlan_application_sdk_conformance-1.0.0-py3-none-any.whl").touch()
    return tmp_path


def _drive(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path, **codes: int
) -> tuple[_FakeRun, int | BaseException]:
    fake = _FakeRun(codes)
    monkeypatch.setattr(gate.subprocess, "run", fake)
    monkeypatch.setattr(gate.shutil, "copytree", lambda src, dst: None)
    try:
        return fake, gate.main(["--wheel-dir", str(wheel_dir)])
    except subprocess.CalledProcessError as exc:
        return fake, exc


def test_main_passes_when_every_step_passes(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    fake, result = _drive(monkeypatch, wheel_dir)
    assert result == 0
    assert fake.steps == [
        "export", "venv", "install", "install-local", "check", "smoke", "pytest",
    ]  # fmt: skip
    _, smoke, _ = fake.calls[5]
    assert smoke[smoke.index("--outside") + 1] == str(gate.REPO_ROOT)
    _, pytest_argv, pytest_cwd = fake.calls[6]
    assert [f"tests/{name}" for name in gate.SLICE] == pytest_argv[-len(gate.SLICE) :]
    assert not pytest_cwd.is_relative_to(gate.REPO_ROOT)


def test_main_fails_and_skips_slice_when_smoke_fails(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    fake, result = _drive(monkeypatch, wheel_dir, smoke=1)
    assert result == 1
    assert "pytest" not in fake.steps


def test_main_fails_when_slice_fails(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    fake, result = _drive(monkeypatch, wheel_dir, pytest=1)
    assert result == 1
    assert fake.steps[-1] == "pytest"


@pytest.mark.parametrize(
    "step", ["export", "venv", "install", "install-local", "check"]
)
def test_main_fails_when_setup_fails(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path, step: str
) -> None:
    fake, result = _drive(monkeypatch, wheel_dir, **{step: 2})
    assert isinstance(result, subprocess.CalledProcessError)
    assert fake.steps[-1] == step


def test_find_wheel_wants_exactly_one(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        gate.find_wheel(tmp_path)
    wheel = tmp_path / "atlan_application_sdk_conformance-1.0.0-py3-none-any.whl"
    wheel.touch()
    (tmp_path / "atlan_application_sdk_conformance-1.0.0.tar.gz").touch()
    assert gate.find_wheel(tmp_path) == wheel
    (tmp_path / "atlan_application_sdk_conformance-0.9.0-py3-none-any.whl").touch()
    with pytest.raises(SystemExit):
        gate.find_wheel(tmp_path)


def test_path_sourced_packages_are_installed_from_source_not_exported(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    """The conformance lock path-sources the SDK and api (FND-4256). Exported,
    they become ``-e ../../`` lines that resolve against the work dir and would
    install the checkout editable; the gate builds them from source instead."""
    fake, result = _drive(monkeypatch, wheel_dir)
    assert result == 0
    _, export, _ = fake.calls[0]
    for name in gate.LOCAL_PACKAGES:
        assert f"--no-emit-package={name}" in export
    _, install, _ = fake.calls[2]
    _, local, _ = fake.calls[3]
    for path in gate.LOCAL_PACKAGES.values():
        assert str(path) not in install
        assert str(path) in local
        assert (path / "pyproject.toml").is_file()
    assert "-e" not in local
    # Never resolved from the index: a dependency the SDK declares but the
    # lock lacks must fail `uv pip check`, not install at a fresh version.
    assert "--no-deps" in local
    assert fake.steps[4] == "check"


def test_a_stale_lock_fails_before_anything_is_exported(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    """`uv pip check` sees no extras, so a dependency added only to the SDK's
    `[sql]` extra would slip past it. lock_drift runs first and stops the gate."""
    monkeypatch.setattr(gate, "lock_drift", lambda: ["stale"])
    fake, result = _drive(monkeypatch, wheel_dir)
    assert result == 1
    assert fake.steps == []


def test_the_real_lock_parses_into_core_and_sql_requirements() -> None:
    """The synthetic lock below must match the format uv really writes. Not a
    freshness check: that is the gate's job at release, and asserting it here
    would turn unrelated PRs red whenever a root dependency moves first."""
    lock = gate.read_lock(gate.PACKAGE_DIR / "uv.lock")
    core = gate.locked_requirements(lock, "atlan-application-sdk", ())
    with_sql = gate.locked_requirements(lock, "atlan-application-sdk", ("sql",))
    assert core and with_sql
    assert "duckdb" not in {r[0] for r in core}
    assert "duckdb" in {r[0] for r in with_sql}
    api = gate.locked_requirements(lock, "atlan-application-sdk-api", ())
    assert api and "fastapi" in {r[0] for r in api}


_SDK_PYPROJECT = """\
[project]
name = "atlan-application-sdk"
version = "{version}"
dependencies = ["atlan-application-sdk-api=={version}", "httpx>=0.27,<0.29"]
[project.optional-dependencies]
sql = [{sql}]
other = ["boto3>=1"]
"""

_API_PYPROJECT = """\
[project]
name = "atlan-application-sdk-api"
version = "1.0.0"
dependencies = ["Typing_Extensions >= 4.12 , < 5"]
"""

_LOCK = """\
[[package]]
name = "atlan-application-sdk"
[package.metadata]
requires-dist = [
    { name = "atlan-application-sdk-api", editable = "../api" },
    { name = "httpx", specifier = ">=0.27,<0.29" },
    { name = "pandas", marker = "extra == 'sql'", specifier = ">=2.2.3,<4" },
    { name = "boto3", marker = "extra == 'other'", specifier = ">=1" },
]

[[package]]
name = "atlan-application-sdk-api"
[package.metadata]
requires-dist = [{ name = "typing-extensions", specifier = ">=4.12,<5" }]
"""


def _drift(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, sql: str, version: str = "1.0.0"
) -> list[str]:
    sdk, api = tmp_path / "sdk", tmp_path / "api"
    sdk.mkdir()
    api.mkdir()
    (sdk / "pyproject.toml").write_text(_SDK_PYPROJECT.format(version=version, sql=sql))
    (api / "pyproject.toml").write_text(_API_PYPROJECT)
    (tmp_path / "uv.lock").write_text(_LOCK)
    monkeypatch.setattr(
        gate,
        "LOCAL_PACKAGES",
        {"atlan-application-sdk": sdk, "atlan-application-sdk-api": api},
    )
    return gate.lock_drift(tmp_path / "uv.lock")


def test_lock_drift_accepts_a_matching_lock_and_a_version_bump(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Spelling, spacing and specifier order are normalised; the SDK's pin on
    # its own api moves every release and is not a dependency change; an
    # extra the test extra does not select (`other`) is out of scope.
    assert (
        _drift(monkeypatch, tmp_path, sql='"pandas<4,>=2.2.3"', version="9.9.9") == []
    )


@pytest.mark.parametrize(
    "sql",
    [
        '"pandas>=2.2.3,<4", "tomli-w>=1"',  # a dependency added to [sql]
        '"pandas>=2.3,<4"',  # a [sql] floor raised
        "",  # a [sql] dependency dropped
    ],
)
def test_lock_drift_reports_a_sql_extra_the_lock_has_not_seen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, sql: str
) -> None:
    assert _drift(monkeypatch, tmp_path, sql=sql)


def test_local_extras_are_the_ones_the_test_extra_selects() -> None:
    project = tomllib.loads((gate.PACKAGE_DIR / "pyproject.toml").read_text())
    test = project["project"]["optional-dependencies"]["test"]
    sdk = [r for r in test if r.startswith("atlan-application-sdk[")]
    assert len(sdk) == 1, test
    extras = sdk[0].split("[", 1)[1].split("]", 1)[0].split(",")
    assert (
        tuple(e.strip() for e in extras) == gate.LOCAL_EXTRAS["atlan-application-sdk"]
    )
    assert set(gate.LOCAL_EXTRAS) == set(gate.LOCAL_PACKAGES)


def test_local_packages_match_the_conformance_lock_sources() -> None:
    """Every editable source in the conformance lock is one the gate installs
    from source, so a newly path-sourced package cannot slip into the export."""
    lock = tomllib.loads((gate.PACKAGE_DIR / "uv.lock").read_text(encoding="utf-8"))
    editable = {
        p["name"]
        for p in lock["package"]
        if "editable" in p.get("source", {}) and p["source"]["editable"] != "."
    }
    assert editable == set(gate.LOCAL_PACKAGES)
