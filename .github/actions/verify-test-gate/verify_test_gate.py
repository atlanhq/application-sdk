"""Evaluate the Tests Gate: pass/fail from the tests + e2e job results.

The aggregator gate is the single required check for branch protection. It
passes iff:

  * ``tests`` (unit + integration) succeeded, AND
  * e2e discovery succeeded OR was skipped (skipped = e2e not requested; a
    *failed* discovery means e2e was requested but no suites were found), AND
  * the e2e matrix succeeded OR was skipped (skipped = not requested / discovery
    skipped; the matrix aggregate is success only if every leg passed).

Extracted from an inline ``if [ ... ]`` gate into this tested driver per
``docs/standards/ci.md`` once the gate grew past a single check. Co-located with
the composite action — NOT under ``.github/scripts/`` — so it is checked out
alongside the action when the reusable runs in a consumer repo.
"""

from __future__ import annotations

import argparse
import sys

# Job results that are acceptable for the e2e path. ``skipped`` covers both
# "e2e not requested" and "discovery skipped"; everything else (failure,
# cancelled, …) fails the gate.
_OK_OPTIONAL = ("success", "skipped")


def evaluate(tests: str, discover_e2e: str, e2e: str) -> list[str]:
    """Return human-readable failure reasons (empty ⇒ the gate passes)."""
    errors: list[str] = []
    if tests != "success":
        errors.append(f"tests job did not succeed (result={tests})")
    if discover_e2e not in _OK_OPTIONAL:
        errors.append(f"e2e discovery did not succeed (result={discover_e2e})")
    if e2e not in _OK_OPTIONAL:
        errors.append(f"one or more e2e suites did not succeed (result={e2e})")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate the Tests Gate.")
    parser.add_argument("--tests", required=True, help="needs.tests.result")
    parser.add_argument(
        "--discover-e2e", required=True, help="needs.discover-e2e.result"
    )
    parser.add_argument("--e2e", required=True, help="needs.e2e.result")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    errors = evaluate(args.tests, args.discover_e2e, args.e2e)
    for reason in errors:
        print(f"::error::{reason}", file=sys.stderr)
    if errors:
        return 1
    print("All test jobs passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
