"""The scorecard rubric — weights, targets, gates, and grade bands.

The rubric is *data, not code*: it lives in a bundled ``rubric-<version>.json``
so the scoring policy can be tuned (weights, coverage target, band cutoffs)
without a code change, and the change is reviewable as a diff.  ``load_rubric()``
parses and validates it into the typed :class:`Rubric` model — mirroring the
conformance ``catalog.py`` / ``load_catalog()`` pattern.

JSON (not YAML) so no new dependency is pulled into the published package;
the same reasoning as the vendored ``*-schema-*.json`` files.
"""

from __future__ import annotations

import json
from enum import Enum
from functools import lru_cache
from pathlib import Path

from conformance.scorecard.schema import Grade, TierName
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

_COMMON = ConfigDict(populate_by_name=True, alias_generator=to_camel)

_RUBRIC_DIR = Path(__file__).parent


class CheckKind(str, Enum):
    """How a check's 0.0–1.0 score is computed from the raw evidence."""

    COVERAGE = "coverage"
    """``min(coverage_percent / coverage_target, 1.0)``."""

    PASS_RATE = "pass_rate"
    """``passed / ran`` (``ran`` = total − skipped); 0.0 when nothing ran."""

    PRESENT = "present"
    """1.0 if ≥1 test in the tier actually ran, else 0.0."""


class GateKind(str, Enum):
    """A gate condition evaluated against the raw counts."""

    UNIT_PRESENT = "unit_present"
    ALL_GREEN = "all_green"
    E2E_PRESENT = "e2e_present"
    """Aspirational under v1 weights: its ``cap:B`` is currently redundant with
    the band math. With the e2e tier at weight 0.35, an app with no e2e tests
    tops out at score 65 (grade C), already worse than B, so this cap never
    enters ``capped_by``. The e2e signal is still carried by the tier's
    ``present:false``, the gate ``status:fail``, and the silver maturity cap.
    The cap is retained so it binds automatically if a future rubric version
    lowers the e2e tier weight (letting e2e-absent reach a B/A band); to make it
    bind under v1, lower its cap to ``C`` or reduce the e2e tier weight."""


class CheckConfig(BaseModel):
    id: str
    weight: float = Field(..., ge=0.0, le=1.0)
    kind: CheckKind
    evidence: str
    """Which artifact this check reads — surfaced verbatim on the output check."""

    model_config = _COMMON


class TierConfig(BaseModel):
    name: TierName
    weight: float = Field(..., ge=0.0, le=1.0)
    checks: list[CheckConfig]

    model_config = _COMMON


class GateConfig(BaseModel):
    id: str
    kind: GateKind
    cap: Grade

    model_config = _COMMON


class GradeBand(BaseModel):
    min_score: int = Field(..., ge=0, le=100)
    grade: Grade

    model_config = _COMMON


class Rubric(BaseModel):
    """The full scoring policy for one rubric version."""

    version: str
    coverage_target: float = Field(..., gt=0.0, le=100.0)
    tiers: list[TierConfig]
    gates: list[GateConfig]
    grade_bands: list[GradeBand]
    """Bands checked highest-``min_score`` first; must include a 0-floor band."""

    model_config = _COMMON

    def bands_descending(self) -> list[GradeBand]:
        return sorted(self.grade_bands, key=lambda b: b.min_score, reverse=True)


@lru_cache(maxsize=None)
def load_rubric(version: str = "v1") -> Rubric:
    """Load and validate the bundled rubric for *version* (default ``"v1"``)."""
    path = _RUBRIC_DIR / f"rubric-{version}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return Rubric.model_validate(data)
