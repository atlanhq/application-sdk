"""Tests for the warn-only asset-validation hook in App.upload() (BLDX-1555).

Exercises the module-level ``_warn_on_invalid_transformed_assets`` helper
directly — it is pure with respect to the object store, so no App context or
Temporal runtime is needed. The helper is async (it offloads the scan to an
isolated child process via ``run_best_effort``, CNCT-85), so tests await it.

The scan function is pickled by reference into a spawn child, so test doubles
for it must be module-level functions in this file — mocks and closures cannot
cross the process boundary.

On every *validated* upload the helper emits a structured
``ASSET_VALIDATION_EVENT`` (INFO) carrying per-axis counts and a compact
``asset_validation_matrix`` JSON attribute — allowlisted so it reaches OTLP /
ClickHouse. The human-readable WARNING (full ``format_report()``) is additionally
logged only when the batch is flagged. Uploads with nothing to validate (flag
off / not a ``transformed/`` subtree) — and scans that crash/time out — emit
neither.
"""

from __future__ import annotations

import asyncio
import faulthandler
import importlib.util
import json
import time
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


# Module-level scan doubles: run_best_effort pickles the scan function by
# reference into a spawn child, so a failing double must be an importable
# module-level function (a MagicMock cannot cross the process boundary).
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


def _valid_hierarchy_wide(base: Path, tables: int = 200) -> None:
    """A valid batch with a real, non-trivial number of assets to decode.

    The single-record ``_valid_hierarchy`` returns from the child almost
    instantly, leaving no window for concurrent submissions to overlap. Writing
    a few hundred real pyatlan_v9 tables makes each child scan take long enough
    that concurrently-submitted validations genuinely pile up on the pool — the
    load shape that mattered in production (see the concurrency regression test).
    """
    _write_transformed(
        base, "Database", [Database.creator(name="DB", connection_qualified_name=CONN)]
    )
    _write_transformed(
        base,
        "Schema",
        [Schema.creator(name="SCHEMA", database_qualified_name=f"{CONN}/DB")],
    )
    _write_transformed(
        base,
        "Table",
        [
            Table.creator(name=f"T{i}", schema_qualified_name=SCHEMA_QN)
            for i in range(tables)
        ],
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
    @pytest.fixture(autouse=True)
    def _enable_validation(self):
        # The production default is OFF (CNCT-85, see constants). These tests
        # exercise the enabled behavior, so force the flag on; the disabled-path
        # test re-patches it False for its own body.
        with patch("application_sdk.constants.VALIDATE_ASSETS_ON_UPLOAD", True):
            yield

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
            # _invalid_table() clears qualified_name, so the row must carry the
            # asset's actual .validate() message — pin a stable substring of it
            # rather than a bare truthiness check.
            assert "qualified_name is required" in invalid_rows[0]["error"]

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
        # A scan error must be swallowed (warn + traceback), the upload continues,
        # and no outcome event is emitted (the scan produced no report). The scan
        # runs in a spawn child, so the failing double is a picklable module-level
        # function, not a MagicMock.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch(
                "application_sdk.validation.validate_transformed_dir",
                _raise_runtime_error,
            ):
                # Must not propagate the RuntimeError (raised in the child,
                # re-raised here from the future, swallowed by run_best_effort).
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
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
            # The matrix is built as an eager arg to _task_logger.info(...), so a
            # raise there is hit before .info() is called — no partial outcome
            # event is emitted (mirrors test_unexpected_scan_error_is_swallowed).
            logger.info.assert_not_called()

    async def test_native_crash_warns_and_continues(self, tmp_path: Path) -> None:
        # CNCT-85: a segfault in the decode path (e.g. a native msgspec bug) is
        # not a Python exception — it must kill only the validation child, never
        # the worker. run_best_effort logs and the handoff proceeds; the scan
        # produced no report, so no outcome event is emitted.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch(
                "application_sdk.validation.validate_transformed_dir", _segfault
            ):
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.warning.assert_called_once()
            assert "subprocess died" in logger.warning.call_args.args[0]
            logger.info.assert_not_called()

    async def test_hung_validation_times_out_and_continues(
        self, tmp_path: Path
    ) -> None:
        # Warn-only validation must not be able to stall a handoff: a hung scan
        # is killed at the timeout and the upload proceeds; no report, no event.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with (
                patch("application_sdk.validation.validate_transformed_dir", _hang),
                patch("application_sdk.constants.VALIDATE_ASSETS_TIMEOUT_SECONDS", 1.0),
            ):
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.warning.assert_called_once()
            assert "timed out" in logger.warning.call_args.args[0]
            logger.info.assert_not_called()

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
                results = await asyncio.gather(
                    _warn_on_invalid_transformed_assets(str(hang_dir), APP),
                    _warn_on_invalid_transformed_assets(str(ok_dir), APP),
                )
            # The bug was the second caller (queued behind the hung one on the
            # single-worker pool) getting a CancelledError from the first
            # caller's timeout discard, which escaped the warn-only guards into
            # upload(). Assert the outcome directly: both callers return None
            # (warn-and-continue), neither raises. gather() without
            # return_exceptions would already propagate any raise, but naming
            # the contract makes the "survives" property explicit.
            assert results == [None, None]
            messages = [call.args[0] for call in logger.warning.call_args_list]
            # The hung caller is always killed at its timeout; the starved
            # second caller also warns and continues (either its own timeout or
            # the pool discard, whichever fires first), so both warned exactly
            # once and neither raised.
            assert any("timed out" in message for message in messages)
            assert len(messages) == 2

    async def test_concurrent_real_decodes_all_succeed_and_worker_survives(
        self, tmp_path: Path
    ) -> None:
        # THE regression for the CNCT-85 root cause, and the gap the pre-3.23.0
        # suite never covered. That suite decoded real assets only one call at a
        # time, and its *concurrency* coverage patched the decoder away
        # (_hang_if_marked / _segfault). So real msgspec was never exercised
        # under concurrent submission — the exact condition that killed the
        # worker in production: msgspec 0.20.0's concurrent-decode segfault on
        # py3.13, triggered when multiple upload validations decoded assets on
        # the worker's shared thread pool at once.
        #
        # This test closes that gap with NO decoder patch: it fans out many
        # validations of real pyatlan_v9 batches concurrently. It asserts the
        # fix's core claim end-to-end — process isolation serialises the decode
        # into a single spawn child, so concurrent *submission* never becomes
        # concurrent *decode*, the trigger is gone, every scan completes, and
        # the parent (the "worker") is still alive afterwards.
        #
        # NOTE: this only reproduces the original crash on the toxic combination
        # (py3.13 + msgspec 0.20.x). On other interpreters/versions it still
        # passes — as a coverage assertion it is only meaningful when CI runs it
        # on the SAME Python as the container image (3.13). That is why the e2e
        # job must pin the interpreter to the image's version, not the runner
        # default (3.11).
        concurrency = 8
        dirs = []
        for i in range(concurrency):
            batch_dir = tmp_path / f"upload_{i}"
            _valid_hierarchy_wide(batch_dir)
            dirs.append(batch_dir)

        with patch.object(base_module, "_task_logger") as logger:
            results = await asyncio.gather(
                *(_warn_on_invalid_transformed_assets(str(d), APP) for d in dirs)
            )
            # Every real, valid batch validated cleanly: warn-and-continue
            # returns None, and valid assets produce no warning at all (a clean
            # outcome event is emitted, but no WARNING). A native crash in any
            # child would have surfaced here as a "subprocess died" warning
            # (or, pre-fix, taken the process down).
            assert results == [None] * concurrency
            logger.warning.assert_not_called()

        # The parent process survived the concurrent burst: a fresh real
        # validation still works. (Pre-fix, a segfault in a worker thread would
        # have killed the whole worker, and there would be no "after".)
        with patch.object(base_module, "_task_logger") as logger_after:
            await _warn_on_invalid_transformed_assets(str(dirs[0]), APP)
            logger_after.warning.assert_not_called()


