#!/usr/bin/env python3
"""Renovate app-contract-toolkit sync driver.

Invoked by the fleet preset's ``postUpgradeTasks`` command (see the
``app-contract-toolkit`` rule in ``renovate-config/default.json``) when Renovate
bumps the ``@<version>`` URI in ``contract/PklProject``, so the re-resolve lands
inside the PR Renovate is already opening. The self-hosted runner installs this
file on PATH as ``renovate-pkl-sync`` — see the install step in
``.github/workflows/renovate.yaml``.

Until FND-395 there was a second entry point: a ``renovate-pkl-sync.yaml``
reusable that each app called on ``push`` to ``renovate/**``, which existed only
because ``postUpgradeTasks`` commands are gated by the admin-only
``allowedCommands`` allowlist and were therefore inert under the Mend-hosted
app. Mend is uninstalled fleet-wide, so that path was retired along with its
callers. Anything reachable only from a pushed bot branch is gone with it.

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

Both contract families are supported: ``App.pkl`` (output keys prefixed
``app/generated/``) and ``NativeApp.pkl`` / ``NativeAppBundle.pkl`` (unprefixed
keys, relative to the generated dir). Placement for each lives in
``pkl_contract_layout.py`` — see that module for why they differ and what makes
a swap refuse. A refusal degrades this sync to lock-only and says so; it is
never silent, because a silent refusal is exactly how the native family came to
merge toolkit bumps with stale artifacts and a green check.

Scope: assumes the contract lives in ``contract/`` with ``PklProject`` +
``app.pkl`` beside each other. Other layouts (``app/contract/``, monorepo
``apps/*/contract/``) self-skip regeneration when ``contract/app.pkl`` is absent.
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

# Placement of eval output is family-dependent and shared with
# regenerate_contract.py / check_generated_freshness.py — see that module.
sys.path.insert(0, str(Path(__file__).parent))
from pkl_contract_layout import (  # noqa: E402
    GENERATED_DIR,
    ROOT_FILES,
    baseline_contract_ref,
    export_contract_at,
    run_post_generate,
    swap_outputs,
)

# Lock file produced by `pkl project resolve`.
LOCK_PATH = "contract/PklProject.deps.json"

# Everything a contract can emit, relative to the repo root. Mirrors the
# cleanup list in contract-toolkit/scripts/regenerate-all.sh.
OUTPUT_PATHS = [GENERATED_DIR, *ROOT_FILES]

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


def eval_roots(contract_dir: str) -> list[Path]:
    """The contract's pkl EVAL ROOTS, newest-convention first.

    A root is a ``.pkl`` that ``amends`` a toolkit template — that is what
    makes it evaluable on its own. Files the roots merely ``import``
    (``credentials.pkl`` is the common one) are modules, not roots, and
    evaluating them is meaningless.

    ``app.pkl`` is returned alone when present: it is the single-root
    convention and its outputs land at the generated dir itself.
    """
    d = Path(contract_dir)
    if not d.is_dir():
        return []
    if (d / "app.pkl").is_file():
        return [d / "app.pkl"]
    roots: list[Path] = []
    for f in sorted(d.glob("*.pkl")):
        try:
            text = f.read_text(encoding="utf-8")
        except OSError:
            continue
        if any(ln.startswith("amends ") for ln in text.splitlines()):
            roots.append(f)
    return roots


def _eval_root(root: Path, contract_dir: str, out: Path) -> bool:
    """One `pkl eval -m` with the shared retry. True iff it produced output."""
    cmd = ["pkl", "eval", "--project-dir", contract_dir, "-m", str(out), str(root)]
    result = run(cmd)
    attempt = 1
    while result.returncode != 0 and attempt < EVAL_MAX_ATTEMPTS:
        print(
            f"::warning::pkl eval failed for {root.name} "
            f"(attempt {attempt}/{EVAL_MAX_ATTEMPTS}); retrying in "
            f"{EVAL_RETRY_SLEEP_S:g}s — a cold runner may still be fetching the "
            "remote @app-contract-toolkit package."
        )
        time.sleep(EVAL_RETRY_SLEEP_S)
        attempt += 1
        result = run(cmd)
    return result.returncode == 0


def regenerate_multi_root(contract_dir: str, roots: list[Path]) -> bool:
    """Regenerate an app whose contract has ONE ROOT PER ENTRYPOINT.

    Some apps (synapse: ``crawler.pkl`` + ``miner.pkl``) have no single
    ``app.pkl``; each root generates its own tree under
    ``<generated>/<root stem>/``. They CANNOT share one ``pkl eval -m`` base:
    every root emits the same unprefixed key names (``_input.py``,
    ``manifest.json``, ``__init__.py``, the connectors JSON), so a shared base
    silently lets the last root clobber the others — measured on synapse,
    4 of 5 filenames collide. Each root therefore gets its own eval base and
    its own swap target.

    Root files (``atlan.yaml``/``app.yaml``) are deliberately NOT written from
    here: no per-entrypoint root emits them, so anything at the repo root is
    hand-authored and must survive (a prior incident clobbered exactly that).

    Returns True iff at least one root regenerated. A root that fails eval or
    whose swap refuses the layout is warned about and skipped — a partial
    regeneration is still better than none, and the caller diffs the result.
    """
    placed = False
    for root in roots:
        tmp = Path(tempfile.mkdtemp())
        try:
            if not _eval_root(root, contract_dir, tmp):
                print(
                    f"::warning::pkl eval failed after {EVAL_MAX_ATTEMPTS} attempts "
                    f"for {root.name} — its artifacts are left unchanged."
                )
                continue
            target = f"{GENERATED_DIR}/{root.stem}"
            if swap_outputs(tmp, generated_dir=target):
                placed = True
                print(f"Regenerated {target} from {root.name}.")
            else:
                print(f"::warning::swap refused the output layout for {root.name}.")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if placed:
        run_post_generate(contract_dir)
        _format_generated()
    return placed


def regenerate(contract_dir: str) -> bool:
    """Regenerate contract artifacts; swap gated on eval+format success.

    Eval runs into a temp dir; the working tree is only touched once eval (and
    formatting) succeed. The swap itself is best-effort (rmtree + copytree), not
    crash-safe — but the caller commits only after this returns, so a failed or
    killed run commits nothing and never publishes a half-regenerated tree.

    An app shipping ``contract/post-generate.sh`` gets it run after the swap and
    before formatting — see ``pkl_contract_layout.run_post_generate``.

    Returns True only when the working tree was actually updated with fresh
    artifacts. Returns False when there is no contract to generate from, when
    ``pkl eval`` still fails after ``EVAL_MAX_ATTEMPTS`` attempts, or when the
    swap refused the output layout — never raises on any of those, so a bad
    regen cannot fail the job. How to degrade (lock-only sync, red gate) is the
    caller's decision, not this function's.
    """
    app_pkl = Path(contract_dir) / "app.pkl"
    if not app_pkl.exists():
        roots = eval_roots(contract_dir)
        if roots:
            print(
                f"::notice::No {app_pkl}; regenerating {len(roots)} per-entrypoint "
                f"root(s): {', '.join(r.name for r in roots)}."
            )
            return regenerate_multi_root(contract_dir, roots)
        print(
            f"::notice::No {app_pkl} — skipping artifact regeneration (re-resolve only)."
        )
        return False

    tmp = Path(tempfile.mkdtemp())
    baseline_work: Path | None = None
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

        baseline_out, baseline_work = _baseline_output(contract_dir)
        if not swap_outputs(tmp, baseline_dir=baseline_out):
            # swap_outputs already warned with the specific reason.
            return False
        run_post_generate(contract_dir)
        _format_generated()
        print("Regenerated contract artifacts.")
        return True
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if baseline_work is not None:
            shutil.rmtree(baseline_work, ignore_errors=True)


def _baseline_output(contract_dir: str) -> tuple[Path | None, Path | None]:
    """Evaluate the contract at its pre-bump pin, for override detection.

    Returns ``(eval_output_dir, workdir_to_clean)``; the output is None whenever
    there is no baseline to compute or producing it failed, which turns override
    detection off and makes the swap overwrite everything the eval emitted (the
    behaviour without this function). Never raises and never fails the sync — a
    missing baseline must not block a dependency bump.

    The baseline eval is a *second* `pkl eval`, against the toolkit version the
    committed artifacts came from, so it fetches an older package (a few seconds
    on a cold runner). Worth it: comparing committed content against it is what
    lets every app's post-processed artifacts survive regeneration with no per-app
    declaration at all.
    """
    ref = baseline_contract_ref(contract_dir)
    if ref is None:
        # Common and fine: no pin change in flight, so nothing to protect.
        return (None, None)

    work = Path(tempfile.mkdtemp())
    if not export_contract_at(ref, contract_dir, work):
        print(
            f"::warning::Could not export {contract_dir}/ at {ref[:12]} — "
            "app-maintained generated files cannot be detected, so regeneration "
            "will overwrite them. Check the diff for reverted post-processing."
        )
        return (None, work)

    out = work / "out"
    out.mkdir()
    base_contract = work / contract_dir
    result = run(
        [
            "pkl",
            "eval",
            "--project-dir",
            str(base_contract),
            "-m",
            str(out),
            str(base_contract / "app.pkl"),
        ]
    )
    if result.returncode != 0:
        print(
            "::warning::Baseline pkl eval (pre-bump toolkit pin) failed — "
            "app-maintained generated files cannot be detected, so regeneration "
            "will overwrite them. Check the diff for reverted post-processing."
        )
        return (None, work)
    return (out, work)


def _format_generated() -> None:
    """ruff-fix + format every generated *.py in the working tree (post-swap),
    mirroring contract-toolkit/scripts/regenerate-all.sh. The contract emits
    more than _input.py (e.g. _e2e_base.py, _e2e_credential.py,
    _e2e_substitutions.py); every one must match what pre-commit's ruff would
    produce, or the consumer's pre-commit reformats it on the renovate PR and
    fails CI. This sync commit bypasses pre-commit, so we format here.
    Best-effort: a ruff hiccup must not fail the sync (many apps exclude
    app/generated from lint entirely).

    Runs after `swap_outputs`, on the real `app/generated/**` path relative
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
