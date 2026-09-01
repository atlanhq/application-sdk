"""Tests for .github/scripts/renovate_pkl_sync.py.

Covers the driver's conditional logic — the part that used to be inlined shell
in renovate-pkl-sync.yaml:

  * --regenerate false  -> lock-only, generated artifacts untouched
  * --regenerate true, eval OK   -> artifacts regenerated + committed
  * --regenerate true, eval fails -> degrade to lock-only, artifacts untouched
  * no contract/app.pkl -> skip regeneration (re-resolve only)
  * no contract/PklProject -> whole run is a safe no-op (no pkl, no commit)
  * --no-commit         -> regenerate in-tree but leave staging/commit to caller
  * nothing changed     -> no commit

`pkl` and `uvx` are stubbed; `git` runs for real against a throwaway repo in
tmp_path, so the staging/commit decisions are exercised end to end.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import renovate_pkl_sync as mod

STALE_MANIFEST = '{"app_name": "{app_name}"}\n'
FRESH_MANIFEST = '{"app_name": "metabase"}\n'

# A connector config in two versions: what the toolkit emits, and what an app
# with a post-generate step installs over it (a construct the toolkit cannot yet
# express). Used to prove the post-generate hook survives the swap.
TOOLKIT_CONNECTOR_CONFIG = '{"credential": "toolkit-default"}\n'
CANONICAL_CONNECTOR_CONFIG = '{"credential": "hand-maintained"}\n'


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
    (tmp_path / "contract" / "PklProject").write_text(
        'amends "pkl:Project"\n'
        "dependencies {\n"
        '  ["app-contract-toolkit"] {\n'
        '    uri = "package://atlanhq.github.io/application-sdk/contracts/app-contract-toolkit@0.14.1"\n'
        "  }\n"
        "}\n"
    )
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


def _write_eval_output(
    out_dir: Path,
    layout: str,
    manifest: str = FRESH_MANIFEST,
    connector: str = TOOLKIT_CONNECTOR_CONFIG,
) -> None:
    """Write what `pkl eval -m out_dir` emits for each contract family.

    The families differ in whether output keys carry the `app/generated/` prefix
    — see pkl_contract_layout.py. Reproducing both here is the point: the driver
    used to recognise only `prefixed` and silently no-op'd on everything else.
    """
    if layout == "prefixed":  # App.pkl
        gen = out_dir / "app" / "generated"
        gen.mkdir(parents=True, exist_ok=True)
        (gen / "manifest.json").write_text(manifest)
        (gen / "_input.py").write_text("import os\n")
    elif layout == "native":  # NativeApp.pkl — bare keys
        (out_dir / "manifest.json").write_text(manifest)
        (out_dir / "_input.py").write_text("import os\n")
    elif layout == "native-bundle":  # NativeAppBundle.pkl — <entrypoint>/<file>
        crawler = out_dir / "crawler"
        crawler.mkdir(parents=True, exist_ok=True)
        (crawler / "manifest.json").write_text(manifest)
        (crawler / "_input.py").write_text("import os\n")
        (out_dir / "atlan-connectors-x.json").write_text(connector)
    elif layout != "root-only":
        raise AssertionError(f"unknown layout {layout!r}")
    if layout != "empty":
        (out_dir / "atlan.yaml").write_text("deploy: true\n")


def _make_fake_run(
    repo: Path,
    *,
    eval_rc: int = 0,
    resolve_changes_lock: bool = True,
    layout: str = "prefixed",
):
    """Build a `run` replacement: simulate pkl/uvx, pass git through to real
    subprocess so commit/staging decisions are genuinely exercised.

    `layout` selects which contract family's output shape eval produces;
    "root-only" simulates a partial eval (rc=0) that writes only atlan.yaml."""
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
                # The driver evaluates twice: the app's contract (bumped pin) and
                # the same contract exported at the pre-bump pin, for override
                # detection. Distinguish by --project-dir, and give the baseline
                # the OLD manifest so a real refresh is visible.
                project_dir = cmd[cmd.index("--project-dir") + 1]
                is_baseline = Path(project_dir).is_absolute()
                _write_eval_output(
                    Path(cmd[cmd.index("-m") + 1]),
                    layout,
                    manifest=STALE_MANIFEST if is_baseline else FRESH_MANIFEST,
                    connector=TOOLKIT_CONNECTOR_CONFIG,
                )
            return types.SimpleNamespace(returncode=eval_rc)
        if prog == "uvx":  # ruff — no-op in tests
            return types.SimpleNamespace(returncode=0)
        # Everything else (git) runs for real.
        return real_run(cmd, check=check, text=True, capture_output=True)

    return fake_run


def _reshape_generated(repo: Path, files: dict[str, str]) -> None:
    """Replace the fixture's committed app/generated with `files` (relative path
    -> content) and commit, so a test can start from a native-family tree."""
    shutil.rmtree(repo / "app" / "generated")
    for rel, content in files.items():
        path = repo / "app" / "generated" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "reshape generated")


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


def test_regenerate_retries_transient_eval_failure(repo, monkeypatch):
    """A transient `pkl eval` failure (e.g. a cold-runner package fetch) is
    retried; a subsequent success yields a full regeneration + REGEN commit,
    not a lock-only degrade. (Retry backoff is neutralised by the conftest.)"""
    real_run = subprocess.run
    calls = {"eval": 0}

    def fake_run(cmd, *, check=False):
        prog = cmd[0]
        if prog == "pkl" and cmd[1:3] == ["project", "resolve"]:
            (repo / "contract" / "PklProject.deps.json").write_text(
                '{"resolved": "0.14.2"}\n'
            )
            return types.SimpleNamespace(returncode=0)
        if prog == "pkl" and cmd[1] == "eval":
            calls["eval"] += 1
            if calls["eval"] == 1:
                return types.SimpleNamespace(returncode=1)  # transient failure
            out_dir = Path(cmd[cmd.index("-m") + 1])
            gen = out_dir / "app" / "generated"
            gen.mkdir(parents=True, exist_ok=True)
            (gen / "manifest.json").write_text(FRESH_MANIFEST)
            (gen / "_input.py").write_text("import os\n")
            (out_dir / "atlan.yaml").write_text("deploy: true\n")
            return types.SimpleNamespace(returncode=0)
        if prog == "uvx":
            return types.SimpleNamespace(returncode=0)
        return real_run(cmd, check=check, text=True, capture_output=True)

    monkeypatch.setattr(mod, "run", fake_run)
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "true"]) == 0

    assert calls["eval"] == 2, "eval must be retried after the transient failure"
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


def _last_commit_subject(repo: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=repo,
        capture_output=True,
        text=True,
    ).stdout


def test_regenerate_root_only_output_degrades_to_lock_only(repo, monkeypatch, capsys):
    """Partial eval (rc=0, only atlan.yaml, no generated artifacts at all): the
    committed generated dir is left untouched (not deleted), the emitted
    atlan.yaml is still swapped in, and — because no generated artifact was
    placed — the commit degrades to the lock-only message and says why.

    This used to be the *only* non-prefixed case the driver handled, and it
    handled it by silently claiming success; the whole native family fell in
    here. See test_native_* below."""
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="root-only"))
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "true"]) == 0

    # Existing generated preserved (not destroyed because eval emitted none).
    assert (repo / "app" / "generated" / "manifest.json").read_text() == STALE_MANIFEST
    # The artifact eval did emit is swapped in and committed.
    assert (repo / "atlan.yaml").read_text() == "deploy: true\n"
    assert _commit_count(repo) == before + 1
    assert mod.COMMIT_MESSAGE_LOCK_ONLY in _last_commit_subject(repo)
    assert (
        "::warning::pkl eval produced no generated contract artifacts"
        in capsys.readouterr().out
    )


def test_native_bundle_layout_regenerates_and_commits(repo, monkeypatch):
    """NativeAppBundle.pkl (hive's family): output keys are `<entrypoint>/<file>`
    plus a root atlan.yaml, with NO `app/generated/` prefix. These must land under
    app/generated/ and be committed.

    Regression guard for the bug that shipped a toolkit bump with stale artifacts
    and a green check: the driver saw no `app/generated` in the eval output and
    silently regenerated nothing."""
    _reshape_generated(
        repo,
        {
            "crawler/manifest.json": STALE_MANIFEST,
            "crawler/_input.py": "import os\n",
            "atlan-connectors-x.json": TOOLKIT_CONNECTOR_CONFIG,
            "__init__.py": "",
        },
    )
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="native-bundle"))
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "true"]) == 0

    gen = repo / "app" / "generated"
    assert (gen / "crawler" / "manifest.json").read_text() == FRESH_MANIFEST
    assert (repo / "atlan.yaml").read_text() == "deploy: true\n"
    # app-owned file this eval does not emit survives the swap.
    assert (gen / "__init__.py").exists()
    assert _commit_count(repo) == before + 1
    assert mod.COMMIT_MESSAGE_REGEN in _last_commit_subject(repo)


def test_native_layout_regenerates(repo, monkeypatch):
    """NativeApp.pkl: bare output keys (`manifest.json`, `_input.py`) relative to
    the generated dir."""
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="native"))

    assert mod.main(["--regenerate", "true"]) == 0

    assert (repo / "app" / "generated" / "manifest.json").read_text() == FRESH_MANIFEST
    assert mod.COMMIT_MESSAGE_REGEN in _last_commit_subject(repo)


def test_native_swap_refused_when_generated_dir_does_not_match(
    repo, monkeypatch, capsys
):
    """A multi-variant app generating into `app/generated/<variant>` emits the
    same bare keys, but placing them at `app/generated/` would create a set of
    wrong files beside the real ones. Nothing overlaps, so the swap must refuse
    loudly and degrade to lock-only rather than write to the wrong base."""
    _reshape_generated(
        repo,
        {
            "apache/manifest.json": STALE_MANIFEST,
            "confluent/manifest.json": STALE_MANIFEST,
        },
    )
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="native"))

    assert mod.main(["--regenerate", "true"]) == 0

    gen = repo / "app" / "generated"
    assert (gen / "apache" / "manifest.json").read_text() == STALE_MANIFEST
    assert not (gen / "manifest.json").exists()  # nothing written to the wrong base
    assert mod.COMMIT_MESSAGE_LOCK_ONLY in _last_commit_subject(repo)
    out = capsys.readouterr().out
    assert "::warning::pkl eval emitted unprefixed contract artifacts" in out


def test_prefixed_swap_clears_orphans(repo, monkeypatch):
    """App.pkl owns the whole generated dir, so a wholesale replace must drop a
    file the contract no longer emits."""
    (repo / "app" / "generated" / "removed-entrypoint.json").write_text("{}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "orphan")
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="prefixed"))

    assert mod.main(["--regenerate", "true"]) == 0

    assert not (repo / "app" / "generated" / "removed-entrypoint.json").exists()


def test_post_generate_script_runs_after_swap(repo, monkeypatch):
    """An app whose generate task installs a hand-maintained artifact over the
    toolkit output ships contract/post-generate.sh; without it the swap reverts
    the override and commits the toolkit's unusable version."""
    _reshape_generated(
        repo,
        {
            "crawler/manifest.json": STALE_MANIFEST,
            "atlan-connectors-x.json": CANONICAL_CONNECTOR_CONFIG,
        },
    )
    # Mirrors a real post-generate step: copy a hand-maintained file over the
    # toolkit's output.
    (repo / "canonical.json").write_text(CANONICAL_CONNECTOR_CONFIG)
    (repo / "contract" / "post-generate.sh").write_text(
        "cp canonical.json app/generated/atlan-connectors-x.json\n"
    )
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="native-bundle"))

    assert mod.main(["--regenerate", "true"]) == 0

    gen = repo / "app" / "generated"
    # Manifest regenerated by the toolkit; connector config kept by the script.
    assert (gen / "crawler" / "manifest.json").read_text() == FRESH_MANIFEST
    assert (gen / "atlan-connectors-x.json").read_text() == CANONICAL_CONNECTOR_CONFIG


