"""Generate the contract-toolkit baseline from the toolkit's PklProject.

Usage
-----
Regenerate the committed baseline (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-toolkit-baseline

Check whether the committed baseline is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-toolkit-baseline --check

Direct invocation:

    python -m conformance.tools.generate_toolkit_baseline [--sdk-root DIR] [--check]

Design
------
Reads ``contract-toolkit/src/PklProject`` (the toolkit's own package manifest) and
records the canonical package base URI + current published version into the
committed ``data/toolkit_baseline.json``.  Output is deterministic, so ``--check``
is the forcing function that fails CI when the toolkit is bumped but the baseline
is not regenerated in the same PR — keeping K007/K008 honest offline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from conformance.suite.checks._toolkit_baseline import (
    BASELINE_PATH,
    TOOLKIT_PKLPROJECT_RELPATH,
    build_baseline,
    serialize,
)


def _find_sdk_root() -> Path | None:
    """Locate the repo root containing ``contract-toolkit/src/PklProject``."""
    starts = [Path.cwd(), Path(__file__).resolve()]
    for start in starts:
        for parent in [start, *start.parents]:
            if parent.joinpath(*TOOLKIT_PKLPROJECT_RELPATH).is_file():
                return parent
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the contract-toolkit baseline from the toolkit PklProject.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sdk-root",
        type=Path,
        default=None,
        help="Repo root containing contract-toolkit/src/PklProject (default: auto-detected).",
    )
    parser.add_argument(
        "--outfile",
        type=Path,
        default=BASELINE_PATH,
        help=f"Baseline path to write (default: {BASELINE_PATH}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed baseline matches generated output (exit 1 if stale).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    sdk_root: Path | None = args.sdk_root or _find_sdk_root()
    if sdk_root is None or not sdk_root.joinpath(*TOOLKIT_PKLPROJECT_RELPATH).is_file():
        print(
            "error: could not locate contract-toolkit/src/PklProject — pass --sdk-root DIR.",
            file=sys.stderr,
        )
        sys.exit(2)

    baseline = build_baseline(sdk_root)
    content = serialize(baseline)
    outfile: Path = args.outfile

    if args.check:
        if not outfile.exists():
            print(f"MISSING: {outfile}", file=sys.stderr)
            sys.exit(1)
        if outfile.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {outfile}\nRun `uv run atlan-application-sdk-conformance "
                "gen-toolkit-baseline` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(
            f"Baseline up-to-date (toolkit {baseline.latest_version} @ "
            f"{baseline.canonical_base})."
        )
        return

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(content, encoding="utf-8")
    print(f"Wrote {outfile} (toolkit {baseline.latest_version}).")


if __name__ == "__main__":
    main()
