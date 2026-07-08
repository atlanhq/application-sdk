"""Tests for .github/scripts/regenerate_contract.py.

Covers the driver's conditional logic — the part that would otherwise be
inlined shell in the regenerate-contract composite action:

  * no contract/app.pkl                 -> self-skip, exit 0, no pkl
  * app-level, eval OK                   -> regenerate in place, no resolve
  * app-level, eval fails                -> warn, exit 0, artifacts untouched
  * app-level, committed artifacts stale -> warn-only drift annotation
  * app-level, committed artifacts fresh -> no drift annotation
  * SDK-level                            -> toolkit overridden + lock resolved,
                                            drift NOT checked
  * SDK-level, eval fails                -> fatal (exit 1)
  * SDK-level, no toolkit entry          -> fatal (SystemExit)

`pkl` and `uvx` are stubbed; `git` runs for real against a throwaway repo in
tmp_path so the drift comparison is exercised end to end.
"""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import regenerate_contract as mod

STALE_MANIFEST = '{"app_name": "{{app_name}}"}\n'
FRESH_MANIFEST = '{"app_name": "metabase"}\n'

REMOTE_PKLPROJECT = """\
amends "pkl:Project"

dependencies {
  ["app-contract-toolkit"] {
    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.16.0"
  }
}
"""


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway connector repo: a contract, a committed lock, and a committed
    (stale) manifest — the state of a PR before regeneration."""
    contract = tmp_path / "contract"
    contract.mkdir()
    (contract / "app.pkl").write_text('amends "@app-contract-toolkit/App.pkl"\n')
    (contract / "PklProject").write_text(REMOTE_PKLPROJECT)
    (contract / "PklProject.deps.json").write_text('{"resolved": "0.16.0"}\n')
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
    fresh_manifest: str = FRESH_MANIFEST,
    calls: list | None = None,
    emit_output: bool = True,
):
    """Build a `run` replacement: simulate pkl/uvx, pass git through to real
    subprocess so the drift comparison is genuinely exercised. Records pkl
    invocations into `calls` when provided."""
    real_run = subprocess.run

    def fake_run(cmd, *, check=False):
        if calls is not None and cmd and cmd[0] == "pkl":
            calls.append(cmd)
        prog = cmd[0]
        if prog == "pkl" and cmd[1:3] == ["project", "resolve"]:
            return types.SimpleNamespace(returncode=0)
        if prog == "pkl" and cmd[1] == "eval":
            if eval_rc == 0 and emit_output:
                out_dir = Path(cmd[cmd.index("-m") + 1])
                gen = out_dir / "app" / "generated"
                gen.mkdir(parents=True, exist_ok=True)
                (gen / "manifest.json").write_text(fresh_manifest)
                (gen / "_input.py").write_text("import os\n")
            return types.SimpleNamespace(returncode=eval_rc)
        if prog in ("uvx", "ruff"):  # formatter — no-op in tests
            return types.SimpleNamespace(returncode=0)
        # Everything else (git) runs for real.
        return real_run(cmd, check=check, text=True, capture_output=True)

    return fake_run


def test_missing_app_pkl_self_skips(repo, monkeypatch):
    (repo / "contract" / "app.pkl").unlink()
    calls: list = []
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, calls=calls))

    assert mod.main([]) == 0

    assert calls == []  # no pkl invoked
    assert (repo / "app" / "generated" / "manifest.json").read_text() == STALE_MANIFEST


def test_app_level_regenerates_in_place_without_resolve(repo, monkeypatch):
    calls: list = []
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, calls=calls))

    assert mod.main([]) == 0

    # Fresh manifest written in place; no `pkl project resolve` in app-level mode.
    assert (repo / "app" / "generated" / "manifest.json").read_text() == FRESH_MANIFEST
    assert not any(c[1:3] == ["project", "resolve"] for c in calls)
    assert any(c[1] == "eval" for c in calls)


def test_app_level_eval_failure_warns_not_fatal(repo, monkeypatch, capsys):
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, eval_rc=1))

    assert mod.main([]) == 0  # never fatal in app-level mode

    # Committed manifest left untouched (no half-regenerated state).
    assert (repo / "app" / "generated" / "manifest.json").read_text() == STALE_MANIFEST
    assert "::warning::pkl eval failed" in capsys.readouterr().out


def test_app_level_drift_warns_when_stale(repo, monkeypatch, capsys):
    # eval produces output different from the committed manifest -> drift.
    monkeypatch.setattr(mod, "run", _make_fake_run(repo))

    assert mod.main(["--check-drift", "true"]) == 0

    out = capsys.readouterr().out
    assert "::warning::Committed contract artifacts are stale" in out
    assert "app/generated" in out


def test_app_level_no_drift_when_fresh(repo, monkeypatch, capsys):
    # eval reproduces exactly the committed manifest -> no drift warning.
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, fresh_manifest=STALE_MANIFEST))

    assert mod.main(["--check-drift", "true"]) == 0

    out = capsys.readouterr().out
    assert "::warning::Committed contract artifacts are stale" not in out
    assert "up to date" in out


def test_sdk_level_overrides_toolkit_and_resolves(repo, monkeypatch, tmp_path, capsys):
    toolkit = tmp_path / "sdk" / "contract-toolkit" / "src"
    toolkit.mkdir(parents=True)
    (toolkit / "PklProject").write_text('amends "pkl:Project"\n')
    calls: list = []
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, calls=calls))

    assert mod.main(["--sdk-toolkit-src", str(toolkit)]) == 0

    # PklProject now points the dependency at the local toolkit checkout.
    pkl_project = (repo / "contract" / "PklProject").read_text()
    assert f'import("{toolkit.resolve()}/PklProject")' in pkl_project
    assert "package://" not in pkl_project
    # Lock re-resolved (override changed the dependency) and eval ran.
    assert any(c[1:3] == ["project", "resolve"] for c in calls)
    assert any(c[1] == "eval" for c in calls)
    # Drift is expected with the PR toolkit, so it is NOT checked.
    assert (
        "::warning::Committed contract artifacts are stale"
        not in capsys.readouterr().out
    )


def test_sdk_level_eval_failure_is_fatal(repo, monkeypatch, tmp_path, capsys):
    toolkit = tmp_path / "sdk" / "contract-toolkit" / "src"
    toolkit.mkdir(parents=True)
    (toolkit / "PklProject").write_text('amends "pkl:Project"\n')
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, eval_rc=1))

    assert mod.main(["--sdk-toolkit-src", str(toolkit)]) == 1
    assert "::error::pkl eval failed" in capsys.readouterr().out


def test_sdk_level_no_toolkit_entry_is_fatal(repo, monkeypatch, tmp_path):
    (repo / "contract" / "PklProject").write_text('amends "pkl:Project"\n')
    toolkit = tmp_path / "sdk" / "contract-toolkit" / "src"
    toolkit.mkdir(parents=True)
    monkeypatch.setattr(mod, "run", _make_fake_run(repo))

    with pytest.raises(SystemExit):
        mod.main(["--sdk-toolkit-src", str(toolkit)])


def test_stale_residue_removed_on_regeneration(repo, monkeypatch):
    """Files the new contract/toolkit no longer emits must not linger: a
    removed entrypoint's manifest would otherwise be baked into the image and
    served as if current (mirrors regenerate-all.sh's rm-before-eval)."""
    residue = repo / "app" / "generated" / "old-ep" / "manifest.json"
    residue.parent.mkdir()
    residue.write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "residue")
    monkeypatch.setattr(mod, "run", _make_fake_run(repo))

    assert mod.main([]) == 0

    assert not residue.exists()
    assert (repo / "app" / "generated" / "manifest.json").read_text() == FRESH_MANIFEST


