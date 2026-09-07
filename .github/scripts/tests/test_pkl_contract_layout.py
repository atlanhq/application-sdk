"""Tests for .github/scripts/pkl_contract_layout.py.

The module's job is to place `pkl eval` output for BOTH contract families —
`App.pkl` (keys prefixed `app/generated/`) and `NativeApp.pkl` /
`NativeAppBundle.pkl` (bare keys relative to the generated dir). Getting the
native family wrong is not a cosmetic bug: it was silently placing nothing,
which let toolkit bumps merge with stale artifacts behind a green check.

The end-to-end wiring (commit messages, degrade-to-lock-only) is covered in
test_renovate_pkl_sync.py; this file pins the placement rules themselves.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import pkl_contract_layout as mod


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Working tree (cwd) plus an empty eval output dir at `out`."""
    (tmp_path / "out").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(path: Path, content: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


# ── detect_layout ────────────────────────────────────────────────────────────


def test_detect_prefixed(tree):
    _write(tree / "out" / "app" / "generated" / "manifest.json")
    assert mod.detect_layout(tree / "out") == "prefixed"


def test_detect_native_bare_keys(tree):
    _write(tree / "out" / "manifest.json")
    assert mod.detect_layout(tree / "out") == "native"


def test_detect_native_entrypoint_keys(tree):
    _write(tree / "out" / "crawler" / "manifest.json")
    _write(tree / "out" / "atlan.yaml")
    assert mod.detect_layout(tree / "out") == "native"


def test_detect_root_only(tree):
    _write(tree / "out" / "atlan.yaml")
    _write(tree / "out" / "app.yaml")
    assert mod.detect_layout(tree / "out") == "root-only"


def test_detect_empty(tree):
    assert mod.detect_layout(tree / "out") == "empty"


# ── swap_outputs: prefixed family ────────────────────────────────────────────


def test_prefixed_replaces_wholesale(tree):
    """App.pkl owns every file under the generated dir, so orphans must go."""
    _write(tree / "app" / "generated" / "orphan.json")
    _write(tree / "out" / "app" / "generated" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is True

    assert (tree / "app" / "generated" / "manifest.json").read_text() == "fresh\n"
    assert not (tree / "app" / "generated" / "orphan.json").exists()


def test_prefixed_creates_missing_target(tree):
    _write(tree / "out" / "app" / "generated" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is True

    assert (tree / "app" / "generated" / "manifest.json").read_text() == "fresh\n"


def test_prefixed_preserves_reserved_frontend_subdir(tree):
    """`frontend/` (the app-playground install target) is not an orphan — no
    pkl contract, in any family or version, ever emits it — so it must survive
    the wholesale rmtree+copytree untouched, unlike `orphan.json` above."""
    _write(tree / "app" / "generated" / "frontend" / "static" / "index.html", "ui\n")
    _write(tree / "out" / "app" / "generated" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is True

    assert (tree / "app" / "generated" / "manifest.json").read_text() == "fresh\n"
    assert (
        tree / "app" / "generated" / "frontend" / "static" / "index.html"
    ).read_text() == "ui\n"


def test_prefixed_reserved_subdir_survives_alongside_override_protection(tree):
    """Reserved-subdir preservation and baseline override protection are
    independent mechanisms that must not interfere with each other."""
    _write(tree / "app" / "generated" / "frontend" / "static" / "index.html", "ui\n")
    _write(tree / "app" / "generated" / "manifest.json", "old-toolkit\n")
    _write(tree / "app" / "generated" / "connector.json", "hand-maintained\n")
    _write(tree / "base" / "app" / "generated" / "manifest.json", "old-toolkit\n")
    _write(tree / "base" / "app" / "generated" / "connector.json", "toolkit-default\n")
    _write(tree / "out" / "app" / "generated" / "manifest.json", "new-toolkit\n")
    _write(tree / "out" / "app" / "generated" / "connector.json", "toolkit-v2\n")

    assert mod.swap_outputs(tree / "out", baseline_dir=tree / "base") is True

    gen = tree / "app" / "generated"
    assert gen.joinpath("manifest.json").read_text() == "new-toolkit\n"
    assert gen.joinpath("connector.json").read_text() == "hand-maintained\n"
    assert gen.joinpath("frontend", "static", "index.html").read_text() == "ui\n"


def test_prefixed_reserved_subdir_absent_is_a_noop(tree):
    """No `frontend/` to preserve is the common case — must not error."""
    _write(tree / "out" / "app" / "generated" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is True

    assert not (tree / "app" / "generated" / "frontend").exists()


def test_prefixed_reserved_subdir_yields_to_a_real_emitted_key(tree, capsys):
    """If a contract ever legitimately emits a `frontend` key, that is a real
    change to surface, not something to silently clobber with the withheld
    copy — the fresh output wins and a warning is printed."""
    _write(tree / "app" / "generated" / "frontend" / "static" / "index.html", "old\n")
    _write(tree / "out" / "app" / "generated" / "frontend" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is True

    gen = tree / "app" / "generated"
    assert gen.joinpath("frontend", "manifest.json").read_text() == "fresh\n"
    assert not gen.joinpath("frontend", "static", "index.html").exists()
    assert "collides with a reserved name" in capsys.readouterr().out


# ── swap_outputs: native family ──────────────────────────────────────────────


def test_native_overwrites_without_deleting_app_owned_files(tree):
    """Overwrite-only: the generated dir holds files pkl does not emit (an
    `__init__.py` the app's generate task touches, a post-processed artifact),
    and bare keys give no way to tell those from orphans. Deleting them is the
    worse failure, so they survive."""
    _write(tree / "app" / "generated" / "manifest.json", "stale\n")
    _write(tree / "app" / "generated" / "__init__.py", "")
    _write(tree / "out" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is True

    assert (tree / "app" / "generated" / "manifest.json").read_text() == "fresh\n"
    assert (tree / "app" / "generated" / "__init__.py").exists()


def test_native_bundle_places_entrypoint_dirs(tree):
    _write(tree / "app" / "generated" / "crawler" / "manifest.json", "stale\n")
    _write(tree / "out" / "crawler" / "manifest.json", "fresh\n")
    _write(tree / "out" / "atlan.yaml", "deploy: true\n")

    assert mod.swap_outputs(tree / "out") is True

    assert (
        tree / "app" / "generated" / "crawler" / "manifest.json"
    ).read_text() == "fresh\n"
    # Root files land at the repo root, never inside the generated dir.
    assert (tree / "atlan.yaml").read_text() == "deploy: true\n"
    assert not (tree / "app" / "generated" / "atlan.yaml").exists()


def test_native_merges_new_entrypoint(tree):
    """A contract growing an entrypoint still swaps: some names overlap the
    target, the new one does not."""
    _write(tree / "app" / "generated" / "crawler" / "manifest.json", "stale\n")
    _write(tree / "out" / "crawler" / "manifest.json", "fresh\n")
    _write(tree / "out" / "miner" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is True

    assert (tree / "app" / "generated" / "miner" / "manifest.json").exists()


def test_native_refuses_mismatched_generated_dir(tree, capsys):
    """A multi-variant app generating into `app/generated/<variant>` emits the
    same bare keys. Placing them at `app/generated/` would create wrong files
    beside the real ones, so with no overlap at all the swap refuses."""
    _write(tree / "app" / "generated" / "apache" / "manifest.json", "stale\n")
    _write(tree / "out" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is False

    assert not (tree / "app" / "generated" / "manifest.json").exists()
    assert (
        tree / "app" / "generated" / "apache" / "manifest.json"
    ).read_text() == "stale\n"
    assert "emitted unprefixed contract artifacts" in capsys.readouterr().out


def test_native_accepts_empty_target(tree):
    """A first generation has nothing to compare against."""
    (tree / "app" / "generated").mkdir(parents=True)
    _write(tree / "out" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out") is True

    assert (tree / "app" / "generated" / "manifest.json").read_text() == "fresh\n"


def test_native_honours_explicit_generated_dir(tree):
    """The variant app works once it declares its real base."""
    _write(tree / "app" / "generated" / "apache" / "manifest.json", "stale\n")
    _write(tree / "out" / "manifest.json", "fresh\n")

    assert mod.swap_outputs(tree / "out", "app/generated/apache") is True

    assert (
        tree / "app" / "generated" / "apache" / "manifest.json"
    ).read_text() == "fresh\n"


# ── swap_outputs: nothing to place ───────────────────────────────────────────


def test_root_only_copies_root_files_but_returns_false(tree, capsys):
    """Root files are unambiguous in every layout so they are still copied, but
    no generated artifact was placed — the caller must not report success."""
    _write(tree / "app" / "generated" / "manifest.json", "stale\n")
    _write(tree / "out" / "atlan.yaml", "deploy: true\n")

    assert mod.swap_outputs(tree / "out") is False

    assert (tree / "atlan.yaml").read_text() == "deploy: true\n"
    assert (tree / "app" / "generated" / "manifest.json").read_text() == "stale\n"
    assert (
        "produced no generated contract artifacts (root-only)"
        in capsys.readouterr().out
    )


def test_empty_output_returns_false(tree, capsys):
    _write(tree / "app" / "generated" / "manifest.json", "stale\n")

    assert mod.swap_outputs(tree / "out") is False

    assert (tree / "app" / "generated" / "manifest.json").read_text() == "stale\n"
    assert "(empty)" in capsys.readouterr().out


# ── run_post_generate ────────────────────────────────────────────────────────


def test_post_generate_absent_is_silent_noop(tree, capsys):
    (tree / "contract").mkdir()

    mod.run_post_generate("contract")

    assert capsys.readouterr().out == ""


def test_post_generate_runs_from_repo_root_without_exec_bit(tree):
    """Run via `sh`, so an app does not have to remember chmod +x — and with cwd
    at the repo root, so the script's paths match what it would use locally."""
    _write(tree / "contract" / mod.POST_GENERATE_SCRIPT, "echo patched > marker.txt\n")

    mod.run_post_generate("contract")

    assert (tree / "marker.txt").read_text().strip() == "patched"


def test_post_generate_failure_warns_and_returns(tree, capsys):
    """Best-effort: a failing step must not raise — it runs after the swap, so
    the caller's fresh output stands and the diff is left for a human."""
    _write(tree / "contract" / mod.POST_GENERATE_SCRIPT, "exit 7\n")

    mod.run_post_generate("contract")

    assert "post-generate.sh failed" in capsys.readouterr().out


# ── override detection (app-post-processed files) ────────────────────────────


def test_baseline_preserves_overridden_file_and_refreshes_the_rest(tree, capsys):
    """The core of centralized override protection. `connector.json` is committed
    with hand-maintained content that differs from what the old pin emitted, so
    the app post-processes it -> preserve. `manifest.json` matches the old pin's
    output exactly, so the toolkit owns it -> refresh."""
    _write(tree / "app" / "generated" / "manifest.json", "old-toolkit\n")
    _write(tree / "app" / "generated" / "connector.json", "hand-maintained\n")
    # Baseline = what the PREVIOUS toolkit pin emitted.
    _write(tree / "base" / "manifest.json", "old-toolkit\n")
    _write(tree / "base" / "connector.json", "toolkit-default\n")
    # New eval at the bumped pin.
    _write(tree / "out" / "manifest.json", "new-toolkit\n")
    _write(tree / "out" / "connector.json", "toolkit-default-v2\n")

    assert mod.swap_outputs(tree / "out", baseline_dir=tree / "base") is True

    gen = tree / "app" / "generated"
    assert gen.joinpath("manifest.json").read_text() == "new-toolkit\n"
    assert gen.joinpath("connector.json").read_text() == "hand-maintained\n"
    assert "Preserved app-maintained generated file(s)" in capsys.readouterr().out


def test_baseline_protection_also_covers_the_prefixed_wholesale_replace(tree):
    """The prefixed family rmtree's the whole dir, so preserved content has to be
    read out and written back — not just skipped."""
    _write(tree / "app" / "generated" / "manifest.json", "old-toolkit\n")
    _write(tree / "app" / "generated" / "connector.json", "hand-maintained\n")
    _write(tree / "base" / "app" / "generated" / "manifest.json", "old-toolkit\n")
    _write(tree / "base" / "app" / "generated" / "connector.json", "toolkit-default\n")
    _write(tree / "out" / "app" / "generated" / "manifest.json", "new-toolkit\n")
    _write(tree / "out" / "app" / "generated" / "connector.json", "toolkit-v2\n")

    assert mod.swap_outputs(tree / "out", baseline_dir=tree / "base") is True

    gen = tree / "app" / "generated"
    assert gen.joinpath("manifest.json").read_text() == "new-toolkit\n"
    assert gen.joinpath("connector.json").read_text() == "hand-maintained\n"


def test_baseline_does_not_protect_a_newly_emitted_file(tree):
    """A file absent from the baseline is new, not overridden — it must land."""
    _write(tree / "app" / "generated" / "manifest.json", "old\n")
    _write(tree / "base" / "manifest.json", "old\n")
    _write(tree / "out" / "manifest.json", "new\n")
    _write(tree / "out" / "brand-new.json", "new\n")

    assert mod.swap_outputs(tree / "out", baseline_dir=tree / "base") is True

    assert (tree / "app" / "generated" / "brand-new.json").exists()


def test_without_baseline_everything_is_overwritten(tree):
    """No baseline (no pin change in flight, or it could not be produced) keeps
    the plain behaviour — overwrite, so regeneration is never a silent no-op."""
    _write(tree / "app" / "generated" / "connector.json", "hand-maintained\n")
    _write(tree / "out" / "connector.json", "toolkit-default\n")

    assert mod.swap_outputs(tree / "out") is True

    assert (
        tree / "app" / "generated" / "connector.json"
    ).read_text() == "toolkit-default\n"


# ── baseline_contract_ref ────────────────────────────────────────────────────


def _repo(tree: Path) -> None:
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@e.com"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=tree, check=True, capture_output=True)


def _commit(tree: Path, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=tree, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", message], cwd=tree, check=True, capture_output=True
    )


def test_baseline_ref_is_head_for_an_uncommitted_bump(tree):
    """Renovate's postUpgradeTasks path: the pin is rewritten in the working tree
    but not committed, so HEAD still holds the pre-bump pin."""
    _repo(tree)
    _write(tree / "contract" / "PklProject", "toolkit@1.0.0\n")
    _commit(tree, "pin 1.0.0")
    (tree / "contract" / "PklProject").write_text("toolkit@1.0.1\n")

    assert mod.baseline_contract_ref("contract") == "HEAD"


def test_baseline_ref_is_the_bump_commits_parent_when_committed(tree):
    """The workflow-shim path: Renovate already committed the bump."""
    _repo(tree)
    _write(tree / "contract" / "PklProject", "toolkit@1.0.0\n")
    _commit(tree, "pin 1.0.0")
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=tree,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    (tree / "contract" / "PklProject").write_text("toolkit@1.0.1\n")
    _commit(tree, "bump to 1.0.1")

    assert mod.baseline_contract_ref("contract") == parent


def test_baseline_ref_is_none_when_the_pin_is_unchanged(tree):
    """No bump in flight — an ordinary PR. Protection must stay OFF here, or an
    arbitrarily old baseline would freeze the tree and blind the freshness gate."""
    _repo(tree)
    _write(tree / "contract" / "PklProject", "toolkit@1.0.0\n")
    _commit(tree, "pin 1.0.0")
    _write(tree / "app" / "generated" / "manifest.json", "x\n")
    _commit(tree, "unrelated change")

    assert mod.baseline_contract_ref("contract") is None


def test_baseline_ref_is_none_without_a_parent_commit(tree):
    """Root commit (or a shallow clone that lacks the parent): no baseline, so the
    caller overwrites and says so rather than preserving everything."""
    _repo(tree)
    _write(tree / "contract" / "PklProject", "toolkit@1.0.0\n")
    _commit(tree, "initial, pin arrives in the root commit")

    assert mod.baseline_contract_ref("contract") is None


def test_baseline_ref_is_none_outside_a_repo(tree):
    _write(tree / "contract" / "PklProject", "toolkit@1.0.0\n")

    assert mod.baseline_contract_ref("contract") is None


def test_export_contract_at_materialises_the_old_pin(tree):
    _repo(tree)
    _write(tree / "contract" / "PklProject", "toolkit@1.0.0\n")
    _write(tree / "contract" / "app.pkl", "amends x\n")
    _commit(tree, "pin 1.0.0")
    (tree / "contract" / "PklProject").write_text("toolkit@1.0.1\n")
    _commit(tree, "bump")

    dest = tree / "exported"
    assert mod.export_contract_at("HEAD~1", "contract", dest) is True
    assert (dest / "contract" / "PklProject").read_text() == "toolkit@1.0.0\n"


def test_export_contract_at_returns_false_for_a_bad_ref(tree):
    _repo(tree)
    _write(tree / "contract" / "PklProject", "x\n")
    _commit(tree, "init")

    assert mod.export_contract_at("nope-not-a-ref", "contract", tree / "e") is False


def test_export_contract_at_accepts_a_multi_root_archive(tmp_path, monkeypatch):
    """Validity is "the archive holds an evaluable contract", not "it holds
    app.pkl". Requiring app.pkl meant a one-root-per-entrypoint app never got
    a baseline, silently disabling override detection for exactly the apps
    whose generated trees are most likely to be post-processed."""
    src = tmp_path / "src"
    (src / "contract").mkdir(parents=True)
    (src / "contract" / "crawler.pkl").write_text(
        'amends "@app-contract-toolkit/NativeApp.pkl"\n'
    )
    (src / "contract" / "credentials.pkl").write_text('name = "creds"\n')
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "c"],
    ):
        subprocess.run(["git", *args], cwd=src, check=True, capture_output=True)

    monkeypatch.chdir(src)
    dest = tmp_path / "dest"
    assert mod.export_contract_at("HEAD", "contract", dest) is True

    # an archive with only imported modules is NOT evaluable
    src2 = tmp_path / "src2"
    (src2 / "contract").mkdir(parents=True)
    (src2 / "contract" / "credentials.pkl").write_text('name = "creds"\n')
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t"],
        ["config", "user.name", "t"],
        ["add", "-A"],
        ["commit", "-qm", "c"],
    ):
        subprocess.run(["git", *args], cwd=src2, check=True, capture_output=True)
    monkeypatch.chdir(src2)
    assert mod.export_contract_at("HEAD", "contract", tmp_path / "dest2") is False
