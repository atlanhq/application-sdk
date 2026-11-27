"""Tests for .github/scripts/python_support_window.py, and the window's wiring.

The supported Python window is declared once, as ``requires-python`` in the
root pyproject.toml. The tests at the bottom fail when the other places that
name it (both package pyprojects, the classifiers, the lock, the CI unit
matrix, the Renovate Python cap) drift from it (FND-5476).
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from collections.abc import Sequence
from pathlib import Path

import pytest
import python_support_window as psw
from _workflows import REPO_ROOT, job, load_workflow

PYPROJECTS = [
    REPO_ROOT / "pyproject.toml",
    REPO_ROOT / "packages" / "api" / "pyproject.toml",
    REPO_ROOT / "packages" / "conformance" / "pyproject.toml",
]


# ---------------------------------------------------------------------------
# parse_window / next_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (">=3.11,<3.15", ["3.11", "3.12", "3.13", "3.14"]),
        (">=3.11, <3.15", ["3.11", "3.12", "3.13", "3.14"]),
        (">=3.12,<3.13", ["3.12"]),
    ],
)
def test_parse_window(spec: str, expected: list[str]):
    assert psw.parse_window(spec) == expected


@pytest.mark.parametrize(
    "spec", [">=3.11", ">=3.11,<4", "<3.15,>=3.11", ">=3.14,<3.14"]
)
def test_parse_window_rejects_uncapped_or_unexpected(spec: str):
    with pytest.raises(ValueError, match="requires-python"):
        psw.parse_window(spec)


def test_next_version():
    assert psw.next_version(["3.11", "3.14"]) == "3.15"


# ---------------------------------------------------------------------------
# declared_requirements
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _repo(tmp_path: Path) -> Path:
    """A root SDK with a path-sourced api package and a workspace member."""
    _write(
        tmp_path / "packages" / "api" / "pyproject.toml",
        '[project]\nname = "atlan-application-sdk-api"\n'
        'dependencies = ["fastapi>=0.1", "orjson>=3"]\n',
    )
    _write(
        tmp_path / "packages" / "conformance" / "pyproject.toml",
        '[project]\nname = "atlan-application-sdk-conformance"\n'
        'dependencies = ["libcst>=1", "atlan-application-sdk>=3"]\n',
    )
    return _write(
        tmp_path / "pyproject.toml",
        "[project]\n"
        'name = "atlan-application-sdk"\n'
        'requires-python = ">=3.11,<3.15"\n'
        'dependencies = ["rocksdict>=0.3.0,<0.4.0", "orjson>=3", '
        '"atlan-application-sdk-api==3.60.0"]\n'
        "[project.optional-dependencies]\n"
        'sql = ["sqlalchemy[asyncio]>=2", "orjson>=3"]\n'
        'all = ["atlan-application-sdk[sql]", "Atlan_Application_SDK[x]"]\n'
        'lint = ["atlan-application-sdk-conformance"]\n'
        "[tool.uv.workspace]\n"
        'members = ["packages/conformance"]\n'
        "[tool.uv.sources]\n"
        "atlan-application-sdk-conformance = { workspace = true }\n"
        'atlan-application-sdk-api = { path = "packages/api", editable = true }\n',
    )


def test_local_packages_reads_path_and_workspace_sources(tmp_path: Path):
    root = _repo(tmp_path)
    assert psw.local_packages(root) == {
        "atlan-application-sdk-api": tmp_path / "packages/api/pyproject.toml",
        "atlan-application-sdk-conformance": tmp_path
        / "packages/conformance/pyproject.toml",
    }


def test_declared_requirements_looks_through_local_packages(tmp_path: Path):
    # The api and conformance packages carry the SDK's own cap, so resolving
    # them would block forever (F-6c4892): their third-party requirements are
    # probed instead, and the SDK's self-references are dropped.
    assert psw.declared_requirements(_repo(tmp_path)) == [
        "rocksdict>=0.3.0,<0.4.0",
        "orjson>=3",
        "sqlalchemy[asyncio]>=2",
        "fastapi>=0.1",
        "libcst>=1",
    ]


def test_declared_requirements_of_this_repo_name_no_local_package():
    names = {psw._requirement_name(r) for r in psw.declared_requirements(PYPROJECTS[0])}
    assert "rocksdict" in names
    assert not {n for n in names if n.startswith("atlan-application-sdk")}


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

NO_WHEEL = (
    "error: No solution found when resolving dependencies\n"
    "  cause: Because rocksdict>=0.3.24b0 has no wheels with a matching Python "
    "ABI tag (e.g., `cp315`) and rocksdict==0.3.0 was yanked, we can conclude\n"
)


class FakeUv:
    """Resolves a requirements file unless it names a blocked package."""

    def __init__(self, blocked: set[str], combined_fails: bool = False):
        self.blocked = blocked
        self.combined_fails = combined_fails
        self.calls: list[list[str]] = []

    def __call__(
        self, args: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert 0 < timeout <= psw.UV_TIMEOUT_S
        args = list(args)
        assert args[:3] == ["uv", "pip", "compile"]
        assert args[args.index("--python-version") + 1] == "3.15"
        assert args[args.index("--python-platform") + 1] == psw.PROBE_PLATFORM
        reqs = Path(args[-1]).read_text(encoding="utf-8").split()
        self.calls.append(reqs)
        failing = [r for r in reqs if r.split(">")[0] in self.blocked]
        if failing or (self.combined_fails and len(reqs) > 1):
            stderr = NO_WHEEL if failing else "error: conflict between a and b\n"
            return subprocess.CompletedProcess(args, 1, "", stderr)
        return subprocess.CompletedProcess(args, 0, "", "")


def test_probe_all_resolve_runs_once():
    uv = FakeUv(blocked=set())
    assert psw.probe(["a>=1", "b>=1"], "3.15", uv) == []
    assert uv.calls == [["a>=1", "b>=1"]]


def test_probe_names_each_blocking_requirement():
    uv = FakeUv(blocked={"rocksdict"})
    blockers = psw.probe(["a>=1", "rocksdict>=0.3.0", "b>=1"], "3.15", uv)
    assert blockers == [
        psw.Blocker(
            "rocksdict>=0.3.0",
            "rocksdict>=0.3.24b0 has no wheels with a matching Python ABI tag "
            "(e.g., `cp315`)",
        )
    ]


def test_probe_reports_a_conflict_only_the_combined_set_has():
    uv = FakeUv(blocked=set(), combined_fails=True)
    blockers = psw.probe(["a>=1", "b>=1"], "3.15", uv)
    assert blockers == [
        psw.Blocker("(combined set)", "error: conflict between a and b")
    ]


# ---------------------------------------------------------------------------
# probe-next command
# ---------------------------------------------------------------------------


def test_probe_stops_when_the_budget_runs_out():
    with pytest.raises(psw.ProbeIncomplete, match="budget"):
        psw.probe(["a>=1"], "3.15", FakeUv(blocked=set()), budget_s=0)


def test_run_turns_a_uv_timeout_into_an_incomplete_probe(
    monkeypatch: pytest.MonkeyPatch,
):
    def stalled(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="uv", timeout=1)

    monkeypatch.setattr(psw.subprocess, "run", stalled)
    with pytest.raises(psw.ProbeIncomplete, match="within 1s"):
        psw._run(["uv"], 1)


def _pyproject(tmp_path: Path, deps: list[str]) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[project]\nname = "x"\nrequires-python = ">=3.11,<3.15"\n'
        f"dependencies = {json.dumps(deps)}\n",
        encoding="utf-8",
    )
    return path


def test_probe_next_reports_blockers_and_still_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    pyproject = _pyproject(tmp_path, ["rocksdict>=0.3.0", "a>=1"])
    rc = psw._cmd_probe_next(pyproject, FakeUv(blocked={"rocksdict"}))
    assert rc == 0
    report = summary.read_text(encoding="utf-8")
    assert "## Python 3.15 install probe" in report
    assert "`rocksdict>=0.3.0`" in report
    assert "`a>=1`" not in report
    out = capsys.readouterr().out
    assert (
        "::warning::SDK cannot install on Python 3.15; blocked by: rocksdict>=0.3.0"
        in out
    )


def test_probe_next_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rc = psw._cmd_probe_next(_pyproject(tmp_path, ["a>=1"]), FakeUv(blocked=set()))
    assert rc == 0
    assert (
        "::notice::All SDK dependencies resolve for Python 3.15"
        in capsys.readouterr().out
    )


def test_probe_next_exits_2_when_uv_cannot_run(tmp_path: Path):
    def missing_uv(
        args: Sequence[str], timeout: float
    ) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError("uv")

    assert psw._cmd_probe_next(_pyproject(tmp_path, ["a>=1"]), missing_uv) == 2


def test_probe_next_reports_an_incomplete_probe_without_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    rc = psw._cmd_probe_next(
        _pyproject(tmp_path, ["a>=1"]), FakeUv(blocked=set()), budget_s=0
    )
    assert rc == 0
    assert "Incomplete:" in summary.read_text(encoding="utf-8")
    assert "::warning::Python 3.15 install probe incomplete" in capsys.readouterr().out


def test_main_print_window(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    pyproject = _pyproject(tmp_path, [])
    assert psw.main(["--pyproject", str(pyproject), "print-window"]) == 0
    assert capsys.readouterr().out == "3.11\n3.12\n3.13\n3.14\nnext=3.15\n"


def test_main_dispatches_probe_next(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    seen: list[Path] = []
    monkeypatch.setattr(psw, "_cmd_probe_next", lambda p: seen.append(p) or 0)
    pyproject = _pyproject(tmp_path, [])
    assert psw.main(["--pyproject", str(pyproject), "probe-next"]) == 0
    assert seen == [pyproject]


# ---------------------------------------------------------------------------
# The repo's window is declared consistently everywhere
# ---------------------------------------------------------------------------


def _window() -> list[str]:
    return psw.parse_window(psw.requires_python(psw.load_project(PYPROJECTS[0])))


def test_every_package_declares_the_same_capped_window():
    specs = {
        p.relative_to(REPO_ROOT).as_posix(): psw.requires_python(psw.load_project(p))
        for p in PYPROJECTS
    }
    assert len(set(specs.values())) == 1, specs
    lock = tomllib.loads((REPO_ROOT / "uv.lock").read_text(encoding="utf-8"))
    assert psw.parse_window(lock["requires-python"]) == _window()


@pytest.mark.parametrize("pyproject", PYPROJECTS, ids=lambda p: p.parent.name)
def test_classifiers_list_exactly_the_window(pyproject: Path):
    prefix = "Programming Language :: Python :: 3."
    classified = [
        c.removeprefix("Programming Language :: Python :: ")
        for c in psw.classifiers(psw.load_project(pyproject))
        if c.startswith(prefix)
    ]
    assert classified == _window()


def test_unit_and_warm_cache_matrices_are_the_window():
    workflow = load_workflow("sdk-tests-reusable.yaml")
    assert job(workflow, "unit")["strategy"]["matrix"]["python-version"] == _window()
    warm = job(load_workflow("warm-caches.yaml"), "unit-venvs")
    assert warm["strategy"]["matrix"]["python-version"] == _window()
    guard = job(workflow, "python-version-guard")
    run = "\n".join(s.get("run", "") for s in guard["steps"])
    assert f'--matrix "{",".join(_window())}"' in run


def test_renovate_holds_the_runtime_below_the_next_python():
    config = json.loads((REPO_ROOT / "renovate.json").read_text(encoding="utf-8"))
    caps = [
        rule["allowedVersions"]
        for rule in config["packageRules"]
        if rule.get("matchDepNames") == ["python"] and "allowedVersions" in rule
    ]
    assert caps == [f"<{psw.next_version(_window())}"]


def test_probe_job_runs_the_script_without_gating():
    probe = job(load_workflow("sdk-tests-reusable.yaml"), "next-python-probe")
    runs = [s["run"] for s in probe["steps"] if "run" in s]
    assert any("python_support_window.py probe-next" in r for r in runs), runs
    # A report, never a gate: the script exits 0 on blockers, so nothing here
    # may hide a crash of the probe itself.
    assert "continue-on-error" not in probe
    assert all("continue-on-error" not in s for s in probe["steps"])
