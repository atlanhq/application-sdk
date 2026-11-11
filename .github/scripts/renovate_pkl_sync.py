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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# Placement of eval output is family-dependent and shared with
# regenerate_contract.py / check_generated_freshness.py — see that module.
sys.path.insert(0, str(Path(__file__).parent))
from pkl_contract_layout import (  # noqa: E402
    CALLER_NODE_PATHSPEC,
    GENERATED_DIR,
    ROOT_FILES,
    TESTS_GENERATED_DIR,
    format_generated_python,
    root_files_not_emitted,
    run_post_generate,
    swap_outputs,
)

# Lock file produced by `pkl project resolve`.
LOCK_PATH = "contract/PklProject.deps.json"

# Everything a contract can emit, relative to the repo root, as git pathspecs.
# Mirrors the cleanup list in contract-toolkit/scripts/regenerate-all.sh.
# CALLER_NODE_PATHSPEC names a system app's caller nodes only (FND-3563): the
# rest of contract/generated/ is the app's.
# TESTS_GENERATED_DIR is the e2e harness, emitted under tests/ (FND-4656).
OUTPUT_PATHS = [GENERATED_DIR, TESTS_GENERATED_DIR, CALLER_NODE_PATHSPEC, *ROOT_FILES]

# `pkl eval` can fail transiently on a cold CI runner while fetching the remote
# @app-contract-toolkit package — a network blip returns a non-zero code (not an
# OSError). Retry a few times before giving up, so a transient fetch failure
# neither degrades a renovate sync to lock-only nor turns the freshness gate
# red. A deterministically-broken contract simply fails every attempt (a few
# extra seconds) and still returns False. Shared by both callers on purpose.
EVAL_MAX_ATTEMPTS = 3
EVAL_RETRY_SLEEP_S = 5.0

# What Renovate commits from the post-upgrade working tree: the toolkit rule's
# ``fileFilters`` in renovate-config/default.json, which must equal
# ``renovate_file_filters()`` (pinned by test_renovate_pkl_sync.py). Regeneration
# writes under app/ (the generated dir, plus whatever contract/post-generate.sh
# places beside the app code), contract/ (the lock, caller nodes) and tests/,
# and the root YAMLs. A fixed list rather than ``**/*`` keeps unrelated churn
# such as uv.lock out; a change regeneration makes anywhere else is warned about
# (``warn_uncommitted_changes``).
COMMIT_DIRS = ("app", "contract", "tests")
COMMIT_FILES = ROOT_FILES
_MAX_LISTED_PATHS = 20

COMMIT_MESSAGE_REGEN = (
    "chore: sync Pkl deps and regenerate contract artifacts for app-contract-toolkit"
)
COMMIT_MESSAGE_LOCK_ONLY = (
    "chore: sync PklProject.deps.json with updated app-contract-toolkit"
)


def run(cmd: list[str], *, check: bool = False) -> "subprocess.CompletedProcess[str]":
    """Run a subprocess. Single seam so tests can stub pkl/ruff and let git run
    for real against a throwaway repo."""
    return subprocess.run(cmd, check=check, text=True)


def run_capture(
    cmd: list[str], *, check: bool = False
) -> "subprocess.CompletedProcess[str]":
    """Run a subprocess and capture its output: the seam for the git queries
    whose stdout or exit code the sync reads (snapshot, rollback, staging), so
    tests can stub or fault-inject them the same way they stub ``run``."""
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


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


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def _eval_root(
    root: Path, contract_dir: str, out: Path, run_cmd: Runner | None = None
) -> bool:
    """One `pkl eval -m` with the shared retry. True iff it produced output."""
    run_cmd = run_cmd or run
    cmd = ["pkl", "eval", "--project-dir", contract_dir, "-m", str(out), str(root)]
    result = run_cmd(cmd)
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
        result = run_cmd(cmd)
    return result.returncode == 0


