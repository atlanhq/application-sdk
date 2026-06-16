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


def test_nonempty_skipped_when_no_expectations() -> None:
    # Nothing declared -> the non-empty backstop must not false-fail.
    assert _NoExpectations()._evaluate_asset_expectations({}) == []
