"""Tests for .github/scripts/renovate_pkl_sync.py.

Covers the driver's conditional logic — the part that used to be inlined shell
in renovate-pkl-sync.yaml:

  * --regenerate false  -> lock-only, generated artifacts untouched
  * --regenerate true, eval OK   -> artifacts regenerated + committed
  * --regenerate true, eval fails -> degrade to lock-only, artifacts untouched
  * no contract/app.pkl -> skip regeneration (re-resolve only)
  * nothing changed     -> no commit

`pkl` and `uvx` are stubbed; `git` runs for real against a throwaway repo in
tmp_path, so the staging/commit decisions are exercised end to end.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import renovate_pkl_sync as mod

STALE_MANIFEST = '{"app_name": "{app_name}"}\n'
FRESH_MANIFEST = '{"app_name": "metabase"}\n'


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _commit_count(repo: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--count", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return int(out.stdout.strip())


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway repo with a contract, a committed (stale) lock, and stale
    generated artifacts — the state of a Renovate branch before sync."""
    (tmp_path / "contract").mkdir()
    (tmp_path / "contract" / "app.pkl").write_text(
        'amends "@app-contract-toolkit/App.pkl"\n'
    )
    (tmp_path / "contract" / "PklProject.deps.json").write_text(
        '{"resolved": "0.14.1"}\n'
    )
    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    (gen / "manifest.json").write_text(STALE_MANIFEST)
    (gen / "_input.py").write_text("import os\n")

    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "init")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _make_fake_run(
    repo: Path,
    *,
    eval_rc: int = 0,
    resolve_changes_lock: bool = True,
    emit_generated: bool = True,
):
    """Build a `run` replacement: simulate pkl/uvx, pass git through to real
    subprocess so commit/staging decisions are genuinely exercised.

    emit_generated=False simulates a partial eval (rc=0) that writes only
    atlan.yaml and no app/generated/ dir."""
    real_run = subprocess.run

    def fake_run(cmd, *, check=False):
        prog = cmd[0]
        if prog == "pkl" and cmd[1:3] == ["project", "resolve"]:
            if resolve_changes_lock:
                (repo / "contract" / "PklProject.deps.json").write_text(
                    '{"resolved": "0.14.2"}\n'
                )
            return types.SimpleNamespace(returncode=0)
        if prog == "pkl" and cmd[1] == "eval":
            if eval_rc == 0:
                out_dir = Path(cmd[cmd.index("-m") + 1])
                if emit_generated:
                    gen = out_dir / "app" / "generated"
                    gen.mkdir(parents=True, exist_ok=True)
                    (gen / "manifest.json").write_text(FRESH_MANIFEST)
                    (gen / "_input.py").write_text("import os\n")
                (out_dir / "atlan.yaml").write_text("deploy: true\n")
            return types.SimpleNamespace(returncode=eval_rc)
        if prog == "uvx":  # ruff — no-op in tests
            return types.SimpleNamespace(returncode=0)
        # Everything else (git) runs for real.
        return real_run(cmd, check=check, text=True, capture_output=True)

    return fake_run


def test_regenerate_false_is_lock_only(repo, monkeypatch):
    monkeypatch.setattr(mod, "run", _make_fake_run(repo))
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "false"]) == 0

    # Lock was re-resolved and committed; generated artifacts untouched.
    assert "0.14.2" in (repo / "contract" / "PklProject.deps.json").read_text()
    assert (repo / "app" / "generated" / "manifest.json").read_text() == STALE_MANIFEST
    assert _commit_count(repo) == before + 1
    assert (
        mod.COMMIT_MESSAGE_LOCK_ONLY
        in subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
    )


def test_regenerate_true_success_regenerates_and_commits(repo, monkeypatch):
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, eval_rc=0))
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "true"]) == 0

    assert (repo / "app" / "generated" / "manifest.json").read_text() == FRESH_MANIFEST
    assert (repo / "atlan.yaml").exists()
    assert _commit_count(repo) == before + 1
    assert (
        mod.COMMIT_MESSAGE_REGEN
        in subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
    )


def test_default_regenerates(repo, monkeypatch):
    # No --regenerate flag -> regeneration is the default (opt-out model).
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, eval_rc=0))
    before = _commit_count(repo)

    assert mod.main([]) == 0

    assert (repo / "app" / "generated" / "manifest.json").read_text() == FRESH_MANIFEST
    assert _commit_count(repo) == before + 1
    assert (
        mod.COMMIT_MESSAGE_REGEN
        in subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
    )