def regenerate_multi_root(
    contract_dir: str,
    roots: list[Path],
    run_cmd: Runner | None = None,
) -> RegenerationStatus:
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

    Returns "ok" iff at least one root regenerated. A root that fails eval or
    whose swap refuses the layout is warned about and skipped — a partial
    regeneration is still better than none, and the caller diffs the result.
    A failed ``contract/post-generate.sh`` rolls every root back.
    """
    snapshot = dirty_tree()
    placed = False
    for root in roots:
        tmp = Path(tempfile.mkdtemp())
        try:
            if not _eval_root(root, contract_dir, tmp, run_cmd):
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
    if not placed:
        return "eval-failed"
    if not run_post_generate(contract_dir):
        restore_tree(snapshot)
        return "post-generate-failed"
    _format_generated(run_cmd)
    return "ok"


RegenerationStatus = Literal[
    "ok", "no-contract", "eval-failed", "swap-refused", "post-generate-failed"
]


@dataclass(frozen=True)
class Regeneration:
    """Outcome of one ``regenerate_tree`` call.

    ``placed`` is True only when fresh artifacts are in the working tree.
    ``missing_root_files`` lists the root YAMLs the tree carries but this eval
    did not emit (single-root contracts only; see
    ``pkl_contract_layout.root_files_not_emitted``). ``formatted`` is False when
    no ruff was available, so the generated Python is raw pkl output."""

    status: RegenerationStatus
    missing_root_files: tuple[str, ...] = ()
    formatted: bool = True

    @property
    def placed(self) -> bool:
        return self.status == "ok"


def regenerate_tree(
    contract_dir: str,
    *,
    run_cmd: Runner | None = None,
) -> Regeneration:
    """THE contract regeneration, shared by every SDK entry point.

    Renovate's toolkit bump (``main`` here), the freshness gate
    (``check_generated_freshness``), pre-test regeneration
    (``regenerate_contract``) all regenerate through this function, so none
    of them can disagree about what a regenerated tree looks like (FND-4736).
    Certify's contract-drift check still runs the app's ``poe generate``;
    FND-4244 moves it onto this function.
    ``.github/scripts/tests/test_regeneration_parity.py`` pins that every
    caller reaches it.

    Steps: ``pkl eval`` into a temp dir (with ``EVAL_MAX_ATTEMPTS`` retries),
    ``swap_outputs`` into the working tree, ``contract/post-generate.sh`` if the
    app ships one, then ruff on generated Python. The working tree is touched
    only once eval succeeds, and a failed post-generate step rolls the swap
    back (``restore_tree``), so a regeneration is either the app's complete
    output or nothing. The swap itself is best-effort (rmtree +
    copytree), not crash-safe, so a caller that commits must commit only after
    this returns.

    *run_cmd* is the caller's subprocess seam (each script's tests stub their
    own module's ``run``); None means this module's ``run``.

    There is deliberately no override protection: an app that installs its own
    version of a generated file does it in ``contract/post-generate.sh``, which
    runs here, so the result is exactly what certify checks once it
    regenerates through this function (FND-4244).

    Never raises on a bad contract: a missing contract, a failed eval and a
    refused swap are all reported in the result. How to degrade (lock-only sync,
    a red gate, tests on the committed manifest) is the caller's decision.
    """
    run_cmd = run_cmd or run
    app_pkl = Path(contract_dir) / "app.pkl"
    if not app_pkl.exists():
        roots = eval_roots(contract_dir)
        if not roots:
            print(
                f"::notice::No {app_pkl} — skipping artifact regeneration "
                "(re-resolve only)."
            )
            return Regeneration("no-contract")
        print(
            f"::notice::No {app_pkl}; regenerating {len(roots)} per-entrypoint "
            f"root(s): {', '.join(r.name for r in roots)}."
        )
        return Regeneration(regenerate_multi_root(contract_dir, roots, run_cmd))

    tmp = Path(tempfile.mkdtemp())
    try:
        # --project-dir: the contract is a Pkl project declaring
        # app-contract-toolkit as a *remote package*, so eval must load that
        # project to resolve the `@app-contract-toolkit` import. -m writes each
        # output key relative to the output base.
        if not _eval_root(app_pkl, contract_dir, tmp, run_cmd):
            print(
                f"::warning::pkl eval failed after {EVAL_MAX_ATTEMPTS} attempts "
                "— generated artifacts left unchanged."
            )
            return Regeneration("eval-failed")

        snapshot = dirty_tree()
        if not swap_outputs(tmp):
            # swap_outputs already warned with the specific reason.
            return Regeneration("swap-refused")
        missing = tuple(root_files_not_emitted(tmp))
        if not run_post_generate(contract_dir):
            # Fail closed: raw output without the app's step is not its real
            # output, so put the tree back exactly as it was.
            restore_tree(snapshot)
            print(
                "::warning::contract/post-generate.sh failed — regeneration rolled "
                "back; the working tree is as it was before regenerating."
            )
            return Regeneration("post-generate-failed")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    formatted = _format_generated(run_cmd)
    print("Regenerated contract artifacts.")
    return Regeneration("ok", missing_root_files=missing, formatted=formatted)


def regenerate(contract_dir: str) -> bool:
    """``regenerate_tree`` with Renovate's settings, as a bool: True only when
    the working tree was actually updated with fresh artifacts. Kept as the
    stable entry point that the freshness gate imports, and that certify's
    contract-drift check imports from FND-4244."""
    return regenerate_tree(contract_dir).placed


def _format_generated(run_cmd: Runner | None = None) -> bool:
    """ruff-fix + format every generated *.py in the working tree (post-swap).
    The contract emits more than _input.py (e.g. _e2e_base.py,
    _e2e_credential.py); every one must match what pre-commit's ruff would
    produce, or the consumer's pre-commit reformats it on the renovate PR and
    fails CI. This sync commit bypasses pre-commit, so we format here.

    Runs after `swap_outputs`, on the real `app/generated/**` path relative to
    cwd (the consumer repo root) — not the temp eval output dir. The exact ruff
    calls live in ``pkl_contract_layout.format_generated_python``, shared with
    ``regenerate_contract.py`` so the freshness gate and local regeneration
    cannot disagree about formatting (FND-3560). Best-effort: a ruff hiccup or
    a missing ruff must not fail the sync. Returns False when no ruff was
    available (the Python is raw pkl output)."""
    return format_generated_python(run_cmd or run)


def renovate_file_filters() -> list[str]:
    """The toolkit rule's ``fileFilters``, in the order default.json lists them."""
    return [*(f"{d}/**" for d in COMMIT_DIRS), *COMMIT_FILES]


