"""Evaluate the Tests Gate and render its status strings — one source of truth.

The aggregator gate is the single required check for branch protection. This
driver is both the pass/fail authority AND the source of the human-readable
status strings the PR "Tests Summary" table shows, so the displayed status and
the enforced result can never drift.

Gate passes iff:

  * ``unit`` tests succeeded (the per-commit tier; runs on every event), AND
  * ``detect-integration`` (the suite-detection job that gates the integration
    tier) succeeded OR was skipped (skipped = pull_request; a *failed* detection
    must not be indistinguishable from "no suite" — otherwise a checkout/glob
    flake silently skips integration and greens the gate), AND
  * ``integration`` tests succeeded OR were skipped (skipped is legitimate: the
    job is skipped on pull_request — the unit tier is the PR signal — and when
    the connector ships no integration suite), AND
  * e2e discovery succeeded OR was skipped (skipped = e2e not requested; a
    *failed* discovery means e2e was requested but no suites were found), AND
  * the e2e matrix succeeded OR was skipped (matrix aggregate is success only
    if every leg passed).

Note the asymmetry with e2e: ``discover-e2e`` *fails* on count=0 (it only runs
when e2e was explicitly requested, so zero suites is a misconfig), which is why
``discover-e2e == success and e2e == skipped`` is an anomaly. ``detect-
integration`` *succeeds* on count=0 (a connector with no integration suite is
legitimate) and ``integration`` then skips cleanly, so there is no analogous
``detect-integration == success and integration == skipped`` check — that pair
is the normal no-suite path.

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


def evaluate(
    unit: str, integration: str, detect_integration: str, discover_e2e: str, e2e: str
) -> list[str]:
    """Return human-readable failure reasons (empty ⇒ the gate passes)."""
    errors: list[str] = []
    if unit != "success":
        errors.append(f"unit tests did not succeed (result={unit})")
    # detect-integration gates the integration job's `if`. A *failure* there
    # (checkout/glob flake) drops integration to a skip, which the check below
    # would read as a legitimate pass — so a failed detection must fail the gate
    # here, exactly as a failed discover-e2e does. skipped (pull_request) and
    # success (suite present or not) are both valid.
    if detect_integration not in _OK_OPTIONAL:
        errors.append(
            f"integration-suite detection did not succeed (result={detect_integration})"
        )
    # Integration is optional-by-skip: the job is intentionally skipped on
    # pull_request and when the connector has no integration suite. Any result
    # other than success/skipped (failure, cancelled, timed_out) fails the gate.
    if integration not in _OK_OPTIONAL:
        errors.append(f"integration tests did not succeed (result={integration})")
    if discover_e2e not in _OK_OPTIONAL:
        errors.append(f"e2e discovery did not succeed (result={discover_e2e})")
    if e2e not in _OK_OPTIONAL:
        errors.append(f"one or more e2e suites did not succeed (result={e2e})")
    # Defensive: a successful discovery means suites exist (discover-e2e fails on
    # count=0), so the matrix should have run. A skipped matrix here is an
    # anomaly — this driver is consumed cross-repo via @main, so don't let a
    # future caller that re-wires the e2e `if` green the gate by skipping it.
    if discover_e2e == "success" and e2e == "skipped":
        errors.append(
            "e2e discovery succeeded (suites found) but the matrix was skipped"
        )
    return errors


def _unit_status(unit: str) -> str:
    if unit == "success":
        return "✅ Passed"
    if unit == "skipped":
        return "⊘ Skipped"
    return "❌ Failed"


def _integration_status(integration: str, detect_integration: str) -> str:
    # A detection failure drops integration to a skip; surface that as a failure
    # rather than the benign "skipped" string, so the display never claims the
    # tier was cleanly skipped when detection actually broke.
    if detect_integration not in _OK_OPTIONAL:
        return "❌ Integration-suite detection failed"
    if integration == "success":
        return "✅ Passed"
    if integration == "skipped":
        return "⊘ Skipped — PRs, or no integration suite (runs in merge queue)"
    return "❌ Failed"


def _e2e_status(discover_e2e: str, e2e: str) -> str:
    if discover_e2e == "skipped":
        return "⊘ Skipped — add `e2e` label to trigger"
    if discover_e2e == "failure":
        return "❌ No suites discovered (e2e was requested)"
    if e2e == "success":
        return "✅ Passed"
    if discover_e2e == "success" and e2e == "skipped":
        return "❌ Matrix skipped despite discovered suites"
    if e2e == "skipped":
        return "⊘ Skipped"
    return "❌ Failed"


def render(
    unit: str, integration: str, detect_integration: str, discover_e2e: str, e2e: str
) -> dict[str, str]:
    """Compute the gate's outputs: pass/fail + the display status strings."""
    errors = evaluate(unit, integration, detect_integration, discover_e2e, e2e)
    return {
        "passed": "true" if not errors else "false",
        "unit-status": _unit_status(unit),
        "integration-status": _integration_status(integration, detect_integration),
        "e2e-status": _e2e_status(discover_e2e, e2e),
        "overall-status": "✅ All passed" if not errors else "❌ Some failed",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate + render the Tests Gate.")
    parser.add_argument("--unit", required=True, help="needs.unit.result")
    parser.add_argument("--integration", required=True, help="needs.integration.result")
    parser.add_argument(
        "--detect-integration",
        required=True,
        help="needs.detect-integration.result",
    )
    parser.add_argument(
        "--discover-e2e", required=True, help="needs.discover-e2e.result"
    )
    parser.add_argument("--e2e", required=True, help="needs.e2e.result")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    # Annotate each failure reason (shows on the gate step regardless of the
    # zero exit; the job's enforce step turns `passed=false` into a red check).
    for reason in evaluate(
        args.unit,
        args.integration,
        args.detect_integration,
        args.discover_e2e,
        args.e2e,
    ):
        print(f"::error::{reason}", file=sys.stderr)

    for key, value in render(
        args.unit,
        args.integration,
        args.detect_integration,
        args.discover_e2e,
        args.e2e,
    ).items():
        print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
