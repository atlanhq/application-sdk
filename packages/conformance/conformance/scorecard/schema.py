"""Pydantic models for the test-readiness scorecard artifact.

The scorecard is a *quantified* companion to conformance SARIF.  SARIF
interchanges findings (categorical pass/fail); this interchanges test-readiness
*scores* — per-tier sub-scores, an aggregate 0–100, a letter grade, and a
Bronze/Silver/Gold maturity level.  It is deliberately **not** SARIF: SARIF's
only numeric axis is ``result.rank`` (semantically "priority"), so scores would
have no first-class home there.

An instance is emitted per app as ``results/test-readiness.json``, rolled up
into the ``k.atlan.dev/test-readiness-dashboard/`` bundle, and ingested by
connector-pulse as the ``test_readiness`` metric.

camelCase on the wire via ``to_camel`` — mirrors ``suite/schema/sarif.py`` so
the two artifacts share one serialization convention.  Construct with Python
snake_case names; serialise with ``model_dump(by_alias=True, exclude_none=True)``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_COMMON = ConfigDict(populate_by_name=True, alias_generator=to_camel)

TierName = Literal["unit", "integration", "e2e"]
Grade = Literal["A", "B", "C", "D", "F"]
Maturity = Literal["gold", "silver", "bronze", "none"]

SCHEMA_VERSION = "1.0"

# Grade ordering (worst → best); used to apply gate caps as a "min" operation.
GRADE_ORDER: dict[str, int] = {"F": 0, "D": 1, "C": 2, "B": 3, "A": 4}


# ---------------------------------------------------------------------------
# Raw measured evidence
# ---------------------------------------------------------------------------


class CoverageMetrics(BaseModel):
    """Aggregate coverage numbers parsed from coverage.py JSON ``totals``."""

    lines_covered: int = Field(..., ge=0)
    lines_valid: int = Field(..., ge=0)
    percent: float = Field(..., ge=0.0, le=100.0)
    branch_percent: float | None = Field(default=None, ge=0.0, le=100.0)

    model_config = _COMMON


class TierTestCounts(BaseModel):
    """Per-tier test outcome counts sliced from one junit XML by file path."""

    total: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    duration_sec: float = Field(default=0.0, ge=0.0)

    model_config = _COMMON

    @property
    def ran(self) -> int:
        """Tests that actually executed (collected minus skipped).

        The load-bearing distinction for readiness: an e2e suite that
        collect-and-skips because creds are absent has ``ran == 0`` and so
        reads as *not present*, rather than masquerading as green.
        """
        return max(self.total - self.skipped, 0)

    @property
    def present(self) -> bool:
        return self.ran > 0

    @property
    def green(self) -> bool:
        """No failures or errors among the tests that ran."""
        return self.failed == 0 and self.errors == 0


class RawTests(BaseModel):
    """Per-tier counts for all three tiers (always present, zeroed if absent)."""

    unit: TierTestCounts = Field(default_factory=TierTestCounts)
    integration: TierTestCounts = Field(default_factory=TierTestCounts)
    e2e: TierTestCounts = Field(default_factory=TierTestCounts)

    model_config = _COMMON

    def by_name(self, name: TierName) -> TierTestCounts:
        return getattr(self, name)


class RawMetrics(BaseModel):
    """The underlying measured numbers, for dashboard drill-down and recompute."""

    coverage: CoverageMetrics | None = None
    tests: RawTests = Field(default_factory=RawTests)

    model_config = _COMMON


# ---------------------------------------------------------------------------
# Scored structure
# ---------------------------------------------------------------------------


class Check(BaseModel):
    """A single scored dimension within a tier (0.0–1.0)."""

    id: str
    """Stable check id, e.g. ``"unit.coverage"``."""

    score: float = Field(..., ge=0.0, le=1.0)
    value: str
    """Human-readable measured value, e.g. ``"86.0%"`` or ``"412/412"``."""

    weight: float = Field(..., ge=0.0, le=1.0)
    evidence: str
    """Source artifact this check was derived from, e.g. ``"coverage.json"``."""

    model_config = _COMMON


class Tier(BaseModel):
    """A test tier (unit / integration / e2e) with its weighted sub-score."""

    name: TierName
    present: bool
    weight: float = Field(..., ge=0.0, le=1.0)
    score: int = Field(..., ge=0, le=100)
    checks: list[Check] = Field(default_factory=list)

    model_config = _COMMON


class Gate(BaseModel):
    """A SonarQube-style quality gate that can cap the grade regardless of score."""

    id: str
    status: Literal["pass", "fail"]
    effect: str
    """The cap this gate applies when failing, e.g. ``"cap:B"``."""

    model_config = _COMMON


class Aggregate(BaseModel):
    """The headline rollup: score, grade (after gate caps), and maturity."""

    score: int = Field(..., ge=0, le=100)
    grade: Grade
    maturity: Maturity
    capped_by: list[str] = Field(default_factory=list)
    """Gate ids that lowered the grade below the earned band; empty if none."""

    model_config = _COMMON


class Scorecard(BaseModel):
    """Top-level per-repo test-readiness scorecard."""

    schema_version: str = SCHEMA_VERSION
    rubric_version: str
    repo: str
    """GitHub full name, e.g. ``"atlanhq/atlan-mysql-app"`` — the connector-pulse key."""

    app: str
    commit_sha: str | None = None
    tool_version: str
    generated_at: str
    """ISO-8601 UTC timestamp; stamped by the CLI (the one impure edge)."""

    aggregate: Aggregate
    tiers: list[Tier] = Field(default_factory=list)
    gates: list[Gate] = Field(default_factory=list)
    raw: RawMetrics

    model_config = _COMMON
