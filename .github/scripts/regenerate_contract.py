#!/usr/bin/env python3
"""Regenerate an app's contract artifacts before tests run (BLDX-1493).

Invoked by the ``regenerate-contract`` composite action from
``connector-integration-tests`` and ``sdr-e2e`` so the ``manifest.json`` the
tests — and the worker container, which COPYs ``app/generated/`` at build time
and reads ``manifest.json`` at runtime — consume is generated from the current
contract + toolkit source rather than a possibly-stale committed
``app/generated/``.

Two modes:

  * **App-level** (default): regenerate from the app's own pinned
    ``@app-contract-toolkit`` version (the committed ``PklProject.deps.json``
    lock). Optionally **warn** (never fail) when the committed
    ``app/generated/`` drifts from freshly-generated output, so a contract
    change that was not regenerated is surfaced without blocking the fleet.

  * **SDK-level** (``--sdk-toolkit-src`` given): override the
    ``app-contract-toolkit`` dependency to a local checkout of the SDK PR's
    ``contract-toolkit/src`` and re-resolve the lock, so a toolkit change in
    the SDK PR is exercised against the *real* connector contract end-to-end.
    Drift is expected in this mode, so the drift check is skipped and an
    ``eval`` failure is **fatal** (the toolkit change does not generate valid
    artifacts for this connector).

Why this is a script and not inlined YAML: it carries the conditional logic
(self-skip, mode branching, eval-failure policy, drift warning) and is
therefore unit-tested in ``.github/scripts/tests/test_regenerate_contract.py``.
Inlined shell with branching cannot be regression-tested (see
``docs/standards/ci.md``). Mirrors ``renovate_pkl_sync.py``.

Safety contract — this can never make CI *worse* than the prior behaviour
(tests reading the committed manifest):

  * Self-skip: no ``contract/app.pkl`` (non-standard layouts) -> exit 0,
    nothing touched.
  * App-level eval failure: warn and leave the committed artifacts untouched
    so tests run against the committed manifest exactly as before.
  * Drift is warn-only in app-level mode; it never fails the job.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

# Everything ``pkl eval -m .`` emits, relative to the repo root. Mirrors the
# cleanup/stage list in contract-toolkit/scripts/regenerate-all.sh and
# renovate_pkl_sync.py.
OUTPUT_PATHS = ["app/generated", "atlan.yaml", "app.yaml"]

# Matches the ``["app-contract-toolkit"]`` dependency entry in a consumer's
# contract/PklProject, in either the block form
#   ["app-contract-toolkit"] { uri = "package://...@x.y.z" }
# or the shorthand assignment form
#   ["app-contract-toolkit"] = "package://...@x.y.z"
# The block alternative assumes no nested braces inside the entry (true for a
# bare ``uri = "..."``), which is the documented consumer format.
TOOLKIT_DEP_RE = re.compile(r'(\["app-contract-toolkit"\]\s*)(\{[^{}]*\}|=\s*[^\n]+)')


def run(cmd: list[str], *, check: bool = False) -> subprocess.CompletedProcess:
    """Run a subprocess. Single seam so tests can stub pkl/uvx and let git run
    for real against a throwaway repo."""
    return subprocess.run(cmd, check=check, text=True)


def override_toolkit(contract_dir: str, toolkit_src: str) -> None:
    """Repoint the ``app-contract-toolkit`` dependency at a local checkout of
    the SDK PR's ``contract-toolkit/src`` (a Pkl *local dependency*), so
    ``pkl eval`` generates from the PR's toolkit instead of the published
    package. Fatal when no entry is found — silently proceeding would test the
    published toolkit and give a false green for an SDK-level change."""
    pkl_project = Path(contract_dir) / "PklProject"
    src = pkl_project.read_text()
    local = Path(toolkit_src).resolve() / "PklProject"
    new, n = TOOLKIT_DEP_RE.subn(lambda m: f'{m.group(1)}= import("{local}")', src)
    if n == 0:
        raise SystemExit(
            f"::error::No app-contract-toolkit dependency entry found in "
            f"{pkl_project} to override for SDK-level testing."
        )
    pkl_project.write_text(new)
    print(f"Overrode app-contract-toolkit -> local {local} (SDK-level toolkit test).")


def resolve(contract_dir: str) -> None:
    """Re-resolve the Pkl lock. Fatal on failure: with a changed (local-override)
    dependency, an unresolved lock means eval would resolve the wrong toolkit."""
    run(["pkl", "project", "resolve", f"{contract_dir}/"], check=True)


def evaluate(contract_dir: str) -> bool:
    """Regenerate artifacts in place via ``pkl eval``. Returns True on success.

    ``--project-dir``: the contract is a Pkl project declaring
    app-contract-toolkit as a dependency, so eval must load that project to
    resolve the ``@app-contract-toolkit`` import. ``-m .`` writes each output
    key (app/generated/**, atlan.yaml, app.yaml) at its natural path relative
    to the repo root — exactly where the Docker build COPYs from and the tests
    read."""
    app_pkl = str(Path(contract_dir) / "app.pkl")
    proc = run(["pkl", "eval", "--project-dir", contract_dir, "-m", ".", app_pkl])
    return proc.returncode == 0


def clean_outputs() -> list[str]:
    """Remove the prior generated outputs before ``pkl eval``, mirroring
    contract-toolkit/scripts/regenerate-all.sh. Overwrite-only regeneration
    leaves files the new contract/toolkit no longer emits (e.g. a removed
    entrypoint's ``<ep>/manifest.json``) in the tree — they would be baked into
    the image and served as if current. Returns the paths that existed (for
    restore-on-failure)."""
    existed = []
    for path in OUTPUT_PATHS:
        if os.path.isdir(path):
            shutil.rmtree(path)
            existed.append(path)
        elif os.path.exists(path):
            os.remove(path)
            existed.append(path)
    return existed


def restore_outputs(paths: list[str]) -> None:
    """Restore previously-cleaned outputs from HEAD so an app-level eval
    failure leaves the committed artifacts exactly as before (the safety
    contract above). Per-path and non-fatal: a path that never existed in HEAD
    simply stays absent."""
    for path in paths:
        run(["git", "checkout", "--", path])


def _format_generated(root: Path) -> None:
    """ruff-fix + format every generated ``*.py``, mirroring
    contract-toolkit/scripts/regenerate-all.sh and renovate_pkl_sync.py, so the
    in-tree artifacts match what the consumer's pre-commit ruff would produce.

    Best-effort: skipped when neither ``uvx`` nor ``ruff`` is on PATH (the e2e
    pre-build invocation runs before ``setup-deps`` installs uv) — unformatted
    but valid generated Python still imports at runtime."""
    gen = root / "app" / "generated"
    if not gen.is_dir():
        return
    py_files = sorted(str(p) for p in gen.rglob("*.py"))
    if not py_files:
        return
    if shutil.which("uvx"):
        prefix = ["uvx", "ruff"]
    elif shutil.which("ruff"):
        prefix = ["ruff"]
    else:
        print("::notice::ruff/uvx not on PATH — skipping generated-Python formatting.")
        return
    run([*prefix, "check", "--fix", "--select", "F401", "--quiet", *py_files])
    run([*prefix, "format", *py_files])


def warn_on_drift() -> bool:
    """Warn (never fail) when the committed contract artifacts differ from the
    freshly-generated ones. Compares the working tree (just regenerated in
    place) against HEAD via ``git status --porcelain`` — unlike ``git diff``,
    that also surfaces a path the contract stopped emitting (deletion) or a
    newly-emitted file (untracked). Returns True when drift was found."""
    drifted = [
        path
        for path in OUTPUT_PATHS
        if subprocess.run(
            ["git", "status", "--porcelain", "--", path],
            capture_output=True,
            text=True,
        ).stdout.strip()
    ]
    if drifted:
        print(
            "::warning::Committed contract artifacts are stale vs contract/app.pkl: "
            + ", ".join(drifted)
            + ". Run `pkl eval -m . contract/app.pkl` (or `poe generate`) and commit "
            "them. Tests are running against the freshly-generated manifest."
        )
        return True
    print("Committed contract artifacts are up to date with contract/app.pkl.")
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract-dir",
        default="contract",
        help="Directory containing PklProject and app.pkl (default: contract).",
    )
    parser.add_argument(
        "--sdk-toolkit-src",
        default="",
        help="Path to a local SDK contract-toolkit/src checkout. When set, "
        "the app-contract-toolkit dependency is overridden to it (SDK-level "
        "test mode). Empty = app-level (use the app's pinned toolkit).",
    )
    parser.add_argument(
        "--check-drift",
        choices=["true", "false"],
        default="true",
        help="'true' (default) to warn (never fail) when committed app/generated "
        "drifts from freshly-generated output. Ignored in SDK-level mode.",
    )
    args = parser.parse_args(argv)

    contract_dir = args.contract_dir
    if not (Path(contract_dir) / "app.pkl").exists():
        print(f"::notice::No {contract_dir}/app.pkl — skipping contract regeneration.")
        return 0

    sdk_mode = bool(args.sdk_toolkit_src)
    if sdk_mode:
        # Override the toolkit, then re-resolve so the lock points at the local
        # source. App-level mode uses the committed lock as-is.
        override_toolkit(contract_dir, args.sdk_toolkit_src)
        resolve(contract_dir)

    cleaned = clean_outputs()

    if not evaluate(contract_dir):
        if sdk_mode:
            print(
                "::error::pkl eval failed using the SDK PR's contract-toolkit — "
                "the toolkit change does not generate valid artifacts for this "
                "connector contract."
            )
            return 1
        restore_outputs(cleaned)
        print(
            "::warning::pkl eval failed — committed contract artifacts restored "
            "unchanged; tests will run against the committed manifest (prior "
            "behaviour)."
        )
        return 0

    if "app/generated" in cleaned and not Path("app/generated").is_dir():
        # Eval "succeeded" but emitted no app/generated at all — leaving the
        # tests with no manifest is strictly worse than a stale one.
        if sdk_mode:
            print(
                "::error::pkl eval with the SDK PR's contract-toolkit emitted no "
                "app/generated/ for this connector contract."
            )
            return 1
        restore_outputs(cleaned)
        print(
            "::warning::pkl eval emitted no app/generated/ — committed contract "
            "artifacts restored; tests will run against the committed manifest "
            "(prior behaviour)."
        )
        return 0

    _format_generated(Path("."))

    if args.check_drift == "true" and not sdk_mode:
        warn_on_drift()

    return 0


if __name__ == "__main__":
    sys.exit(main())
