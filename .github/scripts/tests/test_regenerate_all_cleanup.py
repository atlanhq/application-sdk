"""Regression guard for contract-toolkit/scripts/regenerate-all.sh's cleanup (FND-4937).

The contract-toolkit freshness job regenerates every example and then runs
``git diff --exit-code``. That catches a file whose content changed and a new
untracked file, but not a tracked file that is never rewritten. So the script
has to delete every output path before each eval. A caller node the toolkit
stops emitting is then a deletion that the diff check sees. Before FND-4937 it
skipped ``contract/generated/``, and a stale ``input.pkl`` stayed committed
while CI passed.

The committed examples are fresh, so the normal freshness run can't notice if
this cleanup is removed again. These tests run the real script against a
throwaway toolkit tree, with a stub ``pkl`` on PATH that emits a known set of
outputs. Every stale file seeded before the run must be gone afterwards.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[3]
    / "contract-toolkit"
    / "scripts"
    / "regenerate-all.sh"
)

# Stands in for `pkl eval -m <example_dir> <example_dir>/app.pkl`. It writes the
# outputs a current toolkit would emit for a system app: one caller node, one
# app/generated file, and the two root files.
STUB_PKL = """#!/usr/bin/env bash
set -euo pipefail
[ "$1" = eval ] && [ "$2" = -m ] || { echo "unexpected pkl args: $*" >&2; exit 2; }
out="$3"
mkdir -p "$out/contract/generated/current" "$out/app/generated"
echo fresh > "$out/contract/generated/current/input.pkl"
echo fresh > "$out/app/generated/manifest.json"
echo fresh > "$out/atlan.yaml"
echo fresh > "$out/app.yaml"
"""

STALE_PATHS = [
    # The FND-4937 case: a caller node for an entrypoint that no longer exists.
    "contract/generated/removed-entrypoint/input.pkl",
    # A single-entrypoint caller node sitting directly under contract/generated/.
    "contract/generated/input.pkl",
    "app/generated/old.json",
]


def _make_toolkit(tmp_path: Path) -> tuple[Path, Path]:
    """Lay out <toolkit>/scripts/regenerate-all.sh and one example.

    The script resolves the toolkit root as its own parent directory, so a copy
    placed in a temp toolkit works on that tree and leaves the real examples
    untouched.
    """
    toolkit = tmp_path / "toolkit"
    (toolkit / "scripts").mkdir(parents=True)
    shutil.copy2(SCRIPT, toolkit / "scripts" / "regenerate-all.sh")
    example = toolkit / "examples" / "sys"
    example.mkdir(parents=True)
    (example / "app.pkl").write_text("// stub\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    pkl = bin_dir / "pkl"
    pkl.write_text(STUB_PKL)
    pkl.chmod(pkl.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return toolkit, bin_dir


def _run(toolkit: Path, bin_dir: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(
        ["bash", str(toolkit / "scripts" / "regenerate-all.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("stale", STALE_PATHS)
def test_stale_output_is_removed_before_eval(tmp_path: Path, stale: str) -> None:
    toolkit, bin_dir = _make_toolkit(tmp_path)
    example = toolkit / "examples" / "sys"
    seeded = example / stale
    seeded.parent.mkdir(parents=True, exist_ok=True)
    seeded.write_text("stale\n")

    result = _run(toolkit, bin_dir)

    assert result.returncode == 0, result.stdout + result.stderr
    assert not seeded.exists(), (
        f"regenerate-all.sh left {stale} in place; a committed copy would pass "
        "the freshness diff check while matching no output"
    )
    # The current outputs are recreated, so the cleanup deleted only stale files.
    assert (example / "contract/generated/current/input.pkl").read_text() == "fresh\n"
    assert (example / "app/generated/manifest.json").read_text() == "fresh\n"