def test_post_generate_failure_warns_but_still_commits(repo, monkeypatch, capsys):
    """The script runs after the swap, so a failure must not fail the sync —
    fresh toolkit output in a visible diff beats failing a dependency bump."""
    (repo / "contract" / "post-generate.sh").write_text("exit 3\n")
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="native"))

    assert mod.main(["--regenerate", "true"]) == 0

    assert (repo / "app" / "generated" / "manifest.json").read_text() == FRESH_MANIFEST
    assert mod.COMMIT_MESSAGE_REGEN in _last_commit_subject(repo)
    assert "post-generate.sh failed" in capsys.readouterr().out


def _bump_pin(repo: Path) -> None:
    """Commit a toolkit-pin bump, the way Renovate does on its branch. Gives
    baseline_contract_ref a parent commit holding the pre-bump pin."""
    pkl_project = repo / "contract" / "PklProject"
    pkl_project.write_text(pkl_project.read_text().replace("0.14.1", "0.14.2"))
    _git(repo, "commit", "-qam", "chore(deps): bump app-contract-toolkit")


def test_app_overridden_file_survives_a_bump_with_no_app_level_config(
    repo, monkeypatch, capsys
):
    """Centralized override protection, end to end: no post-generate.sh, no
    workflow input, nothing app-side.

    The committed connector config is hand-maintained (differs from what the
    pre-bump toolkit emitted), so it must be preserved; the manifest matches the
    pre-bump output exactly, so it must be refreshed. This is what stops a
    now-working swap from reverting six apps' post-processing."""
    _reshape_generated(
        repo,
        {
            "crawler/manifest.json": STALE_MANIFEST,
            "atlan-connectors-x.json": CANONICAL_CONNECTOR_CONFIG,
        },
    )
    _bump_pin(repo)
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="native-bundle"))

    assert mod.main(["--regenerate", "true"]) == 0

    gen = repo / "app" / "generated"
    assert (gen / "crawler" / "manifest.json").read_text() == FRESH_MANIFEST
    assert (gen / "atlan-connectors-x.json").read_text() == CANONICAL_CONNECTOR_CONFIG
    out = capsys.readouterr().out
    assert "Preserved app-maintained generated file(s)" in out
    assert "atlan-connectors-x.json" in out