def _failure(i: int, *, deserialize_error: bool = False, error: str | None = None):
    from application_sdk.validation.assets import AssetValidationFailure

    return AssetValidationFailure(
        file="entities.json",
        line=i,
        type_name="Table",
        qualified_name=f"{TABLE_QN}_{i}",
        errors=[error if error is not None else f"bad {i}"],
        deserialize_error=deserialize_error,
    )


def _orphan(i: int):
    from application_sdk.validation.assets import ReferentialFailure

    return ReferentialFailure(
        missing_type_name="Table",
        missing_qualified_name=f"{SCHEMA_QN}/T_MISSING_{i}",
        reference_count=1,
        file="entities.json",
        line=i,
        type_name="Column",
        qualified_name=f"{SCHEMA_QN}/T_MISSING_{i}/C1",
        relationship="table",
    )


def test_matrix_is_bounded_to_max_rows_per_axis() -> None:
    """The matrix caps at ``_VALIDATION_MATRIX_MAX_ROWS`` rows *per axis*, so a
    pathological batch cannot produce an unbounded LogAttributes value. The report
    still carries the true totals — only the drill-down sample is bounded."""
    from application_sdk.validation.assets import AssetValidationReport

    cap = base_module._VALIDATION_MATRIX_MAX_ROWS
    n = cap + 5
    report = AssetValidationReport(
        total=2 * n,
        passed=0,
        failures=[_failure(i) for i in range(n)],
        orphans=[_orphan(i) for i in range(n)],
    )

    matrix = json.loads(base_module._validation_matrix_json(report))
    invalid_rows = [r for r in matrix if r["kind"] == "invalid"]
    orphan_rows = [r for r in matrix if r["kind"] == "orphan"]
    assert len(invalid_rows) == cap
    assert len(orphan_rows) == cap
    assert len(matrix) == 2 * cap
    # Scalar totals are unbounded (full batch), only the matrix is sampled.
    assert report.failed == n
    assert len(report.orphans) == n


