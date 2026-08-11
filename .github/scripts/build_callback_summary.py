#!/usr/bin/env python3
"""Build the summary body for the cross-repo e2e callback (tests-reusable.yaml's
report-to-sdk job).

Moved out of an inlined `run:` block per docs/standards/ci.md: picking which
summary file to use is a branch, which belongs in a tested driver rather than
workflow YAML.

Prefers the e2e leg's already-rendered report (asset/lineage tables etc.,
written by sdr-e2e's PR-comment step) over a plain fallback, which only
applies when e2e was skipped or its artifact never materialised.

BODY ONLY — this script does not decide anything. It used to also emit the
callback's `conclusion`, computed from a strict subset of the Tests Gate's
inputs (no discover-e2e, no image-build legs, no "discovery found suites but
the matrix skipped" anomaly rule), which is exactly how a connector run whose
Tests Gate was red completed the mirrored check on the dispatching SDK PR as
green. The gate driver
(.github/actions/verify-test-gate/verify_test_gate.py) is the single authority
for that verdict; report-to-sdk feeds its `conclusion` output straight to
complete_check_run.py.

Do not reintroduce a verdict here. Two implementations of "did the connector
tests pass" is the defect, not the inputs either one happened to read — see
test_the_callback_reports_the_gates_verdict_not_its_own.

Writes summary_file=<path> to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import os
import sys

# The suite-detection job gates the integration `if`. A *failure* there drops
# integration to a skip, so the rendered line must name the detection failure
# rather than report a clean "skipped" for a tier that was actually dropped.
# skipped (pull_request) and success are the benign values.
PASSING_DETECT_INTEGRATION_RESULTS = {"success", "skipped"}


def build_fallback_summary(
    unit_result: str,
    integration_result: str,
    detect_integration_result: str,
    e2e_result: str,
    unit_summary: str,
    integration_summary: str,
) -> str:
    # Show the integration line as a detection failure when detect-integration
    # broke, so the summary never reports a clean "skipped" for a tier that was
    # actually dropped by a failed detection.
    if detect_integration_result not in PASSING_DETECT_INTEGRATION_RESULTS:
        integration_line = (
            f"**integration:** not run — suite detection "
            f"{detect_integration_result}\n"
        )
    else:
        integration_line = (
            f"**integration:** {integration_result} "
            f"({integration_summary or 'no summary'})\n"
        )
    return (
        "## Tests Summary\n\n"
        f"**unit:** {unit_result} ({unit_summary or 'no summary'})\n"
        f"{integration_line}"
        f"**e2e:** {e2e_result}\n"
    )


def resolve_summary_file(
    artifact_summary_path: str,
    fallback_path: str,
    unit_result: str,
    integration_result: str,
    detect_integration_result: str,
    e2e_result: str,
    unit_summary: str,
    integration_summary: str,
) -> str:
    if os.path.isfile(artifact_summary_path):
        return artifact_summary_path
    with open(fallback_path, "w", encoding="utf-8") as fh:
        fh.write(
            build_fallback_summary(
                unit_result,
                integration_result,
                detect_integration_result,
                e2e_result,
                unit_summary,
                integration_summary,
            )
        )
    return fallback_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unit-result", required=True)
    parser.add_argument("--integration-result", required=True)
    parser.add_argument("--detect-integration-result", required=True)
    parser.add_argument("--e2e-result", required=True)
    parser.add_argument("--unit-summary", default="")
    parser.add_argument("--integration-summary", default="")
    parser.add_argument(
        "--artifact-summary-path", default="connector-results/pr-comment-body.md"
    )
    parser.add_argument("--fallback-path", default="fallback-summary.md")
    args = parser.parse_args(argv)

    summary_file = resolve_summary_file(
        args.artifact_summary_path,
        args.fallback_path,
        args.unit_result,
        args.integration_result,
        args.detect_integration_result,
        args.e2e_result,
        args.unit_summary,
        args.integration_summary,
    )

    line = f"summary_file={summary_file}"
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write(line + "\n")
    else:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