def test_no_baseline_without_a_pin_change_so_drift_stays_visible(repo, monkeypatch):
    """With no bump in flight (an ordinary PR, or the freshness gate) there is no
    baseline, so everything is overwritten and genuine staleness still shows up.
    Protecting files here would freeze the tree and blind the gate."""
    _reshape_generated(
        repo,
        {
            "crawler/manifest.json": STALE_MANIFEST,
            "atlan-connectors-x.json": CANONICAL_CONNECTOR_CONFIG,
        },
    )
    # No _bump_pin() — contract/PklProject is untouched.
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="native-bundle"))

    assert mod.main(["--regenerate", "true"]) == 0

    gen = repo / "app" / "generated"
    assert (gen / "crawler" / "manifest.json").read_text() == FRESH_MANIFEST
    assert (gen / "atlan-connectors-x.json").read_text() == TOOLKIT_CONNECTOR_CONFIG


def test_no_post_generate_script_is_a_noop(repo, monkeypatch, capsys):
    """Almost every app ships no such script; regeneration must not mention it."""
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, layout="native"))

    assert mod.main(["--regenerate", "true"]) == 0

    assert "post-generate" not in capsys.readouterr().out


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


def test_no_commit_regenerates_but_does_not_commit(repo, monkeypatch):
    """--no-commit: artifacts are re-resolved/regenerated in the working tree
    but the driver makes NO git commit — Renovate's postUpgradeTasks stages the
    fileFilters matches into the branch itself."""
    monkeypatch.setattr(mod, "run", _make_fake_run(repo, eval_rc=0))
    before = _commit_count(repo)

    assert mod.main(["--regenerate", "true", "--no-commit"]) == 0

    # Working tree WAS updated ...
    assert (repo / "app" / "generated" / "manifest.json").read_text() == FRESH_MANIFEST
    assert (repo / "atlan.yaml").exists()
    assert "0.14.2" in (repo / "contract" / "PklProject.deps.json").read_text()
    # ... but nothing was committed (the changes sit unstaged for the caller).
    assert _commit_count(repo) == before


