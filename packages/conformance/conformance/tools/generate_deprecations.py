"""Generate the deprecated-symbol manifest from SDK source.

Usage
-----
Regenerate the committed manifest (normal developer workflow):

    uv run atlan-application-sdk-conformance gen-deprecations

Check whether the committed manifest is up-to-date (CI gate / drift test):

    uv run atlan-application-sdk-conformance gen-deprecations --check

Direct invocation:

    python -m conformance.tools.generate_deprecations [--sdk-root DIR] [--check]

Design
------
Scans ``<sdk-root>/application_sdk`` with the shared B-series extractor, records
every *marked* deprecated symbol, and either writes the canonical JSON manifest
or (with ``--check``) compares it to the committed file.  Output is deterministic
(sorted), so ``--check`` is a reliable staleness gate — the forcing function that
makes a new ``@deprecated`` fail CI until the manifest is regenerated in the same
PR (BLDX-1418).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from conformance.suite.checks.deprecation._manifest import (
    MANIFEST_PATH,
    SDK_IMPORT_ROOT,
    build_manifest,
    serialize,
)


def _find_sdk_root() -> Path | None:
    """Locate the repo root containing ``application_sdk/`` (best-effort).

    Walks up from the current directory and from this file, returning the first
    ancestor that holds an ``application_sdk/__init__.py``.
    """
    starts = [Path.cwd(), Path(__file__).resolve()]
    for start in starts:
        for parent in [start, *start.parents]:
            if (parent / SDK_IMPORT_ROOT / "__init__.py").is_file():
                return parent
    return None


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the deprecated-symbol manifest from SDK source.",
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
        default=MANIFEST_PATH,
        help=f"Manifest path to write (default: {MANIFEST_PATH}).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify the committed manifest matches generated output (exit 1 if stale).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = _parse_args(argv)

    sdk_root: Path | None = args.sdk_root or _find_sdk_root()
    if sdk_root is None or not (sdk_root / SDK_IMPORT_ROOT).is_dir():
        print(
            "error: could not locate application_sdk/ — pass --sdk-root DIR.",
            file=sys.stderr,
        )
        sys.exit(2)

    manifest = build_manifest(sdk_root)
    content = serialize(manifest)
    outfile: Path = args.outfile

    if args.check:
        if not outfile.exists():
            print(f"MISSING: {outfile}", file=sys.stderr)
            sys.exit(1)
        if outfile.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {outfile}\nRun `uv run atlan-application-sdk-conformance "
                "gen-deprecations` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Manifest up-to-date ({len(manifest.symbols)} symbols).")
        return

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(content, encoding="utf-8")
    print(f"Wrote {outfile} ({len(manifest.symbols)} symbols).")


if __name__ == "__main__":
    main()
