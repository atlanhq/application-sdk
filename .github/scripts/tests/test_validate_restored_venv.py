"""validate_restored_venv.py rejects venvs whose interpreter is gone or changed.

A venv restored from the setup-deps cache can link to a uv-managed Python that
only existed on the runner that built it. A cache hit skips ``uv sync``, so
without this check the first ``uv run`` rebuilds the venv with default groups
only and the e2e leg fails with ``Failed to spawn: pytest``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / ".github/scripts/validate_restored_venv.py"
ACTION = REPO_ROOT / ".github/actions/setup-deps/action.yaml"

_spec = importlib.util.spec_from_file_location("validate_restored_venv", SCRIPT)
assert _spec and _spec.loader
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

_HERE = ".".join(map(str, sys.version_info[:3]))


def _venv(tmp_path: Path, *, target: str, version: str | None) -> Path:
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(target)
    if version is not None:
        (venv / "pyvenv.cfg").write_text(
            f"home = /somewhere\nimplementation = CPython\nversion_info = {version}\n"
        )
    return venv


def _run(venv: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert mod.main([str(venv)]) == 0
    return out.read_text()


def test_working_venv_is_kept(tmp_path, monkeypatch):
    venv = _venv(tmp_path, target=sys.executable, version=_HERE)
    assert _run(venv, tmp_path, monkeypatch) == "usable=true\n"
    assert venv.is_dir()


def test_dangling_interpreter_is_removed(tmp_path, monkeypatch):
    # The failure mode: the venv links into an uncached uv-python-dir.
    venv = _venv(
        tmp_path,
        target="/nonexistent/uv-python-dir/cpython-3.12/bin/python3.12",
        version="3.12.16",
    )
    assert _run(venv, tmp_path, monkeypatch) == "usable=false\n"
    assert not venv.exists()


def test_interpreter_version_changed_is_removed(tmp_path, monkeypatch):
    # /usr/bin/python3 resolves on both images but moved to another version.
    venv = _venv(tmp_path, target=sys.executable, version="2.7.18")
    assert _run(venv, tmp_path, monkeypatch) == "usable=false\n"
    assert not venv.exists()


def test_missing_pyvenv_cfg_is_removed(tmp_path, monkeypatch):
    venv = _venv(tmp_path, target=sys.executable, version=None)
    assert _run(venv, tmp_path, monkeypatch) == "usable=false\n"
    assert not venv.exists()


def test_no_venv_is_not_usable(tmp_path, monkeypatch):
    assert _run(tmp_path / ".venv", tmp_path, monkeypatch) == "usable=false\n"


def test_venv_tool_version_key_is_accepted(tmp_path):
    # python -m venv writes `version = X.Y.Z` rather than uv's `version_info`.
    venv = tmp_path / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").symlink_to(sys.executable)
    (venv / "pyvenv.cfg").write_text(f"home = /x\nversion = {_HERE}\n")
    assert mod.check(venv) == (True, "")


def _steps() -> list[dict]:
    return yaml.safe_load(ACTION.read_text())["runs"]["steps"]


def test_setup_deps_validates_before_installing():
    steps = _steps()
    ids = [s.get("id") for s in steps]
    names = [s.get("name") for s in steps]
    validate = ids.index("validate-venv")
    install = names.index("Install dependencies")
    assert ids.index("cache-venv") < validate < install
    assert "validate_restored_venv.py" in steps[validate]["run"]
    cond = steps[install]["if"]
    assert "steps.cache-venv.outputs.cache-hit != 'true'" in cond
    # `== 'false'`, not `!= 'true'`: a skipped validate step (lookup-only)
    # leaves the output empty and must not trigger an install.
    assert "steps.validate-venv.outputs.usable == 'false'" in cond
    assert "inputs.lookup-only != 'true'" in steps[validate]["if"]
