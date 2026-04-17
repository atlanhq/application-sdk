"""CLI entry point for parity comparison.

Usage:
    python -m application_sdk.testing.parity \\
        --baseline ./baseline/ --candidate ./candidate/ \\
        [--output-json report.json] [--output-md report.md]

Exit codes:
    0 = parity (no differences)
    1 = differences found
    2 = input error
"""

import argparse
import json
import sys
from pathlib import Path

from application_sdk.testing.parity.comparator import run_comparison
from application_sdk.testing.parity.report import (
    generate_json_report,
    generate_markdown,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parity Test — Compare extraction outputs"
    )
    parser.add_argument(
        "--baseline", required=True, help="Path to baseline transformed output"
    )
    parser.add_argument(
        "--candidate", required=True, help="Path to candidate transformed output"
    )
    parser.add_argument("--output-json", help="Write JSON report to file")
    parser.add_argument("--output-md", help="Write markdown report to file")
    parser.add_argument(
        "--baseline-ref", default="main", help="Git ref for baseline (for report)"
    )
    parser.add_argument(
        "--candidate-ref", default="PR", help="Git ref for candidate (for report)"
    )
    args = parser.parse_args()

    baseline_dir = Path(args.baseline)
    candidate_dir = Path(args.candidate)

    if not baseline_dir.exists():
        print(f"ERROR: Baseline directory not found: {baseline_dir}", file=sys.stderr)  # noqa: T201
        sys.exit(2)
    if not candidate_dir.exists():
        print(  # noqa: T201
            f"ERROR: Candidate directory not found: {candidate_dir}", file=sys.stderr
        )
        sys.exit(2)

    print(f"Comparing: {baseline_dir} (baseline) vs {candidate_dir} (candidate)")  # noqa: T201
    results = run_comparison(baseline_dir, candidate_dir)

    md_report = generate_markdown(results, args.baseline_ref, args.candidate_ref)
    json_report = generate_json_report(results, args.baseline_ref, args.candidate_ref)

    print(md_report)  # noqa: T201

    if args.output_md:
        Path(args.output_md).write_text(md_report, encoding="utf-8")
        print(f"Markdown report written to {args.output_md}", file=sys.stderr)  # noqa: T201

    if args.output_json:
        Path(args.output_json).write_text(
            json.dumps(json_report, indent=2, default=str), encoding="utf-8"
        )
        print(f"JSON report written to {args.output_json}", file=sys.stderr)  # noqa: T201

    is_parity = not any(r.has_diffs for r in results)
    if is_parity:
        print("\nPARITY: No differences found.", file=sys.stderr)  # noqa: T201
        sys.exit(0)
    else:
        total = sum(len(r.added) + len(r.removed) + len(r.modified) for r in results)
        print(f"\nPARITY BROKEN: {total} differences found.", file=sys.stderr)  # noqa: T201
        sys.exit(1)


if __name__ == "__main__":
    main()
