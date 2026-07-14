"""Evaluate the Tests Gate and render its status strings — one source of truth.

The aggregator gate is the single required check for branch protection. This
driver is both the pass/fail authority AND the source of the human-readable
status strings the PR "Tests Summary" table shows, so the displayed status and
the enforced result can never drift.

Gate passes iff:

  * ``tests`` (unit + integration) succeeded, AND
  * e2e discovery succeeded OR was skipped (skipped = e2e not requested; a
    *failed* discovery means e2e was requested but no suites were found), AND
  * the e2e matrix succeeded OR was skipped (matrix aggregate is success only
    if every leg passed).

It emits GitHub Actions outputs (``passed`` + per-row/overall status strings)
and, when failing, ``::error::`` annotations. It does NOT exit non-zero — the
gate job enforces via a branch-free ``if: ... outputs.passed != 'true'`` step,
keeping conditional logic out of inline shell per ``docs/standards/ci.md``.

Co-located with the composite action (checked out with it in consumer repos).
"""

from __future__ import annotations

import argparse
import sys

# Job results acceptable for the optional e2e path. ``skipped`` covers both
# "e2e not requested" and "discovery skipped"; anything else fails the gate.
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


def _tests_status(tests: str) -> str:
    if tests == "success":
        return "✅ Passed"
    if tests == "skipped":
        return "⊘ Skipped"
    return "❌ Failed"


def _e2e_status(discover_e2e: str, e2e: str) -> str:
    if discover_e2e == "skipped":
        return "⊘ Skipped — add `e2e` label to trigger"
    if discover_e2e == "failure":
        return "❌ No suites discovered (e2e was requested)"
    if e2e == "success":
        return "✅ Passed"
    if e2e == "skipped":
        return "⊘ Skipped"
    return "❌ Failed"


def render(tests: str, discover_e2e: str, e2e: str) -> dict[str, str]:
    """Compute the gate's outputs: pass/fail + the display status strings."""
    errors = evaluate(tests, discover_e2e, e2e)
    return {
        "passed": "true" if not errors else "false",
        "tests-status": _tests_status(tests),
        "e2e-status": _e2e_status(discover_e2e, e2e),
        "overall-status": "✅ All passed" if not errors else "❌ Some failed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate + render the Tests Gate.")
    parser.add_argument("--tests", required=True, help="needs.tests.result")
    parser.add_argument(
        "--discover-e2e", required=True, help="needs.discover-e2e.result"
    )
    parser.add_argument("--e2e", required=True, help="needs.e2e.result")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Annotate each failure reason (shows on the gate step regardless of the
    # zero exit; the job's enforce step turns `passed=false` into a red check).
    for reason in evaluate(args.tests, args.discover_e2e, args.e2e):
        print(f"::error::{reason}", file=sys.stderr)

    for key, value in render(args.tests, args.discover_e2e, args.e2e).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
