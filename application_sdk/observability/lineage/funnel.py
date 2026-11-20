"""Map a coverage verdict onto the support lineage-funnel buckets.

Lineage tickets are closed with exactly one ``lineage-funnel`` label. Mapping
every verdict onto the same buckets lets run-time counts be compared with
closed-ticket labels (calibration). The bucket comes from the reason's
category; a few verdicts refine it, and some combinations have no bucket yet
(``None``), which is reported as a gap rather than forced into a near fit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from application_sdk.observability.lineage.taxonomy import ReasonCategory, Verdict

if TYPE_CHECKING:
    from application_sdk.observability.lineage.assessment import Assessment

PARSE_ERROR = "Parse error"
PARTIAL_PARSE = "Dataflow too complex for parser"
NOT_EXTRACTED = "Assets and query history not extracted"
LOOKUP_ALL_SOURCES = "Failure to lookup all source tables"
SOURCE_SPECIFIC = "Source-specific lineage derivation"
PROCESS_NOT_CREATED = "Lineage process not created"
EXPECTATION_MISMATCH = "Product expectation mismatch"
NOT_PUBLISHED = "Not processed by Publish App"

FUNNEL_BY_CATEGORY: dict[ReasonCategory, str | None] = {
    ReasonCategory.PARSER_FAILURE: PARSE_ERROR,
    ReasonCategory.ASSET_NOT_FOUND: NOT_EXTRACTED,
    ReasonCategory.CACHE_MISS: LOOKUP_ALL_SOURCES,
    ReasonCategory.UNSUPPORTED_SOURCE_FEATURE: SOURCE_SPECIFIC,
    ReasonCategory.MISSING_METADATA: SOURCE_SPECIFIC,
    ReasonCategory.NO_UPSTREAM_DATA: SOURCE_SPECIFIC,
    ReasonCategory.NO_SEARCHABLE_DIALECT: SOURCE_SPECIFIC,
    ReasonCategory.PROCESS_MAPPING_FAILURE: PROCESS_NOT_CREATED,
    ReasonCategory.CONNECTOR_LIMITATION: EXPECTATION_MISMATCH,
    # No bucket: not a lineage failure, or nothing known about it.
    ReasonCategory.RELATIONSHIP_FALLBACK: None,
    ReasonCategory.OBSERVABILITY_DISABLED: None,
    ReasonCategory.UNINSTRUMENTED_PATH: None,
}
"""Default bucket per category. Every category must appear (tested)."""


def funnel_bucket(assessment: Assessment) -> str | None:
    """The lineage-funnel bucket for one asset's verdict, or ``None``.

    - VALID and PENDING have no bucket: nothing failed (yet).
    - VALID_NONE (lineage not expected by design) is a product expectation.
    - A lookup miss that left some upstreams in place (INVALID) has no bucket:
      the funnel's lookup bucket means *no* source table resolved.
    - A reason may override its category's bucket with a ``funnel`` ClassVar;
      ``""`` means no bucket.
    """
    verdict = assessment.verdict
    if verdict in (Verdict.VALID, Verdict.PENDING):
        return None
    if verdict is Verdict.VALID_NONE:
        return EXPECTATION_MISMATCH
    reason = assessment.reason
    if reason is None:
        return None
    override = getattr(reason, "funnel", None)
    if override is not None:
        return override or None
    bucket = FUNNEL_BY_CATEGORY.get(reason.category)
    if bucket == LOOKUP_ALL_SOURCES and verdict is Verdict.INVALID:
        return None
    return bucket
