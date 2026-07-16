"""Tests for the warn-only asset-validation hook in App.upload() (BLDX-1555).

Exercises the module-level ``_warn_on_invalid_transformed_assets`` helper
directly — it is pure with respect to the object store, so no App context or
Temporal runtime is needed. The helper is async (it offloads the blocking scan
to a worker thread via ``run_in_thread``), so tests await it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from pyatlan_v9.model.assets import Column, Database, Schema, Table

from application_sdk.app import base as base_module
from application_sdk.app.base import _warn_on_invalid_transformed_assets

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


def _invalid_table() -> Table:
    # Per-asset invalid: qualified_name cleared, caught by pyatlan_v9 .validate().
    table = Table.creator(name="T1", schema_qualified_name=SCHEMA_QN)
    table.qualified_name = None
    return table


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
    async def test_disabled_flag_is_noop(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch("application_sdk.constants.VALIDATE_ASSETS_ON_UPLOAD", False):
                await _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_not_called()

    async def test_non_transformed_dir_is_noop(self, tmp_path: Path) -> None:
        # A directory with no transformed/ subtree — e.g. a raw upload.
        (tmp_path / "raw").mkdir()
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_not_called()

    async def test_empty_path_is_noop(self) -> None:
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets("")
            logger.warning.assert_not_called()

    async def test_non_transformed_file_is_noop(self, tmp_path: Path) -> None:
        # A single file whose path has no ``transformed/`` segment — e.g. a raw
        # upload file. The file branch must return None (no warning), mirroring
        # the directory analog above.
        raw_file = tmp_path / "raw" / "data.json"
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_bytes(_invalid_table().to_nested_bytes() + b"\n")
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(raw_file))
            logger.warning.assert_not_called()

    async def test_orphan_pass_is_scoped_out_of_upload_hook(
        self, tmp_path: Path
    ) -> None:
        # BLDX-1555 decision: the referential (orphan) pass is NOT run on the
        # production upload hook — it assumes a complete batch, which incremental
        # and partial uploads cannot guarantee, so it would false-positive. A
        # Column whose parent Table is absent from the batch is otherwise valid,
        # so the hook must stay silent (the orphan pass lives in the integration
        # runner, where the full run output is known).
        _valid_hierarchy(tmp_path)
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
            await _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_not_called()

    async def test_transformed_dir_passed_directly_is_scanned(
        self, tmp_path: Path
    ) -> None:
        # local_path IS the transformed/ dir (not its parent). The "transformed"
        # in root.parts branch must still target and scan it.
        _write_transformed(tmp_path, "Table", [_invalid_table()])
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(tmp_path / "transformed"))
            logger.warning.assert_called_once()

    async def test_file_path_under_transformed_is_scanned(self, tmp_path: Path) -> None:
        # local_path is a single file whose path contains a transformed/ segment.
        _write_transformed(tmp_path, "Table", [_invalid_table()])
        entities = tmp_path / "transformed" / "Table" / "entities.json"
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(entities))
            logger.warning.assert_called_once()

    async def test_valid_assets_do_not_warn(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_not_called()

    async def test_unexpected_error_is_swallowed(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        boom = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(base_module, "_task_logger") as logger:
            with patch("application_sdk.validation.validate_transformed_dir", boom):
                # Must not propagate the RuntimeError.
                await _warn_on_invalid_transformed_assets(str(tmp_path))
            # Swallowed with a warning + traceback, upload continues.
            logger.warning.assert_called_once()
            assert logger.warning.call_args.kwargs.get("exc_info") is True
