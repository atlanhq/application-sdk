"""Tests for the three-state disposition derivation.

Covers the four rows of the disposition table from docs/schema-contract.md §3,
plus edge cases: suppression-over-level precedence, unknown kinds, and the
gate exit-code derivation.
"""

import pytest

from conformance.schema.disposition import Disposition, derive_disposition
from conformance.schema.sarif import (
    ArtifactLocation,
    Location,
    PhysicalLocation,
    Region,
    Result,
    Suppression,
)


def _result(
    rule_id: str = "P001",
    kind: str = "fail",
    level: str | None = "error",
    suppressions: list[Suppression] | None = None,
) -> Result:
    return Result(
        ruleId=rule_id,
        kind=kind,
        level=level,
        locations=[
            Location(
                physicalLocation=PhysicalLocation(
                    artifactLocation=ArtifactLocation(uri="src/foo.py"),
                    region=Region(startLine=1),
                )
            )
        ],
        suppressions=suppressions or [],
    )


def _suppression() -> Suppression:
    return Suppression(
        kind="inSource",
        justification="optional-dep guard — PIL never available in CI",
    )


# ---------------------------------------------------------------------------
# Table rows
# ---------------------------------------------------------------------------


def test_pass_disposition():
    """kind='pass' → PASS regardless of level."""
    result = _result(kind="pass", level="error")
    assert derive_disposition(result) == Disposition.PASS


def test_pass_disposition_no_level():
    """kind='pass' with no level → PASS."""
    result = _result(kind="pass", level=None)
    assert derive_disposition(result) == Disposition.PASS


def test_failing_disposition():
    """kind='fail', level='error', no suppressions → FAILING (blocks gate)."""
    result = _result(kind="fail", level="error", suppressions=[])
    assert derive_disposition(result) == Disposition.FAILING


def test_warning_disposition():
    """kind='fail', level='warning', no suppressions → WARNING (non-blocking)."""
    result = _result(kind="fail", level="warning", suppressions=[])
    assert derive_disposition(result) == Disposition.WARNING


def test_suppressed_disposition_insource():
    """kind='fail', inSource suppression → SUPPRESSED (own category)."""
    result = _result(kind="fail", level="error", suppressions=[_suppression()])
    assert derive_disposition(result) == Disposition.SUPPRESSED


def test_suppressed_disposition_external():
    """kind='fail', external suppression → SUPPRESSED."""
    suppression = Suppression(kind="external", justification="central allowlist entry")
    result = _result(kind="fail", level="error", suppressions=[suppression])
    assert derive_disposition(result) == Disposition.SUPPRESSED


# ---------------------------------------------------------------------------
# Key invariant: suppression takes precedence over level
# ---------------------------------------------------------------------------


def test_suppression_over_level_block_tier():
    """A block-tier (error) result with inSource suppression → SUPPRESSED, not FAILING.

    This is the critical invariant: inline justified suppressions are always
    counted in their own category regardless of the rule's enforcement tier.
    """
    result = _result(kind="fail", level="error", suppressions=[_suppression()])
    disposition = derive_disposition(result)
    assert disposition == Disposition.SUPPRESSED
    assert disposition != Disposition.FAILING


def test_suppression_over_level_warn_tier():
    """A warn-tier (warning) result with inSource suppression → SUPPRESSED, not WARNING."""
    result = _result(kind="fail", level="warning", suppressions=[_suppression()])
    assert derive_disposition(result) == Disposition.SUPPRESSED


# ---------------------------------------------------------------------------
# Level inheritance (None → "warning" default)
# ---------------------------------------------------------------------------


def test_no_level_defaults_to_warning():
    """level=None with no suppressions → WARNING (inherits 'warning' default)."""
    result = _result(kind="fail", level=None, suppressions=[])
    assert derive_disposition(result) == Disposition.WARNING


# ---------------------------------------------------------------------------
# Unknown / other kinds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["open", "review", "notApplicable"])
def test_unknown_kind_returns_none(kind: str):
    """Non-pass/fail kinds return None — consumers treat as non-blocking."""
    result = _result(kind=kind)
    assert derive_disposition(result) is None


# ---------------------------------------------------------------------------
# Gate exit-code derivation
# ---------------------------------------------------------------------------


def test_gate_exit_code_failing():
    """≥1 FAILING result → exit_code = 1."""
    results = [
        _result(kind="fail", level="error"),  # FAILING
        _result(kind="fail", level="warning"),  # WARNING
        _result(kind="pass"),  # PASS
    ]
    failing = [r for r in results if derive_disposition(r) == Disposition.FAILING]
    assert len(failing) == 1
    exit_code = 1 if failing else 0
    assert exit_code == 1


def test_gate_exit_code_clean():
    """No FAILING results → exit_code = 0 (warnings and suppressed don't block)."""
    results = [
        _result(kind="pass"),  # PASS
        _result(kind="fail", level="warning"),  # WARNING
        _result(
            kind="fail", level="error", suppressions=[_suppression()]
        ),  # SUPPRESSED
    ]
    failing = [r for r in results if derive_disposition(r) == Disposition.FAILING]
    assert len(failing) == 0
    exit_code = 1 if failing else 0
    assert exit_code == 0


# ---------------------------------------------------------------------------
# Type guard
# ---------------------------------------------------------------------------


def test_type_error_on_non_result():
    """Passing a non-Result raises TypeError."""
    with pytest.raises(TypeError):
        derive_disposition({"ruleId": "P001"})  # type: ignore[arg-type]
