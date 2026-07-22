#!/usr/bin/env python3
"""Build a compact test-readiness history entry from a scorecard JSON.

The per-repo scorecard (``results/test-readiness.json``, produced by
``atlan-application-sdk-conformance scorecard``) is uploaded to Kryptonite
verbatim as the per-repo doc.  For trend tracking we additionally append a
small one-line-per-day history entry — this script builds that line.

Extracting it here (rather than inlining in the workflow) keeps the transform
regression-tested per docs/standards/ci.md; the workflow only does straight-line
``aws s3`` orchestration around it.

Usage::

    python scorecard_history_entry.py \\
        --scorecard results/test-readiness.json \\
        --date 2026-07-21 \\
        --out /tmp/history.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def history_entry(scorecard: dict[str, Any], date: str) -> dict[str, Any]:
    """Project a scorecard doc into a compact, trend-friendly history entry."""
    agg = scorecard.get("aggregate", {}) or {}
    return {
        "date": date,
        "repo": scorecard.get("repo", "unknown"),
        "score": agg.get("score"),
        "grade": agg.get("grade"),
        "maturity": agg.get("maturity"),
        "cappedBy": agg.get("cappedBy", []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scorecard", required=True, help="Path to test-readiness.json"
    )
    parser.add_argument("--date", required=True, help="History date, YYYY-MM-DD")
    parser.add_argument("--out", required=True, help="History JSONL to append to")
    args = parser.parse_args(argv)

    scorecard = json.loads(Path(args.scorecard).read_text(encoding="utf-8"))
    entry = history_entry(scorecard, args.date)

    with open(args.out, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")

    print(
        f"{entry['repo']}: score={entry['score']} grade={entry['grade']} "
        f"maturity={entry['maturity']} → {args.out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