def committable(path: str) -> bool:
    """Whether Renovate commits *path* (repo-relative, POSIX) from this task."""
    return path in COMMIT_FILES or (
        "/" in path and path.split("/", 1)[0] in COMMIT_DIRS
    )


FileState = tuple[bytes, int]
"""A file's content and ``st_mode``: a rollback restores both, so a swap that
changed only a file's mode (say, dropped its executable bit) is undone too."""

TreeState = dict[str, FileState | None]


def dirty_tree() -> TreeState:
    """Every path ``git status --untracked-files=all`` reports, with its
    content and mode (None when deleted). Snapshot before regeneration and compare with
    ``changed_since`` to get what regeneration changed, so edits Renovate made
    first (uv.lock, contract/PklProject) are not attributed to it."""
    proc = run_capture(
        ["git", "status", "--porcelain", "-z", "--untracked-files=all"],
        check=True,
    )
    entries = proc.stdout.split("\0")
    state: TreeState = {}
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if "R" in code or "C" in code:
            i += 1  # the rename source follows as its own field
        p = Path(path)
        state[path] = (p.read_bytes(), p.stat().st_mode) if p.is_file() else None
    return state


def changed_since(before: TreeState) -> list[str]:
    """Paths whose state differs from *before* (now dirty, or newly clean)."""
    after = dirty_tree()
    changed = {
        path
        for path, content in after.items()
        if before.get(path, b"\0clean") != content
    }
    # A path dirty before and clean now was also changed (restored).
    changed |= {path for path in before if path not in after}
    return sorted(changed)


