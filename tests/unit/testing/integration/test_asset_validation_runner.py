"""Tests for BaseIntegrationTest's Step 7 asset validation (BLDX-1555).

Exercises ``BaseIntegrationTest._validate_assets`` directly against a fixture
run directory, so no live server or Temporal runtime is needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import pytest
from pyatlan_v9.model.assets import Column, Database, Schema, Table

from application_sdk.testing.integration import BaseIntegrationTest, Scenario, equals
from application_sdk.testing.integration import runner as runner_module
from application_sdk.testing.integration.models import APIType
from application_sdk.testing.integration.runner import _needs_asset_validation

_HAS_ROCKSDICT = importlib.util.find_spec("rocksdict") is not None
requires_rocksdict = pytest.mark.skipif(
    not _HAS_ROCKSDICT, reason="orphan pass needs rocksdict (the [storage] extra)"
)

CONN = "default/snow/123"
SCHEMA_QN = f"{CONN}/DB/SCHEMA"
TABLE_QN = f"{SCHEMA_QN}/T1"
WORKFLOW_ID = "wf-1"
RUN_ID = "run-1"


def _write(base: Path, entity: str, assets: list) -> None:
    out_dir = base / WORKFLOW_ID / RUN_ID / "transformed" / entity
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "entities.json", "wb") as handle:
        for asset in assets:
            handle.write(asset.to_nested_bytes())
            handle.write(b"\n")


def _layout_with_orphan(base: Path) -> None:
    _write(
        base, "Database", [Database.creator(name="DB", connection_qualified_name=CONN)]
    )
    _write(
        base,
        "Schema",
        [Schema.creator(name="SCHEMA", database_qualified_name=f"{CONN}/DB")],
    )
    _write(base, "Table", [Table.creator(name="T1", schema_qualified_name=SCHEMA_QN)])
    # Column pointing at a Table that is not present -> orphan.
    _write(
        base,
        "Column",
        [
            Column.creator(
                name="C1",
                parent_type=Table,
                parent_qualified_name=f"{SCHEMA_QN}/T_MISSING",
                order=1,
            )
        ],
    )


def _scenario(**overrides) -> Scenario:
    return Scenario(
        name="wf",
        api="workflow",
        assert_that={"success": equals(True)},
        **overrides,
    )


_RESPONSE = {"data": {"workflow_id": WORKFLOW_ID, "run_id": RUN_ID}}


@requires_rocksdict
class TestRunnerAssetValidation:
    def test_warn_first_does_not_raise(self, tmp_path: Path) -> None:
        _layout_with_orphan(tmp_path)
        suite = BaseIntegrationTest()
        with patch.object(runner_module, "logger") as logger:
            # Default warn-first: must not raise.
            suite._validate_assets(_scenario(), _RESPONSE, str(tmp_path))
            logger.warning.assert_called_once()
            assert "ORPHAN" in logger.warning.call_args.args[-1]

    def test_strict_class_attr_raises(self, tmp_path: Path) -> None:
        _layout_with_orphan(tmp_path)

        class StrictSuite(BaseIntegrationTest):
            asset_validation_strict = True

        with pytest.raises(AssertionError, match="ORPHAN"):
            StrictSuite()._validate_assets(_scenario(), _RESPONSE, str(tmp_path))

    def test_strict_scenario_override_raises(self, tmp_path: Path) -> None:
        _layout_with_orphan(tmp_path)
        suite = BaseIntegrationTest()  # class default is warn-first
        with pytest.raises(AssertionError, match="ORPHAN"):
            suite._validate_assets(
                _scenario(asset_validation_strict=True), _RESPONSE, str(tmp_path)
            )

    def test_valid_batch_passes_quietly(self, tmp_path: Path) -> None:
        _write(
            tmp_path,
            "Database",
            [Database.creator(name="DB", connection_qualified_name=CONN)],
        )
        _write(
            tmp_path,
            "Schema",
            [Schema.creator(name="SCHEMA", database_qualified_name=f"{CONN}/DB")],
        )
        _write(
            tmp_path,
            "Table",
            [Table.creator(name="T1", schema_qualified_name=SCHEMA_QN)],
        )
        suite = BaseIntegrationTest()
        with patch.object(runner_module, "logger") as logger:
            suite._validate_assets(_scenario(), _RESPONSE, str(tmp_path))
            logger.warning.assert_not_called()


class TestRunnerAssetValidationGuards:
    def test_missing_workflow_id_skips(self, tmp_path: Path) -> None:
        suite = BaseIntegrationTest()
        with patch.object(runner_module, "logger") as logger:
            # No workflow_id/run_id -> skip silently, no raise, no warning.
            suite._validate_assets(_scenario(), {"data": {}}, str(tmp_path))
            logger.warning.assert_not_called()


class TestNeedsAssetValidationGate:
    """The outer Step-7 gate that decides whether _validate_assets runs at all."""

    def test_enabled_workflow_with_path_runs(self) -> None:
        assert _needs_asset_validation(
            validate_assets=True, api_type=APIType.WORKFLOW, asset_base_path="/tmp/out"
        )

    def test_disabled_flag_skips(self) -> None:
        assert not _needs_asset_validation(
            validate_assets=False, api_type=APIType.WORKFLOW, asset_base_path="/tmp/out"
        )

    def test_non_workflow_api_skips(self) -> None:
        assert not _needs_asset_validation(
            validate_assets=True, api_type=APIType.AUTH, asset_base_path="/tmp/out"
        )

    def test_missing_base_path_skips(self) -> None:
        assert not _needs_asset_validation(
            validate_assets=True, api_type=APIType.WORKFLOW, asset_base_path=None
        )
        assert not _needs_asset_validation(
            validate_assets=True, api_type=APIType.WORKFLOW, asset_base_path=""
        )
