"""Tests for the warn-only asset-validation hook in App.upload() (BLDX-1555).

Exercises the module-level ``_warn_on_invalid_transformed_assets`` helper
directly — it is pure with respect to the object store, so no App context or
Temporal runtime is needed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pyatlan_v9.model.assets import Column, Database, Schema, Table

from application_sdk.app import base as base_module
from application_sdk.app.base import _warn_on_invalid_transformed_assets

_HAS_ROCKSDICT = importlib.util.find_spec("rocksdict") is not None

CONN = "default/snow/123"
SCHEMA_QN = f"{CONN}/DB/SCHEMA"
TABLE_QN = f"{SCHEMA_QN}/T1"


def _write_transformed(base: Path, entity: str, assets: list) -> None:
    out_dir = base / "transformed" / entity
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "entities.json", "wb") as handle:
        for asset in assets:
            handle.write(asset.to_nested_bytes())
            handle.write(b"\n")


def _valid_hierarchy(base: Path) -> None:
    _write_transformed(
        base, "Database", [Database.creator(name="DB", connection_qualified_name=CONN)]
    )
    _write_transformed(
        base,
        "Schema",
        [Schema.creator(name="SCHEMA", database_qualified_name=f"{CONN}/DB")],
    )
    _write_transformed(
        base, "Table", [Table.creator(name="T1", schema_qualified_name=SCHEMA_QN)]
    )


class TestWarnOnInvalidTransformedAssets:
    def test_disabled_flag_is_noop(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch("application_sdk.constants.VALIDATE_ASSETS_ON_UPLOAD", False):
                _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_not_called()

    def test_non_transformed_dir_is_noop(self, tmp_path: Path) -> None:
        # A directory with no transformed/ subtree — e.g. a raw upload.
        (tmp_path / "raw").mkdir()
        with patch.object(base_module, "_task_logger") as logger:
            _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_not_called()

    def test_empty_path_is_noop(self) -> None:
        with patch.object(base_module, "_task_logger") as logger:
            _warn_on_invalid_transformed_assets("")
            logger.warning.assert_not_called()

    @pytest.mark.skipif(not _HAS_ROCKSDICT, reason="orphan pass needs rocksdict")
    def test_orphan_assets_warn_but_do_not_raise(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        # Column pointing at a Table that is not present -> orphan.
        _write_transformed(
            tmp_path,
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
        with patch.object(base_module, "_task_logger") as logger:
            # Must not raise.
            _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_called_once()
            assert "ORPHAN" in logger.warning.call_args.args[-1]

    def test_valid_assets_do_not_warn(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_not_called()

    def test_unexpected_error_is_swallowed(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        boom = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(base_module, "_task_logger") as logger:
            with patch("application_sdk.validation.validate_transformed_dir", boom):
                # Must not propagate the RuntimeError.
                _warn_on_invalid_transformed_assets(str(tmp_path))
            # Swallowed with a warning + traceback, upload continues.
            logger.warning.assert_called_once()
            assert logger.warning.call_args.kwargs.get("exc_info") is True
