#!/usr/bin/env python3
"""Renovate app-contract-toolkit sync driver.

Invoked by .github/workflows/renovate-pkl-sync.yaml when Renovate bumps the
``@<version>`` URI in ``contract/PklProject`` on a ``renovate/**`` branch.

Two responsibilities:

  1. Always re-resolve ``contract/PklProject.deps.json`` (``pkl project
     resolve``) so the Pkl lock matches the bumped pin.
  2. When ``--regenerate true`` is passed, regenerate the contract artifacts
     (``app/generated/**`` + ``atlan.yaml`` / ``app.yaml``) via ``pkl eval`` so
     a toolkit bump that changes generated output lands as a self-contained PR.

Then it stages and commits whatever changed; the caller workflow runs
``git push``.

Why this is a script and not inlined YAML: it carries all the conditional
logic (opt-in gate, missing-contract skip, eval-failure fallback,
nothing-changed short-circuit) and is therefore unit-tested in
``.github/scripts/tests/test_renovate_pkl_sync.py``. Inlined shell with
branching cannot be regression-tested.

Safety contract — regeneration can never make a toolkit PR *worse* than a pure
re-resolve:

  * Opt-in: regeneration only runs when ``--regenerate true``. Off by default,
    so an app is never auto-regenerated until its owner has confirmed the
    contract regenerates to working output (and wired the input). Apps with no
    sync caller at all are unaffected.
  * Non-fatal: a failed ``pkl eval`` logs a warning and degrades to a lock-only
    sync rather than failing the job.
  * Atomic: eval writes into a temp dir and is swapped into the working tree
    only on full success, so a failure leaves the committed ``app/generated``
    untouched (no half-regenerated state).

Scope: assumes the standard repo-root layout (``contract/PklProject``,
``contract/app.pkl``, output at the repo root). Non-standard layouts
(``app/contract/``, monorepo ``apps/*/contract/``) self-skip regeneration when
``contract/app.pkl`` is absent.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Lock file produced by `pkl project resolve`.
LOCK_PATH = "contract/PklProject.deps.json"

# Everything `pkl eval -m .` emits, relative to the repo root. Mirrors the
# cleanup list in contract-toolkit/scripts/regenerate-all.sh.
OUTPUT_PATHS = ["app/generated", "atlan.yaml", "app.yaml"]

COMMIT_MESSAGE_REGEN = (
    "chore: sync Pkl deps and regenerate contract artifacts for app-contract-toolkit"
)
COMMIT_MESSAGE_LOCK_ONLY = (
    "chore: sync PklProject.deps.json with updated app-contract-toolkit"
)


def run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess. Single seam so tests can stub pkl/ruff and let git run
    for real against a throwaway repo."""
    return subprocess.run(cmd, check=check, text=True)


def resolve(contract_dir: str) -> None:
    """Re-resolve the Pkl lock. Fatal on failure (same as the pre-script
    behaviour): without a lock the whole sync is moot."""
    run(["pkl", "project", "resolve", f"{contract_dir}/"], check=True)


def regenerate(contract_dir: str) -> bool:
    """Regenerate contract artifacts atomically.

    Returns True only when the working tree was actually updated with fresh
    artifacts. Returns False (degrading to a lock-only sync) when there is no
    contract to generate from or ``pkl eval`` fails — never raises on an eval
    failure, so a bad regen cannot fail the job.
    """
    app_pkl = Path(contract_dir) / "app.pkl"
    if not app_pkl.exists():
        print(
            f"::notice::No {app_pkl} — skipping artifact regeneration (re-resolve only)."
        )
        return False

    tmp = Path(tempfile.mkdtemp())
    try:
        # --project-dir: the contract is a Pkl project declaring
        # app-contract-toolkit as a *remote package*, so eval must load that
        # project to resolve the `@app-contract-toolkit` import. The bare
        # `pkl eval contract/app.pkl` from the repo root finds no project and
        # fails. -m writes each output key relative to the output base.
        result = run(
            ["pkl", "eval", "--project-dir", contract_dir, "-m", str(tmp), str(app_pkl)]
        )
        if result.returncode != 0:
            print(
                "::warning::pkl eval failed — falling back to lock-only sync "
                "(generated artifacts left unchanged)."
            )
            return False

        _format_generated(tmp)
        _swap_outputs(tmp)
        print("Regenerated contract artifacts.")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _format_generated(out_dir: Path) -> None:
    """ruff-fix + format the generated _input.py, mirroring
    contract-toolkit/scripts/regenerate-all.sh. Best-effort: this commit
    bypasses pre-commit, so we format here for apps that lint app/generated,
    but a ruff hiccup must not fail the sync (many apps exclude app/generated
    from lint entirely)."""
    inputs = sorted((out_dir / "app" / "generated").rglob("_input.py"))
    if not inputs:
        return
    paths = [str(p) for p in inputs]
    run(["uvx", "ruff", "check", "--fix", "--select", "F401", "--quiet", *paths])
    run(["uvx", "ruff", "format", *paths])


def _swap_outputs(out_dir: Path) -> None:
    """Replace the working tree's generated artifacts with the freshly
    generated ones. Replacing app/generated wholesale clears orphans left by a
    removed/renamed bundle."""
    generated = out_dir / "app" / "generated"
    if generated.is_dir():
        shutil.rmtree("app/generated", ignore_errors=True)
        os.makedirs("app", exist_ok=True)
        shutil.copytree(generated, "app/generated")
    for name in ("atlan.yaml", "app.yaml"):
        src = out_dir / name
        if src.exists():
            shutil.copyfile(src, name)


def stage_and_commit(message: str) -> bool:
    """Stage the known sync outputs and commit iff something changed.

    Returns True when a commit was made. Stages only the re-resolve /
    generation outputs (``-A`` also captures deletions of orphaned generated
    files) so incidental working-tree noise (e.g. a ruff cache) is never
    committed."""
    run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    for path in [LOCK_PATH, *OUTPUT_PATHS]:
        if os.path.exists(path):
            run(["git", "add", "-A", "--", path], check=True)

    if run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        print("No contract artifact changes to commit.")
        return False

    run(["git", "commit", "-m", message], check=True)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract-dir",
        default="contract",
        help="Directory containing PklProject and app.pkl (default: contract).",
    )
    parser.add_argument(
        "--regenerate",
        default="false",
        help="'true' to also regenerate app/generated/** via pkl eval; "
        "anything else re-resolves the lock only (default: false).",
    )
    args = parser.parse_args(argv)
    regenerate_enabled = str(args.regenerate).strip().lower() == "true"

    resolve(args.contract_dir)
    regenerated = regenerate(args.contract_dir) if regenerate_enabled else False
    message = COMMIT_MESSAGE_REGEN if regenerated else COMMIT_MESSAGE_LOCK_ONLY
    stage_and_commit(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
