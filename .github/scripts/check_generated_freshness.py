#!/usr/bin/env python3
"""Generated-artifact freshness check driver (BLDX-1414).

Invoked by ``.github/workflows/generated-freshness.yaml`` on app pull requests.
Regenerates the contract artifacts from the *committed* ``contract/app.pkl`` +
lock and fails if the working tree changed — i.e. the committed
``app/generated/**`` / ``atlan.yaml`` / ``app.yaml`` are stale or were
hand-edited relative to what ``pkl eval`` produces today.

This is the CI half of BLDX-1414.  It is the only check that can prove *content*
freshness: the static conformance rules K003/K004/K005 catch a stale lock, a
missing output, or a stripped provenance banner, but a hand-edit that keeps the
banner is invisible to them — only regenerate-and-diff catches it.  Lock drift is
owned by K003, so this gate does **not** re-resolve; it regenerates from the
committed lock and diffs the outputs.

Why a script and not inlined YAML (per docs/standards/ci.md): it carries the
conditional logic (opt-out gate, missing-contract skip, eval-failure degrade,
drift decision) and is unit-tested in
``.github/scripts/tests/test_check_generated_freshness.py``.

Exit codes:
  * 0 — artifacts are fresh, OR the check is inconclusive/not-applicable
        (no contract, ``pkl eval`` unavailable/failed), OR opted out
        (``--check-freshness false``).  The gate is non-blocking by design.
  * 1 — drift detected: regeneration changed tracked files or produced untracked
        ones under the generated-output paths.

The regeneration itself is reused verbatim from ``renovate_pkl_sync`` (same
``pkl eval`` invocation, the same ``uvx ruff`` formatting of generated Python,
and the same ``OUTPUT_PATHS``), so a renovate sync and this check can never
disagree about what "regenerated" means.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

# Reuse the exact regeneration primitive + output list the renovate sync uses,
# so "regenerate" means the same thing in both places.
sys.path.insert(0, str(Path(__file__).parent))
from renovate_pkl_sync import OUTPUT_PATHS, regenerate  # noqa: E402


def _changed_output_paths() -> list[str] | None:
    """Return generated-output paths that regeneration changed or created.

    Combines tracked modifications (``git diff``) with untracked additions
    (``git ls-files --others``), both scoped to ``OUTPUT_PATHS``, so a brand-new
    generated file that was never committed is caught alongside edits.

    ``--exclude-standard`` is deliberately omitted from the untracked lookup: a
    ``.gitignore`` rule covering ``OUTPUT_PATHS`` would otherwise hide a
    brand-new generated file from this check, silently defeating the "never
    committed" case this function exists to catch.

    Returns ``None`` if a git invocation fails, so a broken/absent git command
    degrades to inconclusive rather than being silently read as "no changes".
    """
    changed: set[str] = set()
    tracked = subprocess.run(
        ["git", "diff", "--name-only", "--", *OUTPUT_PATHS],
        text=True,
        capture_output=True,
    )
    if tracked.returncode != 0:
        print(
            f"::warning::'git diff' failed ({tracked.stderr.strip()}); freshness check skipped."
        )
        return None
    changed.update(line for line in tracked.stdout.splitlines() if line.strip())

    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--", *OUTPUT_PATHS],
        text=True,
        capture_output=True,
    )
    if untracked.returncode != 0:
        print(
            f"::warning::'git ls-files' failed ({untracked.stderr.strip()}); freshness check skipped."
        )
        return None
    changed.update(line for line in untracked.stdout.splitlines() if line.strip())
    return sorted(changed)


def check_freshness(contract_dir: str = "contract") -> tuple[str, list[str]]:
    """Return ``(status, changed_paths)``.

    ``status`` is one of:
      * ``"clean"``  — regeneration produced no changes.
      * ``"drift"``  — regeneration changed/created output files (``changed_paths``).
      * ``"na"``     — nothing to check: no ``contract/app.pkl``, ``pkl eval``
                       could not run / failed, or a ``git`` invocation failed
                       (all inconclusive, treated as pass).
    """
    if not (Path(contract_dir) / "app.pkl").exists():
        print(f"::notice::No {contract_dir}/app.pkl — no generated artifacts to check.")
        return ("na", [])

    try:
        regenerated = regenerate(contract_dir)
    except OSError as exc:
        # pkl / uvx not installed or not runnable — inconclusive, never block.
        print(f"::warning::Could not run pkl eval ({exc}); freshness check skipped.")
        return ("na", [])

    if not regenerated:
        # regenerate() already logged the reason (eval failed / no output).
        print("::warning::Regeneration did not run; freshness check inconclusive.")
        return ("na", [])

    changed = _changed_output_paths()
    if changed is None:
        return ("na", [])
    return ("drift" if changed else "clean", changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--contract-dir",
        default="contract",
        help="Directory containing PklProject and app.pkl (default: contract).",
    )
    parser.add_argument(
        "--check-freshness",
        choices=["true", "false"],
        default="true",
        help="'true' (default) to run the check; 'false' to opt out (e.g. an app "
        "with hand-maintained generated config). Mirrors renovate-pkl-sync's "
        "regenerate-contract opt-out.",
    )
    args = parser.parse_args(argv)

    if args.check_freshness != "true":
        print(
            "::notice::Generated-artifact freshness check opted out (check-freshness=false)."
        )
        return 0

    status, changed = check_freshness(args.contract_dir)

    if status == "drift":
        print("::error::Committed generated artifacts are stale or hand-edited.")
        print(
            "The following files differ from a fresh 'pkl eval -m . contract/app.pkl':"
        )
        for path in changed:
            print(f"  - {path}")
        print(
            "Regenerate with 'pkl eval -m . contract/app.pkl' "
            "(or 'uv run poe generate') and commit the result."
        )
        return 1

    if status == "clean":
        print("Generated artifacts are up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
