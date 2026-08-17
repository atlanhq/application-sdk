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

import glob as _glob
import json
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from pathlib import Path

from conformance.scorecard.schema import (
    CoverageMetrics,
    RawTests,
    TierName,
    TierTestCounts,
)

_SEGMENT_SPLIT = re.compile(r"[./\\]+")

#: Outcome names, ordered worst-last.  Used to fold one test's results across
#: several per-leg junits down to a single outcome — see
#: :func:`parse_junit_tier_merged` for why ``passed`` outranks ``skipped``.
_OUTCOME_RANK: dict[str, int] = {"skipped": 0, "passed": 1, "failed": 2, "errors": 3}


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
        _tally(buckets[tier], testcase)

    return RawTests(
        unit=buckets["unit"],
        integration=buckets["integration"],
        e2e=buckets["e2e"],
    )


def parse_junit_tier(path: str | Path) -> TierTestCounts:
    """Parse a junit XML whose testcases all belong to ONE tier.

    Post-tier-split, unit and integration run as separate CI jobs, so each
    produces a single-tier junit.  This attributes *every* testcase in the file
    to one tier's counts regardless of path (the file itself identifies the
    tier), which is more robust than path-slicing when an app's directory layout
    differs from ``tests/<tier>/``.
    """
    tree = ET.parse(str(path))
    counts = TierTestCounts()
    for testcase in tree.getroot().iter("testcase"):
        _tally(counts, testcase)
    return counts


def resolve_junit_paths(patterns: Iterable[str]) -> list[str]:
    """Expand *patterns* (plain paths or globs) into existing junit files, de-duplicated.

    The e2e tier arrives as N per-leg artifacts (one per suite × cloud since
    FND-6), so the CLI is handed a glob rather than a list the workflow would
    have to build with shell branching.  A pattern that matches nothing yields
    nothing — which the caller reads as "e2e did not run", never as "e2e ran and
    scored zero".

    A match is only kept if it looks like a junit document — a ``.xml`` file
    whose root element is ``<testsuite>`` or ``<testsuites>``.  Without that
    gate a broad caller glob would either crash the scorecard (binary / non-XML
    match handed to ``ET.parse``) or silently fold an unrelated XML's
    ``<testcase>`` elements into the e2e counts.
    """
    seen: dict[str, None] = {}
    for pattern in patterns:
        if not pattern:
            continue
        matches = sorted(_glob.glob(pattern))
        # A plain (non-glob) path is its own match; glob returns it only when it
        # exists, which is the same existence check the single-file reader does.
        for match in matches:
            if _is_junit_file(match):
                seen.setdefault(match, None)
    return list(seen)


def _is_junit_file(path: str) -> bool:
    """Cheap "is this a junit XML?" gate for glob matches.

    Checks the ``.xml`` suffix first (a directory or suffix-less file never
    reaches the parser), then reads just the root element — not the whole tree —
    so a malformed tail cannot reject a file whose root is genuine junit.
    """
    candidate = Path(path)
    if candidate.suffix != ".xml" or not candidate.is_file():
        return False
    try:
        for _event, element in ET.iterparse(str(candidate), events=("start",)):
            return element.tag in ("testsuite", "testsuites")
    except ET.ParseError:
        return False
    return False


def parse_junit_tier_merged(paths: Iterable[str | Path]) -> TierTestCounts:
    """Fold N per-leg junits for ONE tier into a single worst-case count.

    The e2e matrix runs each suite once per cloud, so the same test id appears
    in several junits.  Tests are keyed on ``(classname, name)`` and folded to
    their **worst** outcome across legs, rather than summed:

    * Summing would make the pass rate a function of how many clouds a repo has
      onboarded — a failure on one of three clouds reads better (11/12) than the
      same failure on the only cloud (3/4).  Onboarding a cloud would *raise*
      the score by diluting an existing failure, which is precisely backwards.
    * Worst-case keeps the denominator at "distinct e2e tests" and treats a test
      that fails on any supported cloud as not passing, which is what
      "e2e-ready" has to mean.

    ``passed`` outranks ``skipped`` deliberately: a test that ran green on one
    cloud and self-skipped on another (absent creds) genuinely ran, and
    :attr:`TierTestCounts.ran` excludes skips.  Ranking skip highest would erase
    the evidence a leg actually produced.

    ``duration_sec`` is the per-test **max** across legs, matching the per-test
    dedup — a sum would count the same test's wall time once per cloud.
    """
    worst: dict[tuple[str, str], str] = {}
    longest: dict[tuple[str, str], float] = {}

    for path in paths:
        for testcase in ET.parse(str(path)).getroot().iter("testcase"):
            key = (testcase.get("classname") or "", testcase.get("name") or "")
            outcome = _outcome(testcase)
            if _OUTCOME_RANK[outcome] > _OUTCOME_RANK.get(worst.get(key, ""), -1):
                worst[key] = outcome
            longest[key] = max(
                longest.get(key, 0.0), float(testcase.get("time") or 0.0)
            )

    counts = TierTestCounts()
    for key, outcome in worst.items():
        counts.total += 1
        counts.duration_sec += longest[key]
        setattr(counts, outcome, getattr(counts, outcome) + 1)
    return counts


def _outcome(testcase: ET.Element) -> str:
    """Classify one ``<testcase>`` (error > failure > skipped > pass).

    error takes precedence over failure, matching how pytest reports
    collection/fixture errors distinctly from assertion failures.
    """
    if testcase.find("error") is not None:
        return "errors"
    if testcase.find("failure") is not None:
        return "failed"
    if testcase.find("skipped") is not None:
        return "skipped"
    return "passed"


def _tally(counts: TierTestCounts, testcase: ET.Element) -> None:
    """Fold one ``<testcase>`` into *counts* (error > failure > skipped > pass)."""
    counts.total += 1
    counts.duration_sec += float(testcase.get("time") or 0.0)
    outcome = _outcome(testcase)
    setattr(counts, outcome, getattr(counts, outcome) + 1)


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