def test_matrix_marks_undeserializable_rows() -> None:
    """A failure carrying ``deserialize_error=True`` must surface in the matrix as
    ``kind="undeserializable"`` (the branch the emitter splits on), never
    ``"invalid"`` — so a dashboard can tell decode failures from per-asset
    ``.validate()`` failures. The scalar tests only assert the ``0`` count, so
    without this the matrix branch is unexercised."""
    from application_sdk.validation.assets import AssetValidationReport

    report = AssetValidationReport(
        total=1,
        passed=0,
        failures=[_failure(0, deserialize_error=True)],
        orphans=[],
    )

    matrix = json.loads(base_module._validation_matrix_json(report))
    assert [r["kind"] for r in matrix] == ["undeserializable"]


def test_matrix_truncates_long_error_to_maxlen() -> None:
    """Per-row error text is clipped to ``_VALIDATION_MATRIX_ERROR_MAXLEN`` so a
    single pathological ``.validate()`` message cannot bloat the ClickHouse
    attribute. Pin the length so a future refactor of the slice can't silently
    drop the guard."""
    from application_sdk.validation.assets import AssetValidationReport

    maxlen = base_module._VALIDATION_MATRIX_ERROR_MAXLEN
    report = AssetValidationReport(
        total=1,
        passed=0,
        failures=[_failure(0, error="x" * (maxlen + 50))],
        orphans=[],
    )

    matrix = json.loads(base_module._validation_matrix_json(report))
    assert len(matrix[0]["error"]) == maxlen


def test_asset_validation_outcome_keys_in_allowlist() -> None:
    """The PR's core promise — the structured outcome attributes reach OTLP /
    ClickHouse — holds only while these keys stay allowlisted. Pin the six new
    validation-outcome keys the emitter relies on (mirrors the storage-op and
    file_ref allowlist guards in tests/unit/observability/test_logger_adaptor.py)."""
    from application_sdk.observability.logger_adaptor import _KNOWN_EXTRA_KEYS

    required = {
        ASSET_VALIDATION_MATRIX_KEY,
        "assets_total",
        "assets_passed",
        "assets_invalid",
        "assets_orphaned",
        "assets_undeserializable",
    }
    missing = required - _KNOWN_EXTRA_KEYS
    assert not missing, f"_KNOWN_EXTRA_KEYS missing validation keys: {missing}"