def test_missing_pkl_project_is_noop(repo, monkeypatch):
    """A repo/branch with no contract/PklProject is a safe no-op: the driver
    returns 0 without invoking `pkl project resolve` (which would be fatal) and
    without committing."""
    (repo / "contract" / "PklProject").unlink()
    before = _commit_count(repo)

    def fail_if_called(cmd, *, check=False):
        if cmd[0] == "pkl":
            pytest.fail(f"pkl must not run when PklProject is absent: {cmd}")
        return subprocess.run(cmd, check=check, text=True, capture_output=True)

    monkeypatch.setattr(mod, "run", fail_if_called)

    assert mod.main(["--regenerate", "true"]) == 0
    assert _commit_count(repo) == before


def test_format_generated_covers_all_py_not_just_input(tmp_path, monkeypatch):
    """`_format_generated` must ruff-format every generated *.py, not only
    _input.py. The contract emits _e2e_*.py too; if those are left unformatted
    the consumer's pre-commit reformats them on the renovate PR and fails CI
    (the bug this guards against). `uvx` is stubbed in the other tests as a
    no-op, so without this assertion the _input.py-only regression is invisible.
    """
    monkeypatch.chdir(tmp_path)
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
            formatted.extend(a for a in cmd[3:] if not a.startswith("-"))
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod, "run", spy_run)
    mod._format_generated()

    formatted_names = {Path(p).name for p in formatted}
    assert formatted_names == {
        "_input.py",
        "_e2e_base.py",
        "_e2e_credential.py",
        "__init__.py",
        "_e2e_substitutions.py",
    }


