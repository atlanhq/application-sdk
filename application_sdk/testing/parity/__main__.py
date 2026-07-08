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
import sys
from pathlib import Path

import orjson

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.testing.parity.comparator import run_comparison
from application_sdk.testing.parity.report import (
    generate_json_report,
    generate_markdown,
)

logger = get_logger(__name__)


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
        logger.error("Baseline directory not found: %s", baseline_dir)
        sys.exit(2)
    if not candidate_dir.exists():
        logger.error("Candidate directory not found: %s", candidate_dir)
        sys.exit(2)

    logger.info(
        "Comparing: %s (baseline) vs %s (candidate)", baseline_dir, candidate_dir
    )
    results = run_comparison(baseline_dir, candidate_dir)

    md_report = generate_markdown(results, args.baseline_ref, args.candidate_ref)
    json_report = generate_json_report(results, args.baseline_ref, args.candidate_ref)

    # conformance: ignore[L005] primary CLI output: the markdown report is written to stdout for capture/piping; routing it through the logger would corrupt the tool's output contract
    print(md_report)  # noqa: T201

    if args.output_md:
        Path(args.output_md).write_text(md_report, encoding="utf-8")
        logger.info("Markdown report written to %s", args.output_md)

    if args.output_json:
        Path(args.output_json).write_text(
            orjson.dumps(json_report, option=orjson.OPT_INDENT_2, default=str).decode(),
            encoding="utf-8",
        )
        logger.info("JSON report written to %s", args.output_json)

    is_parity = not any(r.has_diffs for r in results)
    if is_parity:
        logger.info("PARITY: No differences found.")
        sys.exit(0)
    else:
        total = sum(len(r.added) + len(r.removed) + len(r.modified) for r in results)
        logger.info("PARITY BROKEN: %s differences found.", total)
        sys.exit(1)


if __name__ == "__main__":
    main()
