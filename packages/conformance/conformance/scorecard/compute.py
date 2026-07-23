"""Pure scoring core: raw evidence + rubric → :class:`Scorecard`.

``build_scorecard`` is deterministic and IO-free — no clock, no filesystem.
The timestamp is passed in (``generated_at``) so callers own the one impure
edge and tests can assert exact output.  Everything the scoring depends on
(weights, coverage target, gate caps, grade bands) comes from the
:class:`Rubric`, never a hardcoded constant here.

Two structural concepts:

* **Per-tier coverage** — ``coverage`` is a ``{tier: CoverageMetrics}`` map, so
  the unit and integration tiers each score against their own coverage number.
* **Applicability** — ``measured_tiers`` names the tiers the run actually
  exercised.  unit + integration are always measured (a missing junit → zero
  counts → scored 0, so a missing integration suite still counts against the
  grade).  e2e is measured only when an e2e junit was supplied; otherwise the
  e2e tier is *not applicable* — excluded from the aggregate (weights
  renormalize over applicable tiers), its ``e2e-present`` gate reads ``na``, and
  gold maturity is unreachable — rather than dragging every normal run to a
  capped B.
"""

from __future__ import annotations

from conformance.scorecard.rubric import CheckKind, GateKind, Rubric, TierConfig
from conformance.scorecard.schema import (
    GRADE_ORDER,
    SCHEMA_VERSION,
    Aggregate,
    Check,
    CoverageMetrics,
    Gate,
    Grade,
    Maturity,
    RawMetrics,
    RawTests,
    Scorecard,
    Tier,
    TierName,
    TierTestCounts,
)


def _worst(a: Grade, b: Grade) -> Grade:
    """Return the lower (worse) of two grades by :data:`GRADE_ORDER`."""
    return a if GRADE_ORDER[a] <= GRADE_ORDER[b] else b


def _pass_rate(counts: TierTestCounts) -> float:
    return counts.passed / counts.ran if counts.ran > 0 else 0.0


def _check_score(
    kind: CheckKind,
    counts: TierTestCounts,
    tier_coverage: CoverageMetrics | None,
    coverage_target: float,
) -> tuple[float, str]:
    """Return ``(score, display_value)`` for one check, using this tier's coverage."""
    if kind is CheckKind.COVERAGE:
        if tier_coverage is None:
            return 0.0, "n/a"
        score = min(tier_coverage.percent / coverage_target, 1.0)
        return score, f"{tier_coverage.percent:.1f}%"
    if kind is CheckKind.PASS_RATE:
        return _pass_rate(counts), f"{counts.passed}/{counts.ran}"
    if kind is CheckKind.PRESENT:
        return (1.0, "yes") if counts.present else (0.0, "no")
    raise ValueError(f"unknown check kind: {kind!r}")  # pragma: no cover


def _score_tier(
    tier_cfg: TierConfig,
    counts: TierTestCounts,
    tier_coverage: CoverageMetrics | None,
    coverage_target: float,
    *,
    applicable: bool,
) -> Tier:
    checks: list[Check] = []
    weighted = 0.0
    for check_cfg in tier_cfg.checks:
        score, value = _check_score(
            check_cfg.kind, counts, tier_coverage, coverage_target
        )
        weighted += check_cfg.weight * score
        checks.append(
            Check(
                id=check_cfg.id,
                score=round(score, 4),
                value=value,
                weight=check_cfg.weight,
                evidence=check_cfg.evidence,
            )
        )
    return Tier(
        name=tier_cfg.name,
        applicable=applicable,
        present=counts.present,
        weight=tier_cfg.weight,
        score=round(100 * weighted),
        checks=checks,
    )


def _evaluate_gate(
    kind: GateKind, tests: RawTests, measured_tiers: set[TierName]
) -> str:
    """Return the gate status: ``"pass"``, ``"fail"``, or ``"na"``."""
    if kind is GateKind.UNIT_PRESENT:
        return "pass" if tests.unit.present else "fail"
    if kind is GateKind.E2E_PRESENT:
        if "e2e" not in measured_tiers:
            return "na"
        return "pass" if tests.e2e.present else "fail"
    if kind is GateKind.ALL_GREEN:
        return (
            "pass"
            if all(tests.by_name(name).green for name in measured_tiers)
            else "fail"
        )
    raise ValueError(f"unknown gate kind: {kind!r}")  # pragma: no cover


