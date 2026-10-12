"""Both regeneration entry points must format generated Python identically.

``renovate_pkl_sync.regenerate`` (which the Generated Artifact Freshness gate
reuses) and ``regenerate_contract.main`` (pre-test / app-side regeneration)
used to run different ruff commands: the gate passed ``--force-exclude`` and
deferred to the app's rule selection, while ``regenerate_contract`` pinned
``--select F401`` and omitted ``--force-exclude``. An app whose ruff config
excludes ``app/generated`` then got raw pkl output from one and reformatted
output from the other, and the gate reported the second as "stale or
hand-edited" (FND-3560).

This drives both entry points end-to-end on the same fixture app with a REAL
ruff (only ``pkl`` is stubbed) and asserts byte-identical output. Real ruff
matters: a stubbed formatter cannot tell whether an exclude was honoured.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import regenerate_contract as regen
import renovate_pkl_sync as sync


def _real_ruff() -> list[str] | None:
    """The ruff both entry points get in this test. Any one will do — parity
    only needs the SAME ruff for both. ``uvx`` is what CI has (setup-uv)."""
    if importlib.util.find_spec("ruff") is not None:
        return [sys.executable, "-m", "ruff"]
    if shutil.which("ruff"):
        return ["ruff"]
    if shutil.which("uvx"):
        return ["uvx", "ruff"]
    return None


REAL_RUFF = _real_ruff()

pytestmark = pytest.mark.skipif(REAL_RUFF is None, reason="needs ruff or uvx")

# Wider than ruff's default 88 columns and carrying an unused import, so both
# `ruff format` and `ruff check --fix` (F401 is in ruff's default rule set)
# change it — unless the app's exclude is honoured.
RAW_INPUT_PY = (
    "import os\n"
    'FIELDS = {"alpha_field_name": 1, "beta_field_name": 2, '
    '"gamma_field_name": 3, "delta_field_name": 4}\n'
)
MANIFEST = '{"app_name": "example-app"}\n'

PKLPROJECT = """\
amends "pkl:Project"

dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.16.0"
  }
}
"""

EXCLUDING_PYPROJECT = '[tool.ruff]\nextend-exclude = ["app/generated"]\n'
PLAIN_PYPROJECT = "[tool.ruff]\n"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _make_repo(root: Path, pyproject: str) -> None:
    contract = root / "contract"
    contract.mkdir()
    (contract / "app.pkl").write_text('amends "@app-contract-toolkit/App.pkl"\n')
    (contract / "PklProject").write_text(PKLPROJECT)
    (contract / "PklProject.deps.json").write_text('{"resolved": "0.16.0"}\n')
    (root / "pyproject.toml").write_text(pyproject)
    gen = root / "app" / "generated"
    gen.mkdir(parents=True)
    (gen / "manifest.json").write_text(MANIFEST)
    (gen / "_input.py").write_text("# committed\n")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def _fake_run(cmd, *, check=False):
    """Stub pkl only. ruff runs for real, the same binary for both entry points
    whichever prefix (``uvx ruff`` / ``ruff``) the script resolved."""
    if cmd[0] == "pkl" and cmd[1:3] == ["project", "resolve"]:
        return types.SimpleNamespace(returncode=0)
    if cmd[0] == "pkl" and cmd[1] == "eval":
        gen = Path(cmd[cmd.index("-m") + 1]) / "app" / "generated"
        gen.mkdir(parents=True, exist_ok=True)
        (gen / "manifest.json").write_text(MANIFEST)
        (gen / "_input.py").write_text(RAW_INPUT_PY)
        return types.SimpleNamespace(returncode=0)
    if cmd[:2] == ["uvx", "ruff"]:
        cmd = [*REAL_RUFF, *cmd[2:]]
    elif cmd[0] == "ruff":
        cmd = [*REAL_RUFF, *cmd[1:]]
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _generated_bytes(root: Path) -> dict[str, bytes]:
    gen = root / "app" / "generated"
    return {
        str(p.relative_to(gen)): p.read_bytes()
        for p in sorted(gen.rglob("*"))
        if p.is_file()
    }


@pytest.fixture(autouse=True)
def _stub_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync, "run", _fake_run)
    monkeypatch.setattr(regen, "run", _fake_run)
    # Resolve the formatter as `uvx` whatever this machine has installed;
    # _fake_run maps it onto REAL_RUFF.
    real_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda name, *a, **kw: "/usr/bin/uvx" if name == "uvx" else real_which(name),
    )


@pytest.mark.parametrize(
    ("pyproject", "expect_raw"),
    [
        pytest.param(EXCLUDING_PYPROJECT, True, id="app-excludes-generated"),
        pytest.param(PLAIN_PYPROJECT, False, id="app-lints-generated"),
    ],
)
def test_both_entry_points_produce_identical_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pyproject: str, expect_raw: bool
) -> None:
    gate_repo = tmp_path / "gate"
    regen_repo = tmp_path / "regen"
    gate_repo.mkdir()
    regen_repo.mkdir()
    _make_repo(gate_repo, pyproject)
    _make_repo(regen_repo, pyproject)

    monkeypatch.chdir(gate_repo)
    assert sync.regenerate("contract") is True
    gate_out = _generated_bytes(gate_repo)

    monkeypatch.chdir(regen_repo)
    assert regen.main(["--check-drift", "false"]) == 0
    regen_out = _generated_bytes(regen_repo)

    assert regen_out == gate_out

    # Pin what "identical" means, so both drifting the same wrong way still fails.
    input_py = gate_out["_input.py"].decode()
    if expect_raw:
        assert input_py == RAW_INPUT_PY
    else:
        assert "import os" not in input_py  # app's own rules applied
        assert input_py != RAW_INPUT_PY
