"""Test-readiness scorecard — a quantified companion to conformance SARIF.

Public API::

    from conformance.scorecard import (
        build_scorecard, load_rubric, parse_junit, parse_coverage_json,
        Scorecard, validate_scorecard,
    )

Pipeline: ``parse_junit`` + ``parse_coverage_json`` (pure readers) →
``build_scorecard`` (pure, rubric-driven) → :class:`Scorecard`.  The ``cli``
module is the only impure edge (filesystem + timestamp).
"""

from conformance.scorecard.compute import build_scorecard
from conformance.scorecard.readers import (
    parse_coverage_json,
    parse_junit,
    tier_for_path,
)
from conformance.scorecard.rubric import Rubric, load_rubric
from conformance.scorecard.schema import (
    Aggregate,
    Check,
    CoverageMetrics,
    Gate,
    RawMetrics,
    RawTests,
    Scorecard,
    Tier,
    TierTestCounts,
)
from conformance.scorecard.validate import scorecard_json_schema, validate_scorecard

__all__ = [
    # Pipeline
    "parse_junit",
    "parse_coverage_json",
    "tier_for_path",
    "build_scorecard",
    "load_rubric",
    # Models
    "Rubric",
    "Scorecard",
    "Aggregate",
    "Tier",
    "Check",
    "Gate",
    "RawMetrics",
    "RawTests",
    "TierTestCounts",
    "CoverageMetrics",
    # Validation
    "validate_scorecard",
    "scorecard_json_schema",
]
