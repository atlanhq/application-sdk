"""Tests for .github/scripts/check_generated_freshness.py.

Covers the CI freshness gate's decision logic:

  * no contract/app.pkl        -> na (exit 0)
  * --check-freshness false    -> opted out (exit 0), pkl never invoked
  * regenerate matches commit  -> clean (exit 0)
  * regenerate changes a file  -> drift (exit 1)
  * regenerate adds a new file -> drift (exit 1, untracked caught)
  * pkl eval fails             -> na / inconclusive (exit 0, non-blocking)

`pkl` and `uvx` are stubbed (regeneration is reused from renovate_pkl_sync);
`git` runs for real against a throwaway repo in tmp_path so the diff/untracked
detection is exercised end to end.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import check_generated_freshness as mod
import renovate_pkl_sync as sync

COMMITTED_MANIFEST = '{"app_name": "metabase"}\n'
COMMITTED_ATLAN = (
    "# AUTO-GENERATED from contract/app.pkl — DO NOT EDIT MANUALLY.\ndeploy: true\n"
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway app repo with a contract and committed generated artifacts."""
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "app.pkl").write_text(
        'amends "@app-contract-toolkit/App.pkl"\n'
    )
    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    (gen / "manifest.json").write_text(COMMITTED_MANIFEST)
    (tmp_path / "atlan.yaml").write_text(COMMITTED_ATLAN)

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _fake_run(
    *,
    eval_rc: int = 0,
    manifest: str = COMMITTED_MANIFEST,
    atlan: str = COMMITTED_ATLAN,
):
    """Build a `run` replacement: `pkl eval` writes the given artifacts into the
    output dir; `uvx` (ruff) is a no-op; everything else (git) runs for real."""
    real_run = subprocess.run

    def fake_run(cmd, *, check=False):
        prog = cmd[0]
        if prog == "pkl" and cmd[1] == "eval":
            if eval_rc == 0:
                out_dir = Path(cmd[cmd.index("-m") + 1])
                gen = out_dir / "app" / "generated"
                gen.mkdir(parents=True, exist_ok=True)
                (gen / "manifest.json").write_text(manifest)
                (out_dir / "atlan.yaml").write_text(atlan)
            return types.SimpleNamespace(returncode=eval_rc)
        if prog == "uvx":
            return types.SimpleNamespace(returncode=0)
        return real_run(cmd, check=check, text=True, capture_output=True)

    return fake_run


def test_clean_when_regeneration_matches(repo, monkeypatch):
    monkeypatch.setattr(sync, "run", _fake_run())
    assert mod.main([]) == 0


def test_drift_when_regeneration_changes_a_file(repo, monkeypatch):
    monkeypatch.setattr(sync, "run", _fake_run(manifest='{"app_name": "CHANGED"}\n'))
    assert mod.main([]) == 1


def test_drift_when_regeneration_adds_untracked_file(repo, monkeypatch):
    # Committed tree has no _input.py; a fresh eval that emits one is drift.
    real = _fake_run()

    def fake_run(cmd, *, check=False):
        if cmd[0] == "pkl" and cmd[1] == "eval":
            out_dir = Path(cmd[cmd.index("-m") + 1])
            gen = out_dir / "app" / "generated"
            gen.mkdir(parents=True, exist_ok=True)
            (gen / "manifest.json").write_text(COMMITTED_MANIFEST)
            (gen / "_input.py").write_text("x = 1\n")  # new file
            (out_dir / "atlan.yaml").write_text(COMMITTED_ATLAN)
            return types.SimpleNamespace(returncode=0)
        return real(cmd, check=check)

    monkeypatch.setattr(sync, "run", fake_run)
    assert mod.main([]) == 1


def test_opt_out_skips_check(repo, monkeypatch):
    called = {"pkl": False}

    def fake_run(cmd, *, check=False):
        if cmd[0] == "pkl":
            called["pkl"] = True
        return subprocess.run(cmd, check=check, text=True, capture_output=True)

    monkeypatch.setattr(sync, "run", fake_run)
    assert mod.main(["--check-freshness", "false"]) == 0
    assert not called["pkl"], "pkl must not run when opted out"


def test_na_when_no_contract(tmp_path, monkeypatch):
    _git(tmp_path, "init", "-q")
    monkeypatch.chdir(tmp_path)
    assert mod.main([]) == 0


def test_na_when_eval_fails(repo, monkeypatch):
    monkeypatch.setattr(sync, "run", _fake_run(eval_rc=1))
    # eval failure is inconclusive, not a drift failure — non-blocking.
    assert mod.main([]) == 0
