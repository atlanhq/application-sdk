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

Regeneration *destroys* the committed ``app/generated/``, so anything the app
does to that output which is not ``contract/post-generate.sh`` is dropped —
into the image, silently. ``pkl_contract_layout.warn_unwired_post_generate``
(reached from ``run_post_generate`` on every regeneration path, in both modes)
warns when an app looks like it post-processes elsewhere, and the drift
comparison is now formatting-insensitive so it can also run on the image-build
path, which is the one that bakes the artifacts. See FND-1777.

Eval writes into a temp dir and is placed by ``pkl_contract_layout.swap_outputs``,
which handles both contract families: ``App.pkl`` (output keys prefixed
``app/generated/``) and ``NativeApp.pkl`` / ``NativeAppBundle.pkl`` (unprefixed
keys relative to the generated dir). This used to eval ``-m .`` straight into the
working tree, which only ever placed the prefixed family — a native-family app
scattered its artifacts across the repo root, tripped the
"emitted no app/generated" guard, and had its committed artifacts restored, so
tests silently ran against the committed manifest and BLDX-1493 did nothing for
most of the fleet. Temp-dir eval also means nothing is touched until the output
is known-good, so the clean/restore dance the in-place version needed is gone.
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Placement of eval output is family-dependent and shared with
# renovate_pkl_sync.py / check_generated_freshness.py — see that module.
sys.path.insert(0, str(Path(__file__).parent))
from pkl_contract_layout import (  # noqa: E402
    GENERATED_DIR,
    ROOT_FILES,
    run_post_generate,
    swap_outputs,
)

# Everything a contract can emit, relative to the repo root. Mirrors the
# cleanup/stage list in contract-toolkit/scripts/regenerate-all.sh and
# renovate_pkl_sync.py.
OUTPUT_PATHS = [GENERATED_DIR, *ROOT_FILES]

# Toolkit properties that switch a root file's emission off. Setting one to
# ``false`` is documented toolkit surface — ``contract-toolkit/src/App.pkl``
# (``emitAtlanYaml``:217, ``emitAppYaml``:228), ``NativeAppBundle.pkl`` for
# ``emitAtlanYaml``, and ``contract-toolkit/docs/reference.md`` — so a committed
# copy of an opted-out file is hand-maintained, never an artifact the toolkit
# lost. See ``opted_out_root_files``.
ROOT_FILE_EMIT_FLAGS = {"atlan.yaml": "emitAtlanYaml", "app.yaml": "emitAppYaml"}

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


