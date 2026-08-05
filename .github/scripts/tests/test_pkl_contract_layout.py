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
