#!/usr/bin/env python3
"""Determine the check-run conclusion + summary body for the cross-repo e2e
callback (tests-reusable.yaml's report-to-sdk job).

Moved out of an inlined `run:` block per docs/standards/ci.md: deciding the
conclusion and picking which summary file to use are both branches, which
belong in a tested driver rather than workflow YAML.

Prefers the e2e leg's already-rendered report (asset/lineage tables etc.,
written by sdr-e2e's PR-comment step) over a plain fallback, which only
applies when e2e was skipped or its artifact never materialised.

Writes conclusion=<success|failure> and summary_file=<path> to $GITHUB_OUTPUT.
"""

from __future__ import annotations

import argparse
import os
import sys

PASSING_E2E_RESULTS = {"success", "skipped"}
# Integration is skipped on PRs and when a connector has no integration suite,
# so "skipped" is a pass for it (mirrors the e2e optional-by-skip treatment).
PASSING_INTEGRATION_RESULTS = {"success", "skipped"}
# The suite-detection job gates the integration `if`. A *failure* there drops
# integration to a skip, which PASSING_INTEGRATION_RESULTS would read as a pass
# — so a failed detection must be its own failure signal here, exactly as the
# Tests Gate treats it. skipped (pull_request) and success are passes.
PASSING_DETECT_INTEGRATION_RESULTS = {"success", "skipped"}


def determine_conclusion(
    unit_result: str,
    integration_result: str,
    detect_integration_result: str,
    e2e_result: str,
) -> str:
    if (
        unit_result == "success"
        and detect_integration_result in PASSING_DETECT_INTEGRATION_RESULTS
        and integration_result in PASSING_INTEGRATION_RESULTS
        and e2e_result in PASSING_E2E_RESULTS
    ):
        return "success"
    return "failure"


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

    conclusion = determine_conclusion(
        args.unit_result,
        args.integration_result,
        args.detect_integration_result,
        args.e2e_result,
    )
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

    lines = [f"conclusion={conclusion}", f"summary_file={summary_file}"]
    github_output = os.environ.get("GITHUB_OUTPUT", "")
    if github_output:
        with open(github_output, "a") as fh:
            fh.write("\n".join(lines) + "\n")
    else:
        for line in lines:
            print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
