"""Tests for output-floor validation (asset count / expected types).

Covers the M1/M2 additions:
  * ``Scenario.assert_min_total_assets`` / ``Scenario.expected_asset_types`` model validation.
  * ``BaseIntegrationTest._validate_output_floor`` count + type assertions.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from application_sdk.testing.integration import BaseIntegrationTest, Scenario

_LOAD = "application_sdk.testing.integration.runner.load_actual_output"


def _wf_response() -> dict:
    return {"data": {"workflow_id": "wf-1", "run_id": "run-1"}}


def _scenario(**kwargs) -> Scenario:
    return Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": lambda v: True},
        **kwargs,
    )


class _Runner(BaseIntegrationTest):
    scenarios: list = []
    extracted_output_base_path = "/tmp/out"


class _NoPathRunner(BaseIntegrationTest):
    scenarios: list = []
    extracted_output_base_path = None


# --------------------------------------------------------------------------
# Scenario model validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize("api", ["auth", "metadata", "preflight"])
def test_min_total_only_for_workflow(api: str) -> None:
    with pytest.raises(ValueError, match="workflow"):
        Scenario(
            name="x",
            api=api,
            assert_that={"success": lambda v: True},
            assert_min_total_assets=1,
        )


def test_expected_types_only_for_workflow() -> None:
    with pytest.raises(ValueError, match="workflow"):
        Scenario(
            name="x",
            api="metadata",
            assert_that={"success": lambda v: True},
            expected_asset_types={"Table"},
        )


def test_negative_min_total_raises() -> None:
    with pytest.raises(ValueError, match=">= 0"):
        _scenario(assert_min_total_assets=-1)


def test_valid_workflow_floor_ok() -> None:
    sc = _scenario(assert_min_total_assets=2, expected_asset_types={"Table"})
    assert sc.assert_min_total_assets == 2
    assert sc.expected_asset_types == {"Table"}


# --------------------------------------------------------------------------
# _validate_output_floor — count (M1)
# --------------------------------------------------------------------------


def test_min_total_passes_with_assets() -> None:
    runner = _Runner()
    with patch(_LOAD, return_value=[{"typeName": "Table"}, {"typeName": "Column"}]):
        runner._validate_output_floor(_scenario(), _wf_response(), min_total=1)


def test_min_total_fails_on_zero() -> None:
    runner = _Runner()
    with patch(_LOAD, return_value=[]):
        with pytest.raises(AssertionError, match="at least 1 asset"):
            runner._validate_output_floor(_scenario(), _wf_response(), min_total=1)


def test_min_total_fails_when_below_floor() -> None:
    runner = _Runner()
    with patch(_LOAD, return_value=[{"typeName": "Table"}]):
        with pytest.raises(AssertionError, match="at least 5"):
            runner._validate_output_floor(_scenario(), _wf_response(), min_total=5)


# --------------------------------------------------------------------------
# _validate_output_floor — expected types (M2)
# --------------------------------------------------------------------------


def test_expected_types_present_passes() -> None:
    runner = _Runner()
    actual = [{"typeName": "Database"}, {"typeName": "Table"}, {"typeName": "Table"}]
    with patch(_LOAD, return_value=actual):
        runner._validate_output_floor(
            _scenario(), _wf_response(), required_types={"Database", "Table"}
        )


def test_expected_types_missing_fails() -> None:
    runner = _Runner()
    with patch(_LOAD, return_value=[{"typeName": "Database"}]):
        with pytest.raises(AssertionError, match="Column"):
            runner._validate_output_floor(
                _scenario(), _wf_response(), required_types={"Database", "Column"}
            )


# --------------------------------------------------------------------------
# _validate_output_floor — base-path handling
# --------------------------------------------------------------------------


def test_soft_skip_when_no_base_path() -> None:
    runner = _NoPathRunner()
    with patch(_LOAD) as load:
        # Soft mode: warn + skip, never touch the output dir.
        runner._validate_output_floor(
            _scenario(), _wf_response(), min_total=1, soft_if_no_base_path=True
        )
        load.assert_not_called()


def test_hard_fail_when_no_base_path() -> None:
    runner = _NoPathRunner()
    with pytest.raises(AssertionError, match="extracted_output_base_path not set"):
        runner._validate_output_floor(_scenario(), _wf_response(), min_total=1)


def test_missing_workflow_ids_fails() -> None:
    runner = _Runner()
    with pytest.raises(AssertionError, match="workflow_id or run_id"):
        runner._validate_output_floor(_scenario(), {"data": {}}, min_total=1)