def test_regenerate_eval_failure_falls_back_to_lock_only(repo, monkeypatch):
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, eval_rc=1))
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "true"]) == 0

    # Eval failed: generated artifacts must be left untouched (no half state),
    # but the re-resolved lock still commits.
    assert (repo / "app" / "generated" / "manifest.json").read_text() == STALE_MANIFEST
    assert not (repo / "atlan.yaml").exists()
    assert "0.14.2" in (repo / "contract" / "PklProject.deps.json").read_text()
    assert _commit_count(repo) == before + 1
    assert (
        mod.COMMIT_MESSAGE_LOCK_ONLY
        in subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
    )


def test_regenerate_eval_emits_no_generated_dir(repo, monkeypatch):
    """Partial eval (rc=0, only atlan.yaml, no app/generated/): the existing
    committed app/generated is left untouched (not deleted), the emitted
    atlan.yaml is swapped in, and the commit uses the regen message."""
    monkeypatch.setattr(
        mod, "run", _make_fake_run(repo, eval_rc=0, emit_generated=False)
    )
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "true"]) == 0

    # Existing generated preserved (not destroyed because eval emitted none).
    assert (repo / "app" / "generated" / "manifest.json").read_text() == STALE_MANIFEST
    # The artifact eval did emit is swapped in and committed.
    assert (repo / "atlan.yaml").read_text() == "deploy: true\n"
    assert _commit_count(repo) == before + 1
    assert (
        mod.COMMIT_MESSAGE_REGEN
        in subprocess.run(
            ["git", "log", "-1", "--pretty=%s"],
            cwd=repo,
            capture_output=True,
            text=True,
        ).stdout
    )


def test_missing_app_pkl_skips_regeneration(repo, monkeypatch):
    (repo / "contract" / "app.pkl").unlink()
    monkeypatch.setattr(mod, "run", _make_fake_run(repo))
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "true"]) == 0

    assert (repo / "app" / "generated" / "manifest.json").read_text() == STALE_MANIFEST
    assert _commit_count(repo) == before + 1  # lock-only commit


def test_no_changes_makes_no_commit(repo, monkeypatch):
    # Resolve produces no lock change and regeneration is off -> nothing to do.
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, resolve_changes_lock=False))
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "false"]) == 0

    assert _commit_count(repo) == before  # no new commit


def test_format_generated_covers_all_py_not_just_input(tmp_path, monkeypatch):
    """`_format_generated` must ruff-format every generated *.py, not only
    _input.py. The contract emits _e2e_*.py too; if those are left unformatted
    the consumer's pre-commit reformats them on the renovate PR and fails CI
    (the bug this guards against). `uvx` is stubbed in the other tests as a
    no-op, so without this assertion the _input.py-only regression is invisible.
    """
    gen = tmp_path / "app" / "generated"
    nested = gen / "crawler"  # bundle layout: rglob must recurse
    nested.mkdir(parents=True)
    (gen / "_input.py").write_text("import os\n")
    (gen / "_e2e_base.py").write_text("import os\n")
    (gen / "_e2e_credential.py").write_text("import os\n")
    (gen / "__init__.py").write_text("")
    (nested / "_e2e_substitutions.py").write_text("import os\n")

    formatted: list[str] = []

    def spy_run(cmd, *, check=False):
        if cmd[:2] == ["uvx", "ruff"] and cmd[2] == "format":
            formatted.extend(cmd[3:])
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod, "run", spy_run)
    mod._format_generated(tmp_path)

    formatted_names = {Path(p).name for p in formatted}
    assert formatted_names == {
        "_input.py",
        "_e2e_base.py",
        "_e2e_credential.py",
        "__init__.py",
        "_e2e_substitutions.py",
    }


def test_resolve_failure_is_fatal(repo, monkeypatch):
    def fake_run(cmd, *, check=False):
        if cmd[0] == "pkl" and cmd[1:3] == ["project", "resolve"]:
            if check:
                raise subprocess.CalledProcessError(1, cmd)
            return types.SimpleNamespace(returncode=1)
        return subprocess.run(cmd, check=check, text=True, capture_output=True)

    monkeypatch.setattr(mod, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        mod.main(["--regenerate", "false"])