def _grade_for_score(rubric: Rubric, score: int) -> Grade:
    for band in rubric.bands_descending():
        if score >= band.min_score:
            return band.grade
    # grade_bands is validated to include a 0-floor band, so this is unreachable
    # in practice; default to the worst grade defensively.
    return "F"  # pragma: no cover


def _maturity(
    tests: RawTests,
    coverage: dict[TierName, CoverageMetrics],
    target: float,
    measured_tiers: set[TierName],
) -> Maturity:
    """Bronze/Silver/Gold ladder (Google 'Test Certified' style).

    * bronze — unit present & green
    * silver — + integration present & green & unit coverage ≥ target
    * gold   — + e2e measured & present & green

    Gold is unreachable unless e2e actually ran (``"e2e"`` in ``measured_tiers``),
    matching the applicability rule.
    """
    unit, integ, e2e = tests.unit, tests.integration, tests.e2e
    if not (unit.present and unit.green):
        return "none"
    unit_cov = coverage.get("unit")
    coverage_ok = unit_cov is not None and unit_cov.percent >= target
    if not (integ.present and integ.green and coverage_ok):
        return "bronze"
    if not ("e2e" in measured_tiers and e2e.present and e2e.green):
        return "silver"
    return "gold"


def build_scorecard(
    *,
    tests: RawTests,
    coverage: dict[TierName, CoverageMetrics],
    measured_tiers: set[TierName],
    rubric: Rubric,
    repo: str,
    app: str,
    commit_sha: str | None,
    tool_version: str,
    generated_at: str,
) -> Scorecard:
    """Assemble a :class:`Scorecard` from raw evidence and a rubric.

    Deterministic and IO-free; ``generated_at`` is supplied by the caller.
    ``coverage`` is a per-tier map; ``measured_tiers`` names the tiers exercised
    in this run (drives applicability + aggregate renormalization).
    """
    tiers = [
        _score_tier(
            cfg,
            tests.by_name(cfg.name),
            coverage.get(cfg.name),
            rubric.coverage_target,
            applicable=cfg.name in measured_tiers,
        )
        for cfg in rubric.tiers
    ]

    # Aggregate over applicable tiers only, renormalizing their weights so the
    # score is out of 100 regardless of which tiers were measured.
    applicable = [t for t in tiers if t.applicable]
    total_weight = sum(t.weight for t in applicable)
    aggregate_score = (
        round(sum(t.weight * t.score for t in applicable) / total_weight)
        if total_weight
        else 0
    )
    band_grade = _grade_for_score(rubric, aggregate_score)

    gates: list[Gate] = []
    effective_grade = band_grade
    capped_by: list[str] = []
    for gate_cfg in rubric.gates:
        status = _evaluate_gate(gate_cfg.kind, tests, measured_tiers)
        gates.append(Gate(id=gate_cfg.id, status=status, effect=f"cap:{gate_cfg.cap}"))
        # A failing gate lowers the grade only when its cap is strictly worse
        # than the score-earned band. An ``na`` gate never caps.
        if status == "fail" and GRADE_ORDER[gate_cfg.cap] < GRADE_ORDER[band_grade]:
            capped_by.append(gate_cfg.id)
            effective_grade = _worst(effective_grade, gate_cfg.cap)

    aggregate = Aggregate(
        score=aggregate_score,
        grade=effective_grade,
        maturity=_maturity(tests, coverage, rubric.coverage_target, measured_tiers),
        capped_by=capped_by,
    )

    return Scorecard(
        schema_version=SCHEMA_VERSION,
        rubric_version=rubric.version,
        repo=repo,
        app=app,
        commit_sha=commit_sha,
        tool_version=tool_version,
        generated_at=generated_at,
        aggregate=aggregate,
        tiers=tiers,
        gates=gates,
        raw=RawMetrics(coverage=coverage, tests=tests),
    )
