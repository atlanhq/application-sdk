"""Tests for BaseE2ETest._evaluate_asset_expectations (floors + exact parity + non-empty).

Pure logic — no tenant / Atlas needed; subclasses set class attrs and we call
the evaluator directly with a fake Atlas asset-count dict.
"""

from __future__ import annotations

from application_sdk.testing.e2e import BaseE2ETest


class _Floors(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expected_min_asset_counts = {"Database": 1, "Table": 5}
    expect_lineage = False


class _Exact(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expected_exact_counts = {"Database": 1, "Schema": 3}
    expect_lineage = False


class _NonEmpty(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expected_min_asset_counts = {"Database": 1}
    expect_lineage = False


class _NoExpectations(BaseE2ETest):
    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expect_lineage = False


class _Both(BaseE2ETest):
    """Most realistic connector config: floors AND exact-count parity."""

    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expected_min_asset_counts = {"Database": 1, "Table": 5}
    expected_exact_counts = {"Schema": 3}
    expect_lineage = False


class _NonEmptyOptOut(BaseE2ETest):
    """Floors declared but the non-empty backstop explicitly opted out."""

    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expected_min_asset_counts = {"Database": 1}
    require_nonempty_assets = False
    expect_lineage = False


class _ExactZero(BaseE2ETest):
    """Connector asserting it produces exactly zero of a type."""

    connector_short_name = "x"
    argo_package_name = "@atlan/x"
    argo_template_name = "t"
    expected_exact_counts = {"PlaceholderType": 0}
    expect_lineage = False


# --- floors (existing behaviour, preserved) --------------------------------


def test_floors_met_passes() -> None:
    assert _Floors()._evaluate_asset_expectations({"Database": 1, "Table": 7}) == []


def test_floors_shortfall_fails() -> None:
    failures = _Floors()._evaluate_asset_expectations({"Database": 1, "Table": 2})
    assert any("Table" in f and ">= 5" in f for f in failures)


# --- exact count parity (M3) -----------------------------------------------


def test_exact_counts_match_passes() -> None:
    assert _Exact()._evaluate_asset_expectations({"Database": 1, "Schema": 3}) == []


def test_exact_over_extraction_fails() -> None:
    failures = _Exact()._evaluate_asset_expectations({"Database": 1, "Schema": 5})
    assert any("Schema" in f and "exactly 3" in f for f in failures)


def test_exact_under_extraction_fails() -> None:
    failures = _Exact()._evaluate_asset_expectations({"Database": 1, "Schema": 1})
    assert any("Schema" in f and "exactly 3" in f for f in failures)


# --- non-empty backstop (M1) -----------------------------------------------


def test_nonempty_zero_with_expectations_fails() -> None:
    failures = _NonEmpty()._evaluate_asset_expectations({})
    assert any("ZERO assets" in f for f in failures)


def test_nonempty_fires_without_expectations_when_zero() -> None:
    # Even with nothing declared, a completed run that lands zero assets trips
    # the backstop — that population is the one most likely to silently regress.
    failures = _NoExpectations()._evaluate_asset_expectations({}, total_assets=0)
    assert any("ZERO assets" in f for f in failures)


def test_nonempty_without_expectations_passes_when_assets_present() -> None:
    # The true total (all types) is what the backstop checks, not the probed
    # per-type dict — so a no-expectations connector with real assets passes.
    assert (
        _NoExpectations()._evaluate_asset_expectations({}, total_assets=5) == []
    )


def test_nonempty_opt_out_emits_only_floor_failure() -> None:
    # require_nonempty_assets = False opts out of the backstop: a zero-asset run
    # surfaces only the floor shortfall, not the ZERO-assets line.
    failures = _NonEmptyOptOut()._evaluate_asset_expectations({})
    assert any("Database" in f and ">= 1" in f for f in failures)
    assert not any("ZERO assets" in f for f in failures)


def test_exact_zero_baseline_passes_with_no_assets() -> None:
    # A connector asserting "exactly 0 of X" + zero assets must pass; the
    # backstop must not override an explicit zero expectation.
    assert _ExactZero()._evaluate_asset_expectations({}, total_assets=0) == []


# --- floors + exacts on the same class (most realistic config) -------------


def test_both_floors_and_exacts_emit_both_failures() -> None:
    # Table short of its floor AND Schema off its exact → both lines present,
    # confirming neither failure path shadows the other.
    failures = _Both()._evaluate_asset_expectations(
        {"Database": 1, "Table": 2, "Schema": 5}
    )
    assert any("Table" in f and ">= 5" in f for f in failures)
    assert any("Schema" in f and "exactly 3" in f for f in failures)


def test_both_all_met_passes() -> None:
    assert (
        _Both()._evaluate_asset_expectations({"Database": 1, "Table": 5, "Schema": 3})
        == []
    )