def restore_tree(before: TreeState) -> None:
    """Put every path changed since *before* back the way it was.

    A path dirty before gets its old content and mode back (or is removed
    again if it was deleted). A path clean before is restored from HEAD when HEAD has it,
    and removed when it is new. Used to undo a regeneration whose
    post-generate step failed."""
    for path in changed_since(before):
        target = Path(path)
        if path in before:
            saved = before[path]
            if saved is None:
                target.unlink(missing_ok=True)
            else:
                content, mode = saved
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                target.chmod(mode)
            continue
        in_head = run_capture(["git", "cat-file", "-e", f"HEAD:{path}"]).returncode == 0
        if in_head:
            run(["git", "checkout", "HEAD", "--", path], check=True)
        else:
            target.unlink(missing_ok=True)


def warn_uncommitted_changes(changed: list[str]) -> None:
    """Name every regenerated path Renovate will not commit. Those land in the
    working tree but not in the PR, so certify's whole-tree drift check fails
    on the app's next publish until someone commits them."""
    skipped = [p for p in changed if not committable(p)]
    if not skipped:
        return
    more = len(skipped) - _MAX_LISTED_PATHS
    print(
        f"::warning::Regeneration changed {len(skipped)} path(s) outside what "
        f"this PR commits ({', '.join(renovate_file_filters())}): "
        f"{', '.join(skipped[:_MAX_LISTED_PATHS])}"
        f"{f' (+{more} more)' if more > 0 else ''}. They are not in this PR, so "
        "certify's contract-drift check will fail on publish until they are "
        "committed."
    )


def _git_matches(pathspec: str) -> bool:
    """Whether git tracks, or would add, anything matching *pathspec*.

    ``os.path.exists`` cannot answer for a magic pathspec such as
    ``CALLER_NODE_PATHSPEC``, nor for a tracked file that regeneration deleted
    along with its directory, and ``git add`` fails on a pathspec that matches
    nothing. ``--cached`` keeps a deleted tracked file in the answer, so its
    deletion is staged."""
    proc = run_capture(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            pathspec,
        ]
    )
    return proc.returncode == 0 and bool(proc.stdout.strip())


def stage_and_commit(message: str, changed: list[str] | None = None) -> bool:
    """Stage the known sync outputs and commit iff something changed.

    Returns True when a commit was made. Stages only the re-resolve /
    generation outputs (``-A`` also captures deletions of orphaned generated
    files) so incidental working-tree noise (e.g. a ruff cache) is never
    committed. *changed* is what this run changed (``changed_since``); its
    paths that Renovate would commit are staged too, so a file
    ``contract/post-generate.sh`` writes outside ``OUTPUT_PATHS`` lands here
    exactly as it does in a Renovate PR."""
    run(["git", "config", "user.name", "github-actions[bot]"], check=True)
    run(
        ["git", "config", "user.email", "github-actions[bot]@users.noreply.github.com"],
        check=True,
    )
    regenerated = [p for p in (changed or []) if committable(p)]
    for path in [LOCK_PATH, *OUTPUT_PATHS, *regenerated]:
        if os.path.exists(path) or _git_matches(path):
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

    before = dirty_tree()
    resolve(args.contract_dir)
    regenerated = regenerate(args.contract_dir) if regenerate_enabled else False
    changed = changed_since(before)
    warn_uncommitted_changes(changed)
    if args.no_commit:
        # Renovate commits the fileFilters matches itself; the driver only
        # generates. Leaving git untouched here also keeps the git identity /
        # commit-message concern entirely on the caller side.
        print("--no-commit: staging and commit left to the caller.")
        return 0
    message = COMMIT_MESSAGE_REGEN if regenerated else COMMIT_MESSAGE_LOCK_ONLY
    stage_and_commit(message, changed)
    return 0


if __name__ == "__main__":
    sys.exit(main())