def test_format_generated_check_defers_to_consumer_ruff_config(tmp_path, monkeypatch):
    """`ruff check --fix` must run with no --select restriction, so ruff
    auto-discovers and applies whatever the *consumer* repo's own
    pyproject.toml configures (e.g. import-sort rules some apps enable and
    others don't) — fleet ruff configs are not uniform, so hardcoding a rule
    subset here (previously just F401) would drift from whatever that repo's
    own pre-commit actually enforces."""
    monkeypatch.chdir(tmp_path)
    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    (gen / "_input.py").write_text("import os\n")

    check_calls: list[list[str]] = []

    def spy_run(cmd, *, check=False):
        if cmd[:2] == ["uvx", "ruff"] and cmd[2] == "check":
            check_calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod, "run", spy_run)
    mod._format_generated()

    assert len(check_calls) == 1
    assert "--select" not in check_calls[0]
    assert "F401" not in check_calls[0]


def test_format_generated_lints_real_path_not_temp_dir(tmp_path, monkeypatch):
    """Regression guard for the temp-dir path-resolution bug: `_format_generated`
    must lint files at their real `app/generated/**` path relative to cwd, not
    an absolute temp-dir path. `per-file-ignores`/`exclude` patterns a consumer
    scopes to `app/generated/**` only match a real relative path — an absolute
    path under a disconnected temp dir silently fails to match, over-applying
    rules the consumer explicitly exempted for generated code."""
    monkeypatch.chdir(tmp_path)
    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    (gen / "_input.py").write_text("import os\n")

    check_calls: list[list[str]] = []

    def spy_run(cmd, *, check=False):
        if cmd[:2] == ["uvx", "ruff"] and cmd[2] == "check":
            check_calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod, "run", spy_run)
    mod._format_generated()

    assert len(check_calls) == 1
    linted_path = check_calls[0][-1]
    assert not Path(linted_path).is_absolute()
    assert linted_path == str(Path("app/generated/_input.py"))


def test_format_generated_passes_force_exclude(tmp_path, monkeypatch):
    """Both ruff invocations must pass `--force-exclude` so a consumer's
    `extend-exclude = ["app/generated"]` is honored even though paths are named
    explicitly (ruff ignores excludes for explicit paths otherwise). Without it,
    this pass reformats an app that deliberately keeps generated output raw,
    which the freshness gate then flags as drift (CNCT-70)."""
    monkeypatch.chdir(tmp_path)
    gen = tmp_path / "app" / "generated"
    gen.mkdir(parents=True)
    (gen / "_input.py").write_text("import os\n")

    calls: list[list[str]] = []

    def spy_run(cmd, *, check=False):
        if cmd[:2] == ["uvx", "ruff"]:
            calls.append(cmd)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(mod, "run", spy_run)
    mod._format_generated()

    subcommands = {cmd[2] for cmd in calls}
    assert subcommands == {"check", "format"}
    for cmd in calls:
        assert "--force-exclude" in cmd, cmd
        assert cmd[-1] == str(Path("app/generated/_input.py"))


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


# ── multi-root contracts: one pkl root per entrypoint ────────────────────────


