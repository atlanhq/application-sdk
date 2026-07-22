"""Pure parsers for the standard test-evidence formats.

These read the *native, standard* outputs the test tier already produces —
pytest junit XML (``results/test-results.xml``) and coverage.py JSON
(``coverage.json``) — and project them into the typed counts the scorecard
computer consumes.  No scoring logic lives here; no rubric is consulted.

Tier assignment is directory-based (the layout contract): a testcase belongs
to the ``e2e`` / ``integration`` tier if any path segment of its file or dotted
classname is ``e2e`` / ``integration``; everything else is ``unit`` (the
catch-all base tier).
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from conformance.scorecard.schema import (
    CoverageMetrics,
    RawTests,
    TierName,
    TierTestCounts,
)

_SEGMENT_SPLIT = re.compile(r"[./\\]+")


def tier_for_path(path: str) -> TierName:
    """Bucket a test file path / dotted classname into a tier.

    Matches on whole path *segments* (not substrings) so a directory like
    ``tests/reintegration/`` does not accidentally read as ``integration``.
    """
    segments = {s.lower() for s in _SEGMENT_SPLIT.split(path) if s}
    if "e2e" in segments:
        return "e2e"
    if "integration" in segments:
        return "integration"
    return "unit"


def _testcase_location(testcase: ET.Element) -> str:
    """Best available path for a ``<testcase>`` — ``file`` attr, then classname."""
    return testcase.get("file") or testcase.get("classname") or ""


def parse_junit(path: str | Path) -> RawTests:
    """Parse a pytest junit XML file into per-tier :class:`TierTestCounts`.

    A testcase is counted as ``errors`` if it carries an ``<error>`` child,
    ``failed`` for a ``<failure>`` child, ``skipped`` for a ``<skipped>`` child,
    else ``passed``.  (error takes precedence over failure, matching how pytest
    reports collection/fixture errors distinctly from assertion failures.)
    """
    tree = ET.parse(str(path))
    root = tree.getroot()

    buckets: dict[TierName, TierTestCounts] = {
        "unit": TierTestCounts(),
        "integration": TierTestCounts(),
        "e2e": TierTestCounts(),
    }

    for testcase in root.iter("testcase"):
        tier = tier_for_path(_testcase_location(testcase))
        counts = buckets[tier]
        counts.total += 1
        counts.duration_sec += float(testcase.get("time") or 0.0)

        if testcase.find("error") is not None:
            counts.errors += 1
        elif testcase.find("failure") is not None:
            counts.failed += 1
        elif testcase.find("skipped") is not None:
            counts.skipped += 1
        else:
            counts.passed += 1

    return RawTests(
        unit=buckets["unit"],
        integration=buckets["integration"],
        e2e=buckets["e2e"],
    )


def parse_coverage_json(path: str | Path) -> CoverageMetrics:
    """Parse a coverage.py JSON report (``coverage json``) into aggregate metrics.

    Reads the ``totals`` block.  ``branch_percent`` is derived from
    ``covered_branches`` / ``num_branches`` when branch coverage was enabled,
    else left ``None``.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    totals = data.get("totals", {})

    num_branches = int(totals.get("num_branches", 0) or 0)
    covered_branches = int(totals.get("covered_branches", 0) or 0)
    branch_percent = (
        round(covered_branches / num_branches * 100.0, 2) if num_branches else None
    )

    return CoverageMetrics(
        lines_covered=int(totals.get("covered_lines", 0) or 0),
        lines_valid=int(totals.get("num_statements", 0) or 0),
        percent=round(float(totals.get("percent_covered", 0.0) or 0.0), 2),
        branch_percent=branch_percent,
    )
