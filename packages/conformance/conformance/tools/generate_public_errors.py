"""Generate the public-error allowlist from SDK source.

Usage
-----
Regenerate the committed allowlist (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-public-errors

Check whether the committed allowlist is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-public-errors --check

Design
------
Reads the ``__all__`` of ``application_sdk/errors/__init__.py`` at SDK-dev time
and writes the committed JSON that P043/P045 read at scan time.  The suite runs
inside consumer app repos, where no SDK source and no network are available, so
the data must ship baked into the wheel — the same mechanism as the B-series
deprecation manifest and the contract-toolkit baseline.

The allowlist decides which remediation a finding suggests, never whether it
fires.  A stale file therefore degrades a message rather than changing the gate,
but ``--check`` keeps it honest anyway (CONNECT-970).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from conformance.suite.checks.error_seam._public_error_surface import (
    ALLOWLIST_PATH,
    SDK_ERRORS_INIT_RELPATH,
    build_allowlist,
    serialize,
)


def _find_sdk_root() -> Path | None:
    """Locate the repo root containing ``application_sdk/errors/__init__.py``."""
    starts = [Path.cwd(), Path(__file__).resolve()]
    for start in starts:
        for parent in [start, *start.parents]:
            if parent.joinpath(*SDK_ERRORS_INIT_RELPATH).is_file():
                return parent
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the public-error allowlist from application_sdk.errors.__all__.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--sdk-root",
        type=Path,
        default=None,
        help="Repo root containing application_sdk/errors/__init__.py (default: auto-detected).",
    )
    parser.add_argument(
        "--outfile",
        type=Path,
        default=ALLOWLIST_PATH,
        help=f"Allowlist path to write (default: {ALLOWLIST_PATH}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed allowlist matches generated output (exit 1 if stale).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    sdk_root: Path | None = args.sdk_root or _find_sdk_root()
    if sdk_root is None or not sdk_root.joinpath(*SDK_ERRORS_INIT_RELPATH).is_file():
        print(
            "error: could not locate application_sdk/errors/__init__.py — pass --sdk-root DIR.",
            file=sys.stderr,
        )
        sys.exit(2)

    names = build_allowlist(sdk_root)
    content = serialize(names)
    outfile: Path = args.outfile

    if args.check:
        if not outfile.exists():
            print(f"MISSING: {outfile}", file=sys.stderr)
            sys.exit(1)
        if outfile.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {outfile}\nRun `uv run atlan-application-sdk-conformance "
                "gen-public-errors` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Allowlist up-to-date ({len(names)} public error classes).")
        return

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(content, encoding="utf-8")
    print(f"Wrote {outfile} ({len(names)} public error classes).")


if __name__ == "__main__":
    main()