def test_app_level_empty_eval_output_restores(repo, monkeypatch, capsys):
    """Eval 'succeeding' without emitting app/generated must restore the
    committed artifacts — no manifest at all is strictly worse than stale."""
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, emit_output=False))

    assert mod.main([]) == 0

    assert (repo / "app" / "generated" / "manifest.json").read_text() == STALE_MANIFEST
    assert "::warning::pkl eval emitted no app/generated" in capsys.readouterr().out


def test_sdk_level_empty_eval_output_is_fatal(repo, monkeypatch, tmp_path, capsys):
    toolkit = tmp_path / "sdk" / "contract-toolkit" / "src"
    toolkit.mkdir(parents=True)
    (toolkit / "PklProject").write_text('amends "pkl:Project"\n')
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, emit_output=False))

    assert mod.main(["--sdk-toolkit-src", str(toolkit)]) == 1
    assert "::error::pkl eval" in capsys.readouterr().out


def test_override_toolkit_rewrites_block_form(tmp_path):
    pkl_project = tmp_path / "PklProject"
    pkl_project.write_text(REMOTE_PKLPROJECT)
    toolkit = tmp_path / "toolkit-src"
    toolkit.mkdir()

    mod.override_toolkit(str(tmp_path), str(toolkit))

    text = pkl_project.read_text()
    assert (
        f'["app-contract-toolkit"] = import("{toolkit.resolve()}/PklProject")' in text
    )
    assert "uri =" not in text


def test_resolve_failure_is_fatal(repo, monkeypatch, tmp_path):
    toolkit = tmp_path / "sdk" / "contract-toolkit" / "src"
    toolkit.mkdir(parents=True)
    (toolkit / "PklProject").write_text('amends "pkl:Project"\n')

    def fake_run(cmd, *, check=False):
        if cmd[0] == "pkl" and cmd[1:3] == ["project", "resolve"]:
            if check:
                raise subprocess.CalledProcessError(1, cmd)
            return types.SimpleNamespace(returncode=1)
        return subprocess.run(cmd, check=check, text=True, capture_output=True)

    monkeypatch.setattr(mod, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        mod.main(["--sdk-toolkit-src", str(toolkit)])


def test_format_generated_covers_all_py(tmp_path, monkeypatch):
    """`_format_generated` must ruff-format every generated *.py (not just
    _input.py) so none lands unformatted and trips the consumer's pre-commit."""
    gen = tmp_path / "app" / "generated"
    nested = gen / "crawler"
    nested.mkdir(parents=True)
    (gen / "_input.py").write_text("import os\n")
    (gen / "_e2e_base.py").write_text("import os\n")
    (nested / "_e2e_substitutions.py").write_text("import os\n")

    formatted: list[str] = []

    def spy_run(cmd, *, check=False):
        if cmd[:2] == ["uvx", "ruff"] and cmd[2] == "format":
            formatted.extend(cmd[3:])
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod.shutil, "which", lambda name: "/usr/bin/uvx")
    monkeypatch.setattr(mod, "run", spy_run)
    mod._format_generated(tmp_path)

    assert {Path(p).name for p in formatted} == {
        "_input.py",
        "_e2e_base.py",
        "_e2e_substitutions.py",
    }
