"""Per-asset verdicts, settling PENDING once the lookup reports, and run counts.

Two stages decide an asset's lineage. The app (stage 1) knows whether it built
lineage, why not, or that it handed refs to the asset lookup. The lookup
(stage 2) knows what happened to each ref. :func:`assess` covers stage 1;
:func:`settle` folds stage 2's :class:`RefOutcome` values into a PENDING asset.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from application_sdk.observability.lineage.taxonomy import (
    GapCategory,
    LineageReason,
    UninstrumentedPath,
    UpstreamAmbiguous,
    UpstreamNotInAtlan,
    Verdict,
)


class LineageStatus(StrEnum):
    """What the app itself did for an asset. Same values as application-sdk#2204."""

    HAS_LINEAGE = "HAS_LINEAGE"
    PARTIAL_LINEAGE = "PARTIAL_LINEAGE"
    MISSING_LINEAGE = "MISSING_LINEAGE"
    PENDING = "PENDING"
    """The app emitted refs the asset lookup must resolve."""


class RefOutcome(StrEnum):
    """What the asset lookup did with one ref (stage 2)."""

    MATCHED = "MATCHED"
    MISSED = "MISSED"
    AMBIGUOUS = "AMBIGUOUS"
    """More than one candidate; the lookup picked one."""


@dataclass(frozen=True)
class Assessment:
    """One asset's verdict and, when lineage is missing or partial, why."""

    verdict: Verdict
    reason: type[LineageReason] | None = None
    basis: str = "default"

    @property
    def gap(self) -> GapCategory | None:
        if self.verdict in (Verdict.VALID, Verdict.VALID_NONE):
            return None
        if self.verdict is Verdict.PENDING:
            return GapCategory.ASSET_RESOLUTION
        return self.reason.gap if self.reason else None

    def to_row(self) -> dict[str, Any]:
        reason = self.reason
        return {
            "verdict": self.verdict.value,
            "reasonCode": reason.code if reason else None,
            "reasonCategory": reason.category.value if reason else None,
            "gapCategory": self.gap.value if self.gap else None,
            "expectedBasis": self.basis,
            "customerMessage": reason.customer_message if reason else None,
        }


def assess(
    status: LineageStatus, reason: type[LineageReason] | None = None
) -> Assessment:
    """Stage-1 verdict from what the app did.

    Args:
        status: What the app did for the asset.
        reason: Why lineage is missing or partial. For ``PENDING`` it is an
            app-side partial failure carried until the asset is settled.
    """
    if status is LineageStatus.HAS_LINEAGE:
        return Assessment(Verdict.VALID)
    if status is LineageStatus.PARTIAL_LINEAGE:
        return Assessment(Verdict.INVALID, reason or UninstrumentedPath)
    if status is LineageStatus.PENDING:
        return Assessment(Verdict.PENDING, reason)
    reason = reason or UninstrumentedPath
    if reason.expected:
        return Assessment(Verdict.VALID_NONE, reason, basis="by_design")
    return Assessment(Verdict.MISSING, reason)


def settle(assessment: Assessment, outcomes: Sequence[RefOutcome | None]) -> Assessment:
    """Fold the lookup's per-ref outcomes into a PENDING asset.

    An asset stays PENDING until every ref it emitted has an outcome (``None``
    means not reported yet). Anything that is not PENDING is returned as is.
    """
    if assessment.verdict is not Verdict.PENDING or not outcomes:
        return assessment
    if any(outcome is None for outcome in outcomes):
        return assessment
    counts = Counter(outcomes)
    matched = counts[RefOutcome.MATCHED] + counts[RefOutcome.AMBIGUOUS]
    if matched == 0:
        return Assessment(Verdict.MISSING, UpstreamNotInAtlan, basis="lookup")
    if counts[RefOutcome.MISSED]:
        return Assessment(Verdict.INVALID, UpstreamNotInAtlan, basis="lookup")
    if counts[RefOutcome.AMBIGUOUS]:
        return Assessment(Verdict.INVALID, UpstreamAmbiguous, basis="lookup")
    if assessment.reason is not None:
        return Assessment(Verdict.INVALID, assessment.reason, basis="lookup")
    return Assessment(Verdict.VALID, basis="lookup")


@dataclass
class CoverageCounts:
    """Run totals. PENDING and VALID_NONE stay out of the coverage rate."""

    verdicts: Counter[Verdict] = field(default_factory=Counter)
    gaps: Counter[GapCategory] = field(default_factory=Counter)
    categories: Counter[str] = field(default_factory=Counter)

    def add(self, assessment: Assessment) -> None:
        self.verdicts[assessment.verdict] += 1
        if assessment.verdict in (Verdict.MISSING, Verdict.INVALID):
            gap = assessment.gap
            if gap is not None:
                self.gaps[gap] += 1
            if assessment.reason is not None:
                self.categories[assessment.reason.category.value] += 1

    def extend(self, assessments: Iterable[Assessment]) -> None:
        for assessment in assessments:
            self.add(assessment)

    @property
    def total(self) -> int:
        return sum(self.verdicts.values())

    @property
    def coverage_rate(self) -> float | None:
        """VALID over settled assets that should have lineage; ``None`` if none."""
        settled = (
            self.verdicts[Verdict.VALID]
            + self.verdicts[Verdict.INVALID]
            + self.verdicts[Verdict.MISSING]
        )
        return self.verdicts[Verdict.VALID] / settled if settled else None
