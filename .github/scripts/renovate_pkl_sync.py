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

Then, unless ``--no-commit`` is passed, it stages and commits whatever changed;
the caller workflow runs ``git push``. With ``--no-commit`` it generates only and
leaves staging/commit to the caller — used by Renovate's ``postUpgradeTasks``,
which commits the ``fileFilters`` matches into the upgrade branch itself. A repo
with no ``contract/PklProject`` is a safe no-op (nothing to resolve).

Why this is a script and not inlined YAML: it carries all the conditional
logic (opt-in gate, missing-contract skip, eval-failure fallback,
nothing-changed short-circuit) and is therefore unit-tested in
``.github/scripts/tests/test_renovate_pkl_sync.py``. Inlined shell with
branching cannot be regression-tested.

Safety contract — regeneration can never make a toolkit PR *worse* than a pure
re-resolve:

  * Opt-out: regeneration runs by default (``--regenerate true``); an app opts
    out with ``--regenerate false`` (e.g. hand-maintained generated config, or
    a layout this can't drive). Apps with no sync caller at all are unaffected.
  * Non-fatal: a failed ``pkl eval`` logs a warning and degrades to a lock-only
    sync rather than failing the job.
  * Commit-gated: eval writes into a temp dir and is swapped into the working
    tree only after eval succeeds; the commit only happens after this
    function returns. So a failed/killed eval commits nothing and never
    publishes a half-regenerated tree. The swap itself (rmtree + copytree) is
    best-effort, not crash-safe — a SIGKILL mid-swap could leave the *local*
    tree half-populated, but that run commits nothing, so the branch is safe.
    Formatting runs *after* the swap, on the real in-place files (see
    ``_format_generated``) — it's best-effort and never gates the swap; a
    ruff hiccup leaves valid-but-unformatted generated output rather than
    blocking the commit.

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
import time
from pathlib import Path

# Lock file produced by `pkl project resolve`.
LOCK_PATH = "contract/PklProject.deps.json"

# Everything `pkl eval -m .` emits, relative to the repo root. Mirrors the
# cleanup list in contract-toolkit/scripts/regenerate-all.sh.
OUTPUT_PATHS = ["app/generated", "atlan.yaml", "app.yaml"]

# `pkl eval` can fail transiently on a cold CI runner while fetching the remote
# @app-contract-toolkit package — a network blip returns a non-zero code (not an
# OSError). Retry a few times before giving up, so a transient fetch failure
# neither degrades a renovate sync to lock-only nor turns the freshness gate
# red. A deterministically-broken contract simply fails every attempt (a few
# extra seconds) and still returns False. Shared by both callers on purpose.
EVAL_MAX_ATTEMPTS = 3
EVAL_RETRY_SLEEP_S = 5.0

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
    """Regenerate contract artifacts; swap gated on eval+format success.

    Eval runs into a temp dir; the working tree is only touched once eval (and
    formatting) succeed. The swap itself is best-effort (rmtree + copytree), not
    crash-safe — but the caller commits only after this returns, so a failed or
    killed run commits nothing and never publishes a half-regenerated tree.

    Returns True only when the working tree was actually updated with fresh
    artifacts. Returns False when there is no contract to generate from, or when
    ``pkl eval`` still fails after ``EVAL_MAX_ATTEMPTS`` attempts — never raises
    on an eval failure, so a bad regen cannot fail the job. How to degrade
    (lock-only sync, red gate) is the caller's decision, not this function's.
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
        eval_cmd = [
            "pkl",
            "eval",
            "--project-dir",
            contract_dir,
            "-m",
            str(tmp),
            str(app_pkl),
        ]
        result = run(eval_cmd)
        attempt = 1
        while result.returncode != 0 and attempt < EVAL_MAX_ATTEMPTS:
            print(
                f"::warning::pkl eval failed (attempt {attempt}/{EVAL_MAX_ATTEMPTS}); "
                f"retrying in {EVAL_RETRY_SLEEP_S:g}s — a cold runner may still be "
                "fetching the remote @app-contract-toolkit package."
            )
            time.sleep(EVAL_RETRY_SLEEP_S)
            attempt += 1
            result = run(eval_cmd)
        if result.returncode != 0:
            print(
                f"::warning::pkl eval failed after {EVAL_MAX_ATTEMPTS} attempts "
                "— generated artifacts left unchanged."
            )
            return False

        _swap_outputs(tmp)
        _format_generated()
        print("Regenerated contract artifacts.")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _format_generated() -> None:
    """ruff-fix + format every generated *.py in the working tree (post-swap),
    mirroring contract-toolkit/scripts/regenerate-all.sh. The contract emits
    more than _input.py (e.g. _e2e_base.py, _e2e_credential.py,
    _e2e_substitutions.py); every one must match what pre-commit's ruff would
    produce, or the consumer's pre-commit reformats it on the renovate PR and
    fails CI. This sync commit bypasses pre-commit, so we format here.
    Best-effort: a ruff hiccup must not fail the sync (many apps exclude
    app/generated from lint entirely).

    Runs after `_swap_outputs`, on the real `app/generated/**` path relative
    to cwd (the consumer repo root) — not the temp eval output dir. `ruff
    check --fix` also runs with no --select, so it applies whatever the
    consumer's own pyproject.toml configures (fleet configs aren't uniform:
    e.g. atlan-hello-world-app selects "I" for import sorting; application-sdk's
    own config and the app-template scaffold do not). Both of these depend on
    linting the files at their real repo-relative path: `select`/`extend-select`
    resolve via cwd regardless, but path-scoped `per-file-ignores`/`exclude`
    patterns (e.g. an app that exempts `app/generated/**` from a rule) only
    match a real relative path — they silently fail to match an absolute
    temp-dir path, which would make this over-apply rules relative to what
    pre-commit actually enforces.

    `--force-exclude` makes ruff honor those `exclude`/`extend-exclude` patterns
    even for the explicitly-passed paths (ruff otherwise ignores excludes for
    paths named on the command line). An app that excludes `app/generated` then
    keeps its raw pkl output here instead of this pass reformatting it into
    freshness drift (CNCT-70)."""
    inputs = sorted(Path("app/generated").rglob("*.py"))
    if not inputs:
        return
    paths = [str(p) for p in inputs]
    run(["uvx", "ruff", "check", "--fix", "--quiet", "--force-exclude", *paths])
    run(["uvx", "ruff", "format", "--force-exclude", *paths])


def _swap_outputs(out_dir: Path) -> None:
    """Replace the working tree's generated artifacts with the freshly
    generated ones. Replacing app/generated wholesale clears orphans left by a
    removed/renamed bundle.

    If eval emitted no app/generated at all (a degenerate/partial output a real
    contract never produces), the existing committed dir is left untouched
    rather than deleted — we never destroy generated output just because one
    eval didn't reproduce it. Only the artifacts eval actually emitted are
    swapped in; stage_and_commit then commits whatever changed."""
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
        choices=["true", "false"],
        default="true",
        help="'true' (default) to also regenerate app/generated/** via pkl "
        "eval; 'false' to re-resolve the lock only.",
    )
    parser.add_argument(
        "--no-commit",
        action="store_true",
        help="Generate/re-resolve artifacts but do NOT git-add or commit them. "
        "Use when the caller commits the changed files itself — e.g. Renovate's "
        "postUpgradeTasks, which stages whatever the `fileFilters` match into the "
        "upgrade branch. Without this flag the driver stages and commits (the "
        "GitHub Actions glue-workflow behaviour).",
    )
    args = parser.parse_args(argv)
    regenerate_enabled = args.regenerate == "true"

    # Safe no-op on any repo/branch without a Pkl contract. Under Renovate the
    # postUpgradeTask is scoped to the app-contract-toolkit manager, but
    # executionMode=branch still invokes this once per matched branch, and a
    # contract-less repo (application-sdk itself, or an app with no contract/)
    # has no PklProject to resolve. Bail before `pkl project resolve`, which is
    # fatal on a missing project — a contract-less repo must never fail the task.
    pkl_project = Path(args.contract_dir) / "PklProject"
    if not pkl_project.exists():
        print(f"::notice::No {pkl_project} — nothing to sync (skipped).")
        return 0

    resolve(args.contract_dir)
    regenerated = regenerate(args.contract_dir) if regenerate_enabled else False
    if args.no_commit:
        # Renovate commits the fileFilters matches itself; the driver only
        # generates. Leaving git untouched here also keeps the git identity /
        # commit-message concern entirely on the caller side.
        print("--no-commit: staging and commit left to the caller.")
        return 0
    message = COMMIT_MESSAGE_REGEN if regenerated else COMMIT_MESSAGE_LOCK_ONLY
    stage_and_commit(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
