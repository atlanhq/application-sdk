"""Generate the SDK type-alias table that B005 reads at scan time.

Usage
-----
Regenerate the committed table (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-sdk-type-aliases

Check whether the committed table is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-sdk-type-aliases --check

Design
------
Reads every public module-level type alias under ``application_sdk/`` at
SDK-dev time and writes the committed JSON that B005 uses to expand an alias an
app imports from the SDK.  The suite runs inside consumer app repos, where no
SDK source is installed, so the data must ship baked into the wheel — the same
mechanism as the deprecation manifest, the public-error allowlist and the
contract-toolkit baseline.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from conformance.suite.checks.deprecation._sdk_type_aliases import (
    DATA_PATH,
    SDK_PACKAGE,
    build_sdk_type_aliases,
    serialize,
)


def _find_sdk_root() -> Path | None:
    """Locate the repo root containing the ``application_sdk`` package."""
    starts = [Path.cwd(), Path(__file__).resolve()]
    for start in starts:
        for parent in [start, *start.parents]:
            if parent.joinpath(SDK_PACKAGE, "__init__.py").is_file():
                return parent
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the SDK type-alias table from application_sdk/.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sdk-root",
        type=Path,
        default=None,
        help="Repo root containing application_sdk/ (default: auto-detected).",
    )
    parser.add_argument(
        "--outfile",
        type=Path,
        default=DATA_PATH,
        help=f"Table path to write (default: {DATA_PATH}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed table matches generated output (exit 1 if stale).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    sdk_root: Path | None = args.sdk_root or _find_sdk_root()
    if sdk_root is None or not sdk_root.joinpath(SDK_PACKAGE, "__init__.py").is_file():
        print(
            "error: could not locate application_sdk/ — pass --sdk-root DIR.",
            file=sys.stderr,
        )
        sys.exit(2)

    table = build_sdk_type_aliases(sdk_root)
    content = serialize(table)
    outfile: Path = args.outfile

    if args.check:
        if not outfile.exists():
            print(f"MISSING: {outfile}", file=sys.stderr)
            sys.exit(1)
        if outfile.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {outfile}\nRun `uv run atlan-application-sdk-conformance "
                "gen-sdk-type-aliases` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Type-alias table up-to-date ({len(table)} aliases).")
        return

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(content, encoding="utf-8")
    print(f"Wrote {outfile} ({len(table)} aliases).")


if __name__ == "__main__":
    main()
