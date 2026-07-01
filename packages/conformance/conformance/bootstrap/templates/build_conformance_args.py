"""Build the `detect` argument list for atlan-application-sdk-conformance.

Reads EXCLUDE_PATHS and EXIT_ZERO from the environment (set by GitHub Actions
``env:`` blocks) and prints one argument per line to stdout.  Callers do:

    mapfile -t detect_args < <(python .github/scripts/build_conformance_args.py \\
      --series C --slug ci)
    uvx atlan-application-sdk-conformance detect "${detect_args[@]}"

This keeps all conditional argument-building logic in a tested Python script
rather than inlined in YAML (per docs/standards/ci.md).
"""

from __future__ import annotations

import argparse
import os
import sys


def build_args(
    series: str,
    slug: str,
    *,
    exclude: str = "",
    exit_zero: bool = False,
) -> list[str]:
    result = ["--repo", ".", "--series", series, "--output", f"{slug}.sarif"]
    if exclude:
        result += ["--exclude", exclude]
    if exit_zero:
        result.append("--exit-zero")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", required=True, help="Conformance series letter(s)")
    parser.add_argument("--slug", required=True, help="Series slug for SARIF filename")
    args = parser.parse_args(argv)

    exclude = os.environ.get("EXCLUDE_PATHS", "")
    exit_zero = os.environ.get("EXIT_ZERO", "").lower() == "true"

    detect_args = build_args(
        args.series, args.slug, exclude=exclude, exit_zero=exit_zero
    )
    print("\n".join(detect_args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