def _multi_root_contract(repo: Path) -> None:
    """synapse's shape: no app.pkl, one amending root per entrypoint, plus an
    imported (non-root) module."""
    (repo / "contract" / "app.pkl").unlink(missing_ok=True)
    (repo / "contract" / "crawler.pkl").write_text(
        'amends "@app-contract-toolkit/NativeApp.pkl"\nimport "./credentials.pkl"\n'
    )
    (repo / "contract" / "miner.pkl").write_text(
        'amends "@app-contract-toolkit/NativeApp.pkl"\n'
    )
    # imported, never evaluated on its own
    (repo / "contract" / "credentials.pkl").write_text('name = "creds"\n')


def test_eval_roots_finds_amending_roots_not_imported_modules(repo):
    _multi_root_contract(repo)
    assert [r.name for r in mod.eval_roots("contract")] == [
        "crawler.pkl",
        "miner.pkl",
    ]  # credentials.pkl imports nothing and amends nothing — not a root


def test_eval_roots_prefers_app_pkl_alone(repo):
    """The single-root convention wins outright: app.pkl's outputs land at the
    generated dir itself, never in a per-root subdir."""
    (repo / "contract" / "extra.pkl").write_text(
        'amends "@app-contract-toolkit/NativeApp.pkl"\n'
    )
    assert [r.name for r in mod.eval_roots("contract")] == ["app.pkl"]


def test_multi_root_gives_each_root_its_own_eval_base(repo, monkeypatch):
    """Every root emits the SAME unprefixed key names, so a shared `pkl eval
    -m` base lets the last root clobber the others (measured on synapse: 4 of
    5 filenames collide). Each root must get its own base and its own target.
    """
    _multi_root_contract(repo)
    bases: list[str] = []

    def fake_run(cmd, *, check=False):
        if cmd[0] == "pkl" and cmd[1] == "eval":
            out = Path(cmd[cmd.index("-m") + 1])
            bases.append(str(out))
            root = Path(cmd[-1]).stem
            out.mkdir(parents=True, exist_ok=True)
            # identical key names from both roots — the collision under test
            (out / "_input.py").write_text(f"# {root}\n")
            (out / "manifest.json").write_text(f'{{"entrypoint": "{root}"}}\n')
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.regenerate("contract") is True
    assert len(bases) == 2 and len(set(bases)) == 2  # never a shared base

    crawler = repo / "app" / "generated" / "crawler"
    miner = repo / "app" / "generated" / "miner"
    assert "crawler" in (crawler / "_input.py").read_text()
    assert "miner" in (miner / "_input.py").read_text()  # not clobbered


def test_multi_root_never_writes_repo_root_files(repo, monkeypatch):
    """No per-entrypoint root emits atlan.yaml/app.yaml, so anything at the
    repo root is hand-authored and must survive (a prior incident clobbered
    exactly that)."""
    _multi_root_contract(repo)
    (repo / "atlan.yaml").write_text("hand: authored\n")

    def fake_run(cmd, *, check=False):
        if cmd[0] == "pkl" and cmd[1] == "eval":
            out = Path(cmd[cmd.index("-m") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "_input.py").write_text("x = 1\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod, "run", fake_run)
    assert mod.regenerate("contract") is True
    assert (repo / "atlan.yaml").read_text() == "hand: authored\n"


def test_multi_root_one_bad_root_does_not_sink_the_others(repo, monkeypatch):
    """A partial regeneration beats none: the caller diffs the result."""
    _multi_root_contract(repo)

    def fake_run(cmd, *, check=False):
        if cmd[0] == "pkl" and cmd[1] == "eval":
            if Path(cmd[-1]).stem == "miner":
                return subprocess.CompletedProcess(cmd, 1, "", "boom")
            out = Path(cmd[cmd.index("-m") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "_input.py").write_text("ok = 1\n")
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(mod, "run", fake_run)
    monkeypatch.setattr(mod.time, "sleep", lambda _s: None)
    assert mod.regenerate("contract") is True
    assert (repo / "app" / "generated" / "crawler" / "_input.py").exists()
    assert not (repo / "app" / "generated" / "miner").exists()


def test_no_roots_at_all_is_still_a_skip(repo):
    """An app with a PklProject but no evaluable root regenerates nothing."""
    (repo / "contract" / "app.pkl").unlink(missing_ok=True)
    (repo / "contract" / "credentials.pkl").write_text('name = "creds"\n')
    assert mod.eval_roots("contract") == []
    assert mod.regenerate("contract") is False
