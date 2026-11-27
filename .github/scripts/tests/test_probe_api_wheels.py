"""Tests for probe_api_wheels.py's pure helpers (the install legs run in CI)."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import probe_api_wheels as probe
import pytest


def _wheel(path: Path, files: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


def test_disjoint_wheels_pass(tmp_path: Path) -> None:
    api = _wheel(tmp_path / "api.whl", {"application_sdk/__init__.py": b"x"})
    sdk = _wheel(tmp_path / "sdk.whl", {"application_sdk/main.py": b"z"})
    tracked = {"application_sdk/__init__.py", "application_sdk/main.py"}
    assert probe.partition(api, sdk, ["application_sdk/__init__.py"], tracked) == []


def test_overlap_misplacement_and_gaps_fail(tmp_path: Path) -> None:
    api = _wheel(
        tmp_path / "api.whl",
        {"application_sdk/a.py": b"1", "application_sdk/stray.py": b"2"},
    )
    sdk = _wheel(
        tmp_path / "sdk.whl",
        {"application_sdk/a.py": b"1", "application_sdk/main.py": b"3"},
    )
    listed = ["application_sdk/a.py", "application_sdk/b.py"]
    tracked = {
        "application_sdk/a.py",
        "application_sdk/b.py",
        "application_sdk/main.py",
        "application_sdk/lost.py",
    }
    assert probe.partition(api, sdk, listed, tracked) == [
        "application_sdk/a.py: ships in both wheels",
        "application_sdk/b.py: listed but not in the api wheel",
        "application_sdk/stray.py: in the api wheel but not listed",
        "application_sdk/b.py: in neither wheel",
        "application_sdk/lost.py: in neither wheel",
    ]


def test_an_explicit_release_is_used_as_is(tmp_path: Path) -> None:
    assert probe.resolve_release("3.39.1", tmp_path) == "3.39.1"


def test_latest_tag_reads_the_newest_reachable_release(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "a",
    )
    git("tag", "v3.39.0")
    git(
        "-c",
        "user.email=t@t",
        "-c",
        "user.name=t",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "b",
    )
    git("tag", "v3.39.1")
    git("tag", "conformance-v0.40.1")
    assert probe.resolve_release("latest-tag", tmp_path) == "3.39.1"
    # The built wheels carry the released version right after a release: probe
    # the upgrade from the release before it, never from itself.
    assert probe.resolve_release("latest-tag", tmp_path, below="3.39.1") == "3.39.0"
    assert probe.resolve_release("latest-tag", tmp_path, below="3.40.0") == "3.39.1"


def test_wheel_pick_ignores_sdists_and_requires_them(tmp_path: Path) -> None:
    """Plain `uv build` leaves sdists beside the wheels: the pick ignores them,
    and a wheel without its sdist (a `--wheel` build) is reported."""
    for name in (
        "atlan_application_sdk-3.50.0-py3-none-any.whl",
        "atlan_application_sdk-3.50.0.tar.gz",
        "atlan_application_sdk_api-3.50.0-py3-none-any.whl",
        "atlan_application_sdk_api-3.50.0.tar.gz",
    ):
        (tmp_path / name).write_bytes(b"")
    sdk = probe._wheel(tmp_path, "atlan_application_sdk")
    api = probe._wheel(tmp_path, "atlan_application_sdk_api")
    assert sdk.name == "atlan_application_sdk-3.50.0-py3-none-any.whl"
    assert api.name == "atlan_application_sdk_api-3.50.0-py3-none-any.whl"
    assert probe.sdist_problems(tmp_path, [api, sdk]) == []

    (tmp_path / "atlan_application_sdk-3.50.0.tar.gz").unlink()
    (problem,) = probe.sdist_problems(tmp_path, [api, sdk])
    assert problem.startswith("atlan_application_sdk-3.50.0-py3-none-any.whl: no ")


def _venv_with(tmp_path: Path, modules: dict[str, str]) -> Path:
    """A pip-less venv with ``modules`` written into its site-packages."""
    where = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(where)], check=True
    )
    python = where / "bin" / "python"
    purelib = subprocess.run(
        [
            str(python),
            "-c",
            "import sysconfig; print(sysconfig.get_paths()['purelib'])",
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    for name, source in modules.items():
        path = Path(purelib, *name.split(".")).with_suffix(".py")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    return python


def test_each_module_imports_in_its_own_process(tmp_path: Path) -> None:
    """`b` only imports once `a` is loaded: one sorted bulk import passes it,
    a fresh process per module names it."""
    python = _venv_with(
        tmp_path,
        {
            "pkg/__init__": "",
            "pkg/a": "import json\n",
            # Order-dependent: reads a module an earlier import left behind.
            "pkg/b": "import sys\nsys.modules['pkg.a'].json\n",
            "pkg/c": "import pkg.a\n",
        },
    )
    modules = ["pkg", "pkg.a", "pkg.b", "pkg.c"]
    bulk = subprocess.run(
        [str(python), "-I", "-c", f"import {', '.join(modules)}"], cwd=tmp_path
    )
    assert bulk.returncode == 0  # the sorted single-process walk hides it
    (failure,) = probe.import_each_alone(python, modules, jobs=2)
    assert failure.startswith("pkg.b: KeyError")


def test_isolated_import_ignores_the_cwd_and_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module only reachable from the cwd or PYTHONPATH (the source tree)
    must not count as installed."""
    python = _venv_with(tmp_path, {})
    (tmp_path / "only_in_cwd.py").write_text("")
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    failures = probe.import_each_alone(python, ["only_in_cwd", "no_such_module_xyz"])
    assert [f.split(":")[0] for f in failures] == ["only_in_cwd", "no_such_module_xyz"]
    assert all("ModuleNotFoundError" in f for f in failures)


