"""Tests for the warn-only asset-validation hook in App.upload() (BLDX-1555).

Exercises the module-level ``_warn_on_invalid_transformed_assets`` helper
directly — it is pure with respect to the object store, so no App context or
Temporal runtime is needed. The helper is async (it offloads the blocking scan
to a worker thread via ``run_in_thread``), so tests await it.

On every *validated* upload the helper emits a structured
``ASSET_VALIDATION_EVENT`` (INFO) carrying per-axis counts and a compact
``asset_validation_matrix`` JSON attribute — allowlisted so it reaches OTLP /
ClickHouse. The human-readable WARNING (full ``format_report()``) is additionally
logged only when the batch is flagged. Uploads with nothing to validate (flag
off / not a ``transformed/`` subtree) emit neither.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pyatlan_v9.model.assets import Column, Database, Schema, Table

from application_sdk.app import base as base_module
from application_sdk.app.base import (
    ASSET_VALIDATION_EVENT,
    _warn_on_invalid_transformed_assets,
)
from application_sdk.observability.logger_adaptor import ASSET_VALIDATION_MATRIX_KEY

_HAS_ROCKSDICT = importlib.util.find_spec("rocksdict") is not None

APP = "test-app"
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


def _outcome_event(logger: MagicMock) -> dict:
    """Return the kwargs of the single ASSET_VALIDATION_EVENT info emission."""
    calls = [
        c
        for c in logger.info.call_args_list
        if c.args and c.args[0] == ASSET_VALIDATION_EVENT
    ]
    assert len(calls) == 1, f"expected exactly one outcome event, got {len(calls)}"
    return calls[0].kwargs


class TestWarnOnInvalidTransformedAssets:
    async def test_disabled_flag_is_noop(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch("application_sdk.constants.VALIDATE_ASSETS_ON_UPLOAD", False):
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            # Nothing validated -> no event, no denominator noise, no warning.
            logger.warning.assert_not_called()
            logger.info.assert_not_called()

    async def test_non_transformed_dir_is_noop(self, tmp_path: Path) -> None:
        # A directory with no transformed/ subtree — e.g. a raw upload.
        (tmp_path / "raw").mkdir()
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.warning.assert_not_called()
            logger.info.assert_not_called()

    async def test_empty_path_is_noop(self) -> None:
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets("", APP)
            logger.warning.assert_not_called()
            logger.info.assert_not_called()

    async def test_non_transformed_file_is_noop(self, tmp_path: Path) -> None:
        # A single file whose path has no ``transformed/`` segment — e.g. a raw
        # upload file. The file branch must return None (no event), mirroring
        # the directory analog above.
        raw_file = tmp_path / "raw" / "data.json"
        raw_file.parent.mkdir(parents=True, exist_ok=True)
        raw_file.write_bytes(_invalid_table().to_nested_bytes() + b"\n")
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(raw_file), APP)
            logger.warning.assert_not_called()
            logger.info.assert_not_called()

    async def test_valid_assets_emit_clean_event_no_warning(
        self, tmp_path: Path
    ) -> None:
        # Emit-always: a clean batch still emits the structured event (the
        # denominator) but logs no human WARNING.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.warning.assert_not_called()
            ev = _outcome_event(logger)
            assert ev["outcome"] == "clean"
            assert ev["app_name"] == APP
            assert ev["assets_total"] == 3
            assert ev["assets_passed"] == 3
            assert ev["assets_invalid"] == 0
            assert ev["assets_orphaned"] == 0
            assert ev["assets_undeserializable"] == 0
            # matrix is present and empty for a clean batch
            assert json.loads(ev[ASSET_VALIDATION_MATRIX_KEY]) == []

    async def test_invalid_asset_emits_flagged_event_and_warns(
        self, tmp_path: Path
    ) -> None:
        _write_transformed(tmp_path, "Table", [_invalid_table()])
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            # human WARNING for flagged batches still fires
            logger.warning.assert_called_once()
            ev = _outcome_event(logger)
            assert ev["outcome"] == "flagged"
            assert ev["assets_invalid"] == 1
            matrix = json.loads(ev[ASSET_VALIDATION_MATRIX_KEY])
            # A lone Table also orphans its (absent) parent Schema when the
            # referential pass runs, so filter to the per-asset invalid row.
            invalid_rows = [r for r in matrix if r["kind"] == "invalid"]
            assert len(invalid_rows) == 1
            assert invalid_rows[0]["type_name"] == "Table"
            assert invalid_rows[0]["error"]  # first .validate() message, non-empty

    @pytest.mark.skipif(not _HAS_ROCKSDICT, reason="orphan pass needs rocksdict")
    async def test_orphan_assets_warn_but_do_not_raise(self, tmp_path: Path) -> None:
        # BLDX-1555 decision: the upload hook runs the full referential pass by
        # default — extracts and transforms are full by design, so the batch is
        # complete and the orphan pass is accurate. A Column whose parent Table is
        # absent from the batch is an orphan -> warns + flagged event, never raises.
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
            # Must not raise.
            await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.warning.assert_called_once()
            assert "ORPHAN" in logger.warning.call_args.args[-1]
            ev = _outcome_event(logger)
            assert ev["outcome"] == "flagged"
            assert ev["assets_orphaned"] == 1
            matrix = json.loads(ev[ASSET_VALIDATION_MATRIX_KEY])
            orphan_rows = [r for r in matrix if r["kind"] == "orphan"]
            assert len(orphan_rows) == 1
            assert orphan_rows[0]["reference_count"] == 1

    async def test_transformed_dir_passed_directly_is_scanned(
        self, tmp_path: Path
    ) -> None:
        # local_path IS the transformed/ dir (not its parent). The "transformed"
        # in root.parts branch must still target and scan it.
        _write_transformed(tmp_path, "Table", [_invalid_table()])
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(
                str(tmp_path / "transformed"), APP
            )
            logger.warning.assert_called_once()
            assert _outcome_event(logger)["outcome"] == "flagged"

    async def test_file_path_under_transformed_is_scanned(self, tmp_path: Path) -> None:
        # local_path is a single file whose path contains a transformed/ segment.
        _write_transformed(tmp_path, "Table", [_invalid_table()])
        entities = tmp_path / "transformed" / "Table" / "entities.json"
        with patch.object(base_module, "_task_logger") as logger:
            await _warn_on_invalid_transformed_assets(str(entities), APP)
            logger.warning.assert_called_once()
            assert _outcome_event(logger)["outcome"] == "flagged"

    async def test_unexpected_scan_error_is_swallowed(self, tmp_path: Path) -> None:
        _valid_hierarchy(tmp_path)
        boom = MagicMock(side_effect=RuntimeError("boom"))
        with patch.object(base_module, "_task_logger") as logger:
            with patch("application_sdk.validation.validate_transformed_dir", boom):
                # Must not propagate the RuntimeError.
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            # Swallowed with a warning + traceback, upload continues; no event
            # (the scan produced no report).
            logger.warning.assert_called_once()
            assert logger.warning.call_args.kwargs.get("exc_info") is True
            logger.info.assert_not_called()

    async def test_emit_failure_is_swallowed(self, tmp_path: Path) -> None:
        # A defect in the emit path (e.g. matrix encoding) must never break the
        # upload: it is caught and downgraded to a warning, no raise.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch.object(
                base_module,
                "_validation_matrix_json",
                side_effect=RuntimeError("encode boom"),
            ):
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.warning.assert_called_once()
            assert logger.warning.call_args.kwargs.get("exc_info") is True
