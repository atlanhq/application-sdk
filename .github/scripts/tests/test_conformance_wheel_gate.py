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

import ast
import subprocess
import tomllib
from pathlib import Path
from typing import Sequence

import conformance_wheel_gate as gate
import pytest
import yaml

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


CONFORMANCE_TESTS = Path(__file__).resolve().parents[3] / "packages/conformance/tests"


@pytest.mark.parametrize("node", gate.DESELECT)
def test_deselected_tests_exist_in_the_slice(node: str) -> None:
    file, _, func = node.partition("::")
    assert file in gate.SLICE
    assert func.startswith("test"), (
        f"{func} is not a collected test; deselect its tests"
    )
    assert f"def {func}(" in (CONFORMANCE_TESTS / file).read_text()


def _fixture_name(fn: ast.FunctionDef) -> str | None:
    """The name pytest injects *fn* under, or ``None`` when it is not a fixture."""
    for dec in fn.decorator_list:
        call = dec if isinstance(dec, ast.Call) else None
        target = call.func if call else dec
        if ast.unparse(target) not in ("pytest.fixture", "fixture"):
            continue
        for kw in call.keywords if call else ():
            if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                return str(kw.value.value)
        return fn.name
    return None


def checkout_dependent_tests(source: str) -> set[str]:
    """Test functions in *source* that reach ``sdk_root()``.

    Directly, or through a helper they call or a fixture they request, to any
    depth. A fixture is matched by the name pytest injects it under, so
    ``@pytest.fixture(name="sdk_root")`` on ``_sdk_root_fixture`` marks every
    test taking an ``sdk_root`` parameter.
    """
    funcs = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef)]
    calls = {
        fn.name: {
            c.func.id
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        }
        for fn in funcs
    }
    params = {fn.name: {a.arg for a in fn.args.args} for fn in funcs}
    fixtures = {fn.name: _fixture_name(fn) for fn in funcs}
    dependent: set[str] = set()
    dependent_names = {"sdk_root"}  # helpers called, or fixtures requested
    changed = True
    while changed:
        changed = False
        for fn in funcs:
            if fn.name in dependent:
                continue
            uses = calls[fn.name] & dependent_names
            requests = params[fn.name] & {fixtures[d] for d in dependent if fixtures[d]}
            if uses or requests:
                dependent.add(fn.name)
                dependent_names.add(fn.name)
                changed = True
    return {name for name in dependent if name.startswith("test")}


def test_checkout_dependent_tests_follows_helpers_and_fixtures() -> None:
    source = """
import pytest
from tests._helpers import sdk_root

@pytest.fixture(name="sdk_root")
def _root_fixture():
    return sdk_root()

@pytest.fixture
def tree(sdk_root):
    return sdk_root / "application_sdk"

def _scan():
    return sdk_root()

def test_direct():
    sdk_root()

def test_via_named_fixture(sdk_root):
    pass

def test_via_fixture_chain(tree):
    pass

def test_via_helper():
    _scan()

def test_independent(tmp_path):
    pass
"""
    assert checkout_dependent_tests(source) == {
        "test_direct", "test_via_named_fixture", "test_via_fixture_chain", "test_via_helper",
    }  # fmt: skip


@pytest.mark.parametrize("name", gate.SLICE)
def test_slice_tests_needing_the_checkout_are_deselected(name: str) -> None:
    """A slice test reaching ``sdk_root()`` fails outside the checkout under CI."""
    needs_checkout = {
        f"{name}::{test}"
        for test in checkout_dependent_tests((CONFORMANCE_TESTS / name).read_text())
    }
    assert needs_checkout <= set(gate.DESELECT), (
        f"{sorted(needs_checkout - set(gate.DESELECT))} need the monorepo; add to DESELECT"
    )


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
    for node in gate.DESELECT:
        assert pytest_argv[pytest_argv.index(f"tests/{node}") - 1] == "--deselect"
    assert not pytest_cwd.is_relative_to(gate.REPO_ROOT)
    overrides = gate.ini_overrides()
    start = pytest_argv.index("-o")
    assert pytest_argv[start : start + len(overrides)] == overrides


def test_ini_overrides_read_the_package_pyproject(tmp_path: Path) -> None:
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(
        "[tool.pytest.ini_options]\n"
        'asyncio_mode = "strict"\n'
        'asyncio_default_fixture_loop_scope = "module"\n'
    )
    assert gate.ini_overrides(pyproject) == [
        "-o", "asyncio_mode=strict",
        "-o", "asyncio_default_fixture_loop_scope=module",
    ]  # fmt: skip


def test_ini_overrides_cover_the_real_package_config() -> None:
    assert gate.ini_overrides() == [
        "-o", "asyncio_mode=auto",
        "-o", "asyncio_default_fixture_loop_scope=function",
    ]  # fmt: skip


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
    """The SDK is a workspace member and the api a path source (FND-4671).
    Exported, they become ``-e`` lines that would install the checkout
    editable; the gate builds them from source instead."""
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


def test_export_is_the_conformance_closure_of_the_locked_root_lock(
    monkeypatch: pytest.MonkeyPatch, wheel_dir: Path
) -> None:
    """``--locked``, not ``--frozen``: a root lock that predates a dependency
    the SDK gained (in any extra) fails the export instead of shipping the old
    closure. The lock check replaced a hand-written drift comparison when the
    conformance package's own lock was dropped (FND-4671)."""
    fake, result = _drive(monkeypatch, wheel_dir)
    assert result == 0
    _, export, cwd = fake.calls[0]
    assert "--locked" in export and "--frozen" not in export
    assert export[export.index("--package") + 1] == gate.DIST
    assert export[export.index("--extra") + 1] == "test"
    assert "--no-dev" in export
    assert cwd == gate.REPO_ROOT


def test_local_packages_are_the_root_lock_s_local_sources() -> None:
    """Every editable source in the root lock other than conformance itself is
    one the gate installs from source, so a newly path-sourced package cannot
    slip into the export as an ``-e`` line."""
    lock = tomllib.loads((gate.REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    editable = {p["name"] for p in lock["package"] if "editable" in p.get("source", {})}
    assert gate.DIST in editable
    assert editable - {gate.DIST} == set(gate.LOCAL_PACKAGES)


def test_conformance_is_a_workspace_member_with_no_lock_of_its_own() -> None:
    """One lock for the whole tree (FND-4671). A second ``uv.lock`` under the
    package would be resolved by nothing in CI and drift silently."""
    root = tomllib.loads((gate.REPO_ROOT / "pyproject.toml").read_text())
    assert "packages/conformance" in root["tool"]["uv"]["workspace"]["members"]
    assert not (gate.PACKAGE_DIR / "uv.lock").exists()


@pytest.mark.parametrize(
    ("workflow", "job"),
    [
        ("conformance-release.yml", "open-release-pr"),
        ("conformance-publish.yml", "publish"),
    ],
)
def test_the_build_writes_the_wheel_where_the_gate_and_publish_read_it(
    workflow: str, job: str
) -> None:
    """In a workspace, ``uv build`` in a member writes to the workspace root's
    ``dist/`` unless told otherwise. The gate defaults to, and ``uv publish``
    (run from the package directory) reads, ``packages/conformance/dist``."""
    runs = _step_runs(workflow, job)
    build = runs[_index(runs, "uv build")]
    assert "cd packages/conformance && uv build --out-dir dist" in build