def test_every_install_is_bounded(monkeypatch, tmp_path: Path) -> None:
    """A stalled package index must fail the probe, not hold the job until the
    workflow timeout kills it."""
    calls: list[dict] = []

    def fake_run(argv, **kwargs):
        calls.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    py = probe._venv(tmp_path / "v")
    probe._pip(py, "atlan-application-sdk==3.39.1")
    probe._pip_module(py, "-U", "x.whl")
    assert len(calls) == 3
    for call in calls:
        assert call["check"] is True
        assert 0 < call["timeout"] <= probe.INSTALL_TIMEOUT_S, call


_LISTED = [
    "application_sdk/handler/base.py",
    "application_sdk/_obstore_config.py",
    "application_sdk/storage/ops.py",
    "application_sdk/clients/sql.py",
]
_GROUPS = {
    "application_sdk/handler/base.py": {"core"},
    "application_sdk/_obstore_config.py": {"core", "obstore"},
    "application_sdk/storage/ops.py": {"obstore"},
    "application_sdk/clients/sql.py": {"sql"},
}


def _legs(tmp_path: Path) -> dict[str, probe.ApiLeg]:
    api = tmp_path / "atlan_application_sdk_api-1.0-py3-none-any.whl"
    legs = probe.api_legs(api, _LISTED, _GROUPS, ["obstore", "sql"], 120)
    return {leg.name: leg for leg in legs}


def test_the_obstore_leg_installs_only_that_extra(tmp_path: Path) -> None:
    """The host's store path must import without what the other extras bring
    (python-dotenv): the leg installs ``[obstore]`` alone and imports the core
    and obstore modules, never a module only another extra reaches."""
    leg = _legs(tmp_path)["api [obstore]"]
    assert leg.requirement.startswith("atlan-application-sdk-api[obstore] @ file:")
    assert leg.modules.split(",") == [
        "application_sdk.handler.base",
        "application_sdk._obstore_config",
        "application_sdk.storage.ops",
    ]
    assert leg.store_mode == "full"


def test_the_core_and_all_extras_legs_are_unchanged(tmp_path: Path) -> None:
    legs = _legs(tmp_path)
    assert list(legs) == ["api core", "api [obstore]", "api [obstore,sql]"]
    core = legs["api core"]
    assert core.requirement == str(
        tmp_path / "atlan_application_sdk_api-1.0-py3-none-any.whl"
    )
    assert core.modules.split(",") == [
        "application_sdk.handler.base",
        "application_sdk._obstore_config",
    ]
    assert (core.budget_mb, core.store_mode) == (120, "core")
    assert legs["api [obstore,sql]"].modules.split(",") == [
        m.removesuffix(".py").replace("/", ".") for m in _LISTED
    ]


@pytest.mark.parametrize(
    ("failing", "expected"),
    [
        (None, []),
        ("_ALONE", ["api [obstore] install failed"]),
        ("_STORE", ["api [obstore]: object-store routes failed"]),
    ],
)
def test_a_leg_reports_each_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failing: str | None,
    expected: list[str],
) -> None:
    """Each probe a leg runs surfaces as that leg's failure."""
    installed: list[tuple[str, ...]] = []

    def fake_venv(where: Path) -> Path:
        return where / "bin" / "python"

    def fake_pip(_py: Path, *args: str) -> None:
        installed.append(args)

    def fake_alone(_py: Path, _names: list[str], jobs: int) -> list[str]:
        return []

    def fake_run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        script = argv[2]
        code = 1 if failing and script == getattr(probe, failing) else 0
        return subprocess.CompletedProcess(argv, code)

    monkeypatch.setattr(probe, "_venv", fake_venv)
    monkeypatch.setattr(probe, "_pip", fake_pip)
    monkeypatch.setattr(probe, "import_each_alone", fake_alone)

    monkeypatch.setattr(probe.subprocess, "run", fake_run)
    leg = _legs(tmp_path)["api [obstore]"]
    assert probe.run_leg(leg, tmp_path, "every") == expected
    assert installed == [(leg.requirement, "httpx")]
