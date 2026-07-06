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


def determine_conclusion(tests_result: str, e2e_result: str) -> str:
    if tests_result == "success" and e2e_result in PASSING_E2E_RESULTS:
        return "success"
    return "failure"


def build_fallback_summary(
    tests_result: str, e2e_result: str, tests_summary: str
) -> str:
    return (
        "## Tests Summary\n\n"
        f"**tests:** {tests_result} ({tests_summary or 'no summary'})\n"
        f"**e2e:** {e2e_result}\n"
    )


def resolve_summary_file(
    artifact_summary_path: str,
    fallback_path: str,
    tests_result: str,
    e2e_result: str,
    tests_summary: str,
) -> str:
    if os.path.isfile(artifact_summary_path):
        return artifact_summary_path
    with open(fallback_path, "w", encoding="utf-8") as fh:
        fh.write(build_fallback_summary(tests_result, e2e_result, tests_summary))
    return fallback_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tests-result", required=True)
    parser.add_argument("--e2e-result", required=True)
    parser.add_argument("--tests-summary", default="")
    parser.add_argument(
        "--artifact-summary-path", default="connector-results/pr-comment-body.md"
    )
    parser.add_argument("--fallback-path", default="fallback-summary.md")
    args = parser.parse_args(argv)

    conclusion = determine_conclusion(args.tests_result, args.e2e_result)
    summary_file = resolve_summary_file(
        args.artifact_summary_path,
        args.fallback_path,
        args.tests_result,
        args.e2e_result,
        args.tests_summary,
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
