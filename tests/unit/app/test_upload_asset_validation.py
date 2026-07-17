"""Tests for the warn-only asset-validation hook in App.upload() (BLDX-1555).

Exercises the module-level ``_warn_on_invalid_transformed_assets`` helper
directly — it is pure with respect to the object store, so no App context or
Temporal runtime is needed. The helper is async (it offloads the scan to an
isolated child process via ``run_in_process``, CNCT-85), so tests await it.

The scan function is pickled by reference into a spawn child, so test doubles
for it must be module-level functions in this file — mocks and closures cannot
cross the process boundary.
"""

from __future__ import annotations

import asyncio
import faulthandler
import importlib.util
import time
from pathlib import Path
from unittest.mock import patch

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


def _raise_runtime_error(path, **kwargs):
    raise RuntimeError("boom")


def _segfault(path, **kwargs):
    # Not ctypes.string_at(0): on Windows ctypes converts the access violation
    # to OSError instead of dying. faulthandler's test hook faults for real on
    # every platform.
    faulthandler._sigsegv()


def _hang(path, **kwargs):
    time.sleep(3600)


def _hang_if_marked(path, **kwargs):
    if "hangme" in str(path):
        time.sleep(3600)
    return None


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

    @pytest.mark.skipif(not _HAS_ROCKSDICT, reason="orphan pass needs rocksdict")
    async def test_orphan_assets_warn_but_do_not_raise(self, tmp_path: Path) -> None:
        # BLDX-1555 decision: the upload hook runs the full referential pass by
        # default — extracts and transforms are full by design, so the batch is
        # complete and the orphan pass is accurate. A Column whose parent Table is
        # absent from the batch is an orphan -> warns, never raises.
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
            await _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_called_once()
            assert "ORPHAN" in logger.warning.call_args.args[-1]

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
        with patch.object(base_module, "_task_logger") as logger:
            with patch(
                "application_sdk.validation.validate_transformed_dir",
                _raise_runtime_error,
            ):
                # Must not propagate the RuntimeError (raised in the child,
                # re-raised here from the future).
                await _warn_on_invalid_transformed_assets(str(tmp_path))
            # Swallowed with a warning + traceback, upload continues.
            logger.warning.assert_called_once()
            assert logger.warning.call_args.kwargs.get("exc_info") is True

    async def test_native_crash_warns_and_continues(self, tmp_path: Path) -> None:
        # CNCT-85: a segfault in the decode path (e.g. a native msgspec bug) is
        # not a Python exception — it must kill only the validation child, never
        # the worker. The hook logs and the handoff proceeds.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch(
                "application_sdk.validation.validate_transformed_dir", _segfault
            ):
                await _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_called_once()
            assert "subprocess died" in logger.warning.call_args.args[0]

    async def test_concurrent_upload_survives_anothers_timeout(
        self, tmp_path: Path
    ) -> None:
        # Reviewer finding on the first cut of CNCT-85: with the shared
        # single-worker pool, a second upload's validation queued behind a
        # hung one was cancelled by the first one's timeout discard, and the
        # CancelledError (a BaseException) escaped the warn-only guards into
        # upload(). Both callers must complete without raising.
        hang_dir = tmp_path / "hangme"
        ok_dir = tmp_path / "ok"
        for base in (hang_dir, ok_dir):
            _valid_hierarchy(base)
        with patch.object(base_module, "_task_logger") as logger:
            with (
                patch(
                    "application_sdk.validation.validate_transformed_dir",
                    _hang_if_marked,
                ),
                patch("application_sdk.constants.VALIDATE_ASSETS_TIMEOUT_SECONDS", 1.0),
            ):
                await asyncio.gather(
                    _warn_on_invalid_transformed_assets(str(hang_dir)),
                    _warn_on_invalid_transformed_assets(str(ok_dir)),
                )
            messages = [call.args[0] for call in logger.warning.call_args_list]
            assert any("timed out" in message for message in messages)

    async def test_hung_validation_times_out_and_continues(
        self, tmp_path: Path
    ) -> None:
        # Warn-only validation must not be able to stall a handoff: a hung scan
        # is killed at the timeout and the upload proceeds.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with (
                patch("application_sdk.validation.validate_transformed_dir", _hang),
                patch("application_sdk.constants.VALIDATE_ASSETS_TIMEOUT_SECONDS", 1.0),
            ):
                await _warn_on_invalid_transformed_assets(str(tmp_path))
            logger.warning.assert_called_once()
            assert "timed out" in logger.warning.call_args.args[0]