def run_capture(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run a subprocess and capture stdout. A second seam rather than a flag on
    ``run`` because the emit-flag probe is the only caller that reads output —
    ``run`` deliberately streams pkl's diagnostics straight into the job log."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


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


def evaluate(contract_dir: str, out_dir: Path) -> bool:
    """Evaluate the contract into ``out_dir``. Returns True on success.

    ``--project-dir``: the contract is a Pkl project declaring
    app-contract-toolkit as a dependency, so eval must load that project to
    resolve the ``@app-contract-toolkit`` import. ``-m out_dir`` writes each
    output key relative to that base; ``swap_outputs`` then places it in the
    working tree, where the Docker build COPYs from and the tests read.

    Evaluating into a temp dir rather than in place means the committed artifacts
    are untouched unless the output is usable — the safety contract above holds
    without needing to clean-then-restore around a failure."""
    app_pkl = str(Path(contract_dir) / "app.pkl")
    proc = run(
        ["pkl", "eval", "--project-dir", contract_dir, "-m", str(out_dir), app_pkl]
    )
    return proc.returncode == 0


def root_files_not_emitted(out_dir: Path) -> list[str]:
    """Root YAMLs the working tree carries that this eval did not emit.

    Not a broken tree — ``swap_outputs`` only ever copies root files, so the
    committed one is still in place — but it *may* mean the contract (or the
    toolkit under test) stopped producing an artifact the app ships. Worth
    failing on in SDK-level mode: a later sdr-e2e step hard-errors on a missing
    root ``app.yaml``, resolves it *after* this step, and SDK-level mode skips
    the drift check, so nothing else would explain it. Informational in
    app-level mode.

    "May", because a contract can also *ask* the toolkit not to emit the file.
    That is a sanctioned configuration, not a regression, so callers split this
    list with ``opted_out_root_files`` before reporting — see FND-1723."""
    return [
        name
        for name in ROOT_FILES
        if Path(name).exists() and not (out_dir / name).exists()
    ]


def opted_out_root_files(contract_dir: str, names: list[str]) -> set[str]:
    """Of ``names``, the root files this contract told the toolkit not to emit.

    Answers "would this toolkit emit the file?" rather than "did it". Probed
    with ``pkl eval -x <flag>`` against the same ``--project-dir`` the main eval
    uses, so an SDK-level run reads the flag off the *overridden* toolkit — the
    one whose output we are judging.

    Anything other than a clean ``false`` means "not opted out", which is the
    conservative answer: it leaves the finding in place. That covers a contract
    family without the property (``NativeApp.pkl`` has neither flag,
    ``NativeAppBundle.pkl`` only ``emitAtlanYaml``), where the probe exits
    non-zero, as well as a toolkit that removed the flag entirely. A probe can
    therefore never manufacture a green; it can only withdraw a finding the app
    explicitly asked for."""
    opted_out = set()
    app_pkl = str(Path(contract_dir) / "app.pkl")
    for name in names:
        flag = ROOT_FILE_EMIT_FLAGS.get(name)
        if flag is None:
            continue
        proc = run_capture(
            ["pkl", "eval", "--project-dir", contract_dir, "-x", flag, app_pkl]
        )
        if proc.returncode == 0 and (proc.stdout or "").strip() == "false":
            opted_out.add(name)
    return opted_out


def _format_generated(root: Path) -> bool:
    """ruff-fix + format every generated ``*.py``, mirroring
    contract-toolkit/scripts/regenerate-all.sh and renovate_pkl_sync.py, so the
    in-tree artifacts match what the consumer's pre-commit ruff would produce.

    Best-effort: skipped when neither ``uvx`` nor ``ruff`` is on PATH (the e2e
    pre-build invocation runs before ``setup-deps`` installs uv) — unformatted
    but valid generated Python still imports at runtime.

    Returns True iff the generated Python in the tree is formatted (nothing to
    format counts). ``warn_on_drift`` needs this: unformatted generated Python
    reads as drift against a committed tree the app's pre-commit did format, and
    that false positive is the whole reason the image-build path used to skip the
    drift comparison outright (FND-1777)."""
    gen = root / "app" / "generated"
    if not gen.is_dir():
        return True
    py_files = sorted(str(p) for p in gen.rglob("*.py"))
    if not py_files:
        return True
    if shutil.which("uvx"):
        prefix = ["uvx", "ruff"]
    elif shutil.which("ruff"):
        prefix = ["ruff"]
    else:
        print("::notice::ruff/uvx not on PATH — skipping generated-Python formatting.")
        return False
    run([*prefix, "check", "--fix", "--select", "F401", "--quiet", *py_files])
    run([*prefix, "format", *py_files])
    return True


def _porcelain_paths(path: str) -> list[tuple[str, str]]:
    """``(status, path)`` for everything git reports under ``path``.

    ``git status --porcelain`` rather than ``git diff``, so a path the contract
    stopped emitting (deletion) and a newly-emitted one (untracked) both show up.
    ``-z`` because a generated filename may contain anything; a rename entry is
    ``XY new\\0old\\0``, and the new path is the one we report."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "-z", "--", path],
        capture_output=True,
        text=True,
    )
    out: list[tuple[str, str]] = []
    fields = proc.stdout.split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status, name = entry[:2], entry[3:]
        out.append((status, name))
        if "R" in status or "C" in status:
            i += 1  # skip the paired original path
    return out


def _formatting_only(status: str, name: str) -> bool:
    """Whether this entry could have been produced by *skipped* ruff formatting
    alone, and so must not be reported as drift when formatting did not run.

    Only a *modification* to a generated ``*.py``: formatting rewrites bytes in
    files that already exist, so it can never add (``??``), delete or rename one
    — and it never touches ``manifest.json``, the configmaps or the root YAMLs,
    which is where a dropped post-processing step or an unregenerated contract
    shows up. Narrowing to exactly this class is what lets the image-build path
    compare at all (FND-1777), instead of trading the whole signal away for one
    false positive."""
    return name.endswith(".py") and status.strip() in {"M", "MM", "AM"}


def warn_on_drift(*, formatted: bool = True) -> bool:
    """Warn (never fail) when the committed contract artifacts differ from the
    freshly-generated ones. Returns True when drift was found.

    ``formatted=False`` (generated-Python formatting was skipped — see
    ``_format_generated``) narrows the comparison to the classes formatting
    cannot fabricate, rather than skipping it."""
    drifted: list[str] = []
    suppressed = 0
    for path in OUTPUT_PATHS:
        entries = _porcelain_paths(path)
        if not formatted:
            suppressed += sum(1 for s, n in entries if _formatting_only(s, n))
            entries = [(s, n) for s, n in entries if not _formatting_only(s, n)]
        if entries:
            drifted.append(path)
    if suppressed:
        print(
            f"::notice::{suppressed} generated Python file(s) differ from the "
            "committed tree but generated-Python formatting was skipped on this "
            "path (no uvx/ruff), so the difference is not reported as drift. "
            "Everything else — manifest.json, configmaps, root YAMLs — is still "
            "compared."
        )
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
        "drifts from freshly-generated output. Ignored in SDK-level mode. The "
        "comparison is formatting-insensitive when ruff was unavailable, so "
        "'false' is rarely needed (FND-1777).",
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

    # Nothing in the working tree is touched until the output is known usable, so
    # every failure path below simply leaves the committed artifacts in place —
    # no clean-then-restore, and a committed root atlan.yaml/app.yaml this
    # contract does not emit is never left deleted (swap_outputs only ever
    # *copies* root files it was given).
    tmp = Path(tempfile.mkdtemp())
    try:
        if not evaluate(contract_dir, tmp):
            if sdk_mode:
                print(
                    "::error::pkl eval failed using the SDK PR's contract-toolkit — "
                    "the toolkit change does not generate valid artifacts for this "
                    "connector contract."
                )
                return 1
            print(
                "::warning::pkl eval failed — committed contract artifacts left "
                "unchanged; tests will run against the committed manifest (prior "
                "behaviour)."
            )
            return 0

        # swap_outputs warns with the specific reason (no generated artifacts in
        # the output, or a generated dir it declined to write to).
        if not swap_outputs(tmp):
            if sdk_mode:
                print(
                    "::error::pkl eval with the SDK PR's contract-toolkit produced "
                    "no usable contract artifacts for this connector contract."
                )
                return 1
            print(
                "::warning::Committed contract artifacts left unchanged; tests will "
                "run against the committed manifest (prior behaviour)."
            )
            return 0

        # Committed-but-not-emitted splits two ways: the toolkit stopped
        # producing a file the app ships (the regression this check exists for)
        # and the app told the toolkit not to produce it (a sanctioned config).
        # The probe only runs when there is something to explain.
        missing_roots = root_files_not_emitted(tmp)
        opted_out = opted_out_root_files(contract_dir, missing_roots)
        if opted_out:
            print(
                "::notice::"
                + ", ".join(
                    f"{name} not emitted because this contract sets "
                    f"{ROOT_FILE_EMIT_FLAGS[name]} = false"
                    for name in sorted(opted_out)
                )
                + " — the committed file(s) are hand-maintained and left in "
                "place, not a lost artifact."
            )
        stale_roots = [name for name in missing_roots if name not in opted_out]
        if stale_roots:
            if sdk_mode:
                print(
                    "::error::pkl eval with the SDK PR's contract-toolkit did not "
                    "emit " + ", ".join(stale_roots) + " for this connector "
                    "contract — the committed file(s) are left in place, but the "
                    "toolkit no longer produces them."
                )
                return 1
            print(
                "::warning::pkl eval did not re-emit "
                + ", ".join(stale_roots)
                + " — the committed file(s) are left in place (this contract does "
                "not emit them)."
            )

        run_post_generate(contract_dir)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    formatted = _format_generated(Path("."))

    # Still skipped in SDK-level mode: a toolkit change legitimately changes the
    # output, so every SDK-dispatched run would warn and the signal would mean
    # nothing. The class of drift that IS a bug there — the committed tree holds
    # a transformation the fresh output does not — is caught mode-independently
    # by run_post_generate's unwired-post-processing warning above (FND-1777).
    if args.check_drift == "true" and not sdk_mode:
        warn_on_drift(formatted=formatted)

    return 0


if __name__ == "__main__":
    sys.exit(main())
