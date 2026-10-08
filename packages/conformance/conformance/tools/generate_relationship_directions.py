"""Generate the 1-to-N relationship table that P055 reads at scan time.

Usage
-----
Regenerate the committed table (normal developer workflow):

    uv run --directory packages/conformance --extra test atlan-application-sdk-conformance gen-relationship-directions

Check whether the committed table is up-to-date (CI gate / drift test):

    uv run --directory packages/conformance --extra test atlan-application-sdk-conformance gen-relationship-directions --check

Design
------
Reads relationship cardinality off the installed ``pyatlan_v9`` asset models at
SDK-dev time and writes the committed JSON P055 uses to recognise the list end
of a 1-to-N relationship.  The suite runs inside consumer app repos without
pyatlan, so the data must ship baked into the wheel — the same mechanism as the
deprecation manifest, the public-error allowlist and the SDK type-alias table.
See ``conformance.suite.checks.prescriptions._relationship_directions`` for how
the ends are paired.

Run it from ``packages/conformance`` (as above): the drift test runs there, so
the table must match the pyatlan pinned in that package's ``uv.lock``, which can
differ from the repo root's.  A pyatlan bump that changes a relationship fails
the drift test until the table is regenerated in the same PR.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from conformance.suite.checks.prescriptions._relationship_directions import (
    DATA_PATH,
    build_relationship_data,
    serialize,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the 1-to-N relationship table from pyatlan_v9.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
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

    try:
        data = build_relationship_data()
    except ImportError:
        print(
            "error: pyatlan_v9 is not importable — run from an environment with "
            "the SDK's pinned pyatlan installed (run from packages/conformance with `--extra test`).",
            file=sys.stderr,
        )
        sys.exit(2)
    content = serialize(data)
    outfile: Path = args.outfile
    entries = sum(len(fields) for fields in data.set_ends.values())

    if args.check:
        if not outfile.exists():
            print(f"MISSING: {outfile}", file=sys.stderr)
            sys.exit(1)
        if outfile.read_text(encoding="utf-8") != content:
            print(
                f"STALE: {outfile}\nRun `uv run atlan-application-sdk-conformance "
                "gen-relationship-directions` to update.",
                file=sys.stderr,
            )
            sys.exit(1)
        print(f"Relationship table up-to-date ({entries} list ends).")
        return

    outfile.parent.mkdir(parents=True, exist_ok=True)
    outfile.write_text(content, encoding="utf-8")
    print(f"Wrote {outfile} ({entries} list ends).")


if __name__ == "__main__":
    main()
