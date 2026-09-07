"""Tests for the warn-only asset-validation hook in App.upload() (BLDX-1555).

Exercises the module-level ``_warn_on_invalid_transformed_assets`` helper
directly — it is pure with respect to the object store, so no App context or
Temporal runtime is needed. The helper is async (it offloads the scan to an
isolated child process via ``run_best_effort``, CNCT-85), so tests await it.

The scan function is pickled by reference into a spawn child, so test doubles
for it must be module-level functions in this file — mocks and closures cannot
cross the process boundary.

Since FND-690 the check is reached through the generic artifact wrapper —
``validate_artifact(target, ModelSource(model=Asset))``, the NDJSON x ``ModelSource``
cell — so that is what the offloaded scan doubles below patch. The cell *is*
``validate_transformed_dir``, so every assertion here is about the same walk it
always was.

On every *validated* upload the helper emits a structured
``ASSET_VALIDATION_EVENT`` (INFO) carrying per-axis counts and a compact
``asset_validation_matrix`` JSON attribute — allowlisted so it reaches OTLP /
ClickHouse. Its name and every attribute key are a shipped contract, pinned
byte-for-byte in ``tests/unit/validation/test_asset_event.py``. The human-readable
WARNING (full ``format_report()``) is additionally logged only when the batch is
flagged. Uploads with nothing to validate (flag off / not a ``transformed/``
subtree) — and scans that crash/time out — emit neither.
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
def _raise_runtime_error(path, source, **kwargs):
    raise RuntimeError("boom")


def _segfault(path, source, **kwargs):
    # Not ctypes.string_at(0): on Windows ctypes converts the access violation
    # to OSError instead of dying. faulthandler's test hook faults for real on
    # every platform.
    faulthandler._sigsegv()


def _hang(path, source, **kwargs):
    time.sleep(3600)


def _absent_report(path, source, **kwargs):
    # What the wrapper returns when a plug-in seam breaks: a report, never a raise.
    from application_sdk.validation import ArtifactValidationReport

    return ArtifactValidationReport.absent(reason="validator raised: RuntimeError")


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
        # Force the flag on so these tests are independent of the env default
        # (this branch defaults it ON; main currently defaults it OFF). The
        # disabled-path test re-patches it False for its own body.
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
                "application_sdk.validation.validate_artifact",
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
        # upload: it is caught and downgraded to a warning, no raise. The hook
        # resolves the projection lazily at call time, so patching it on the
        # validation package is what the hook actually looks up.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch(
                "application_sdk.validation.asset_validation_event_fields",
                side_effect=RuntimeError("encode boom"),
            ):
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.warning.assert_called_once()
            assert logger.warning.call_args.kwargs.get("exc_info") is True
            # The attribute map is built as an eager arg to _task_logger.info(...),
            # so a raise there is hit before .info() is called — no partial outcome
            # event is emitted (mirrors test_unexpected_scan_error_is_swallowed).
            logger.info.assert_not_called()

    async def test_a_wrapper_outcome_without_counts_emits_no_event(
        self, tmp_path: Path
    ) -> None:
        # The wrapper never raises, so a validator that blows up arrives as a plain
        # `absent` report rather than as an exception — and a plain report carries no
        # asset-vocabulary counts. Emitting zeros there would put a fabricated
        # denominator on the board, so the hook names the skip and emits nothing.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch(
                "application_sdk.validation.validate_artifact",
                _absent_report,
            ):
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.info.assert_not_called()
            logger.warning.assert_called_once()
            assert "produced no counts" in logger.warning.call_args.args[0]

    async def test_native_crash_warns_and_continues(self, tmp_path: Path) -> None:
        # CNCT-85: a segfault in the decode path (e.g. a native msgspec bug) is
        # not a Python exception — it must kill only the validation child, never
        # the worker. run_best_effort logs and the handoff proceeds; the scan
        # produced no report, so no outcome event is emitted.
        _valid_hierarchy(tmp_path)
        with patch.object(base_module, "_task_logger") as logger:
            with patch("application_sdk.validation.validate_artifact", _segfault):
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
                patch("application_sdk.validation.validate_artifact", _hang),
                patch("application_sdk.constants.VALIDATE_ASSETS_TIMEOUT_SECONDS", 1.0),
            ):
                await _warn_on_invalid_transformed_assets(str(tmp_path), APP)
            logger.warning.assert_called_once()
            assert "timed out" in logger.warning.call_args.args[0]
            logger.info.assert_not_called()

    async def test_concurrent_uploads_both_survive_timeouts(
        self, tmp_path: Path
    ) -> None:
        # Reviewer finding on the first cut of CNCT-85: when one caller's timeout
        # discarded the shared pool, a concurrent caller's future could surface
        # as CancelledError (a BaseException) and escape the warn-only guards
        # into upload(). The pool is now multi-worker, so both run concurrently.
        # Run two validations that both hang past the timeout: each is either
        # killed by its own timeout or by the other's pool-discard — either way
        # it must warn-and-continue (return None), never raise.
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        for base in (dir_a, dir_b):
            _valid_hierarchy(base)
        with patch.object(base_module, "_task_logger") as logger:
            with (
                patch("application_sdk.validation.validate_artifact", _hang),
                patch("application_sdk.constants.VALIDATE_ASSETS_TIMEOUT_SECONDS", 1.0),
            ):
                results = await asyncio.gather(
                    _warn_on_invalid_transformed_assets(str(dir_a), APP),
                    _warn_on_invalid_transformed_assets(str(dir_b), APP),
                )
            # Both callers warn-and-continue (return None), neither raises.
            # gather() without return_exceptions would already propagate any
            # raise, but naming the contract makes the "survives" property
            # explicit — in particular that no CancelledError leaked out.
            assert results == [None, None]
            messages = [call.args[0] for call in logger.warning.call_args_list]
            # Both hung, so both were killed and warned exactly once; at least
            # the first to fire reports its own timeout.
            assert any("timed out" in message for message in messages)
            assert len(messages) == 2

    async def test_concurrent_real_decodes_all_succeed_and_worker_survives(
        self, tmp_path: Path
    ) -> None:
        # THE regression for the CNCT-85 root cause, and the gap the pre-3.23.0
        # suite never covered. That suite decoded real assets only one call at a
        # time, and its *concurrency* coverage patched the decoder away
        # (_hang / _segfault). So real msgspec was never exercised under
        # concurrent submission — the exact condition that killed the worker in
        # production: msgspec 0.20.0's concurrent-decode segfault on py3.13, when
        # multiple upload validations decoded assets in the same worker process
        # at once.
        #
        # This test closes that gap with NO decoder patch: it fans out many
        # validations of real pyatlan_v9 batches concurrently. The isolation
        # here is purely fault containment — the pool is multi-worker, so these
        # decodes genuinely run in PARALLEL across child processes (no artificial
        # serialisation). That is safe because separate processes don't share the
        # in-process decoder state the 0.20.0 bug needs, and once msgspec 0.21.1
        # lands even same-process concurrent decode is safe. Every scan completes
        # and the parent (the "worker") is still alive afterwards.
        #
        # NOTE: on other interpreters/versions this always passes; as a
        # regression it is most meaningful when CI runs it on the SAME Python as
        # the container image (3.13). That is why the e2e job pins the
        # interpreter to the image's version, not the runner default.
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


class TestValidationAlongsideTransfer:
    """FND-1339: the scan no longer gates the transfer's budget.

    ``App.upload`` starts the scan as a task and moves bytes while it runs;
    retry attempts skip it; its bound is capped at half the activity's own so a
    600s scan can never eat a 600s activity before the first PUT.
    """

    def test_retry_attempt_skips_the_scan(self, monkeypatch) -> None:
        info = MagicMock(attempt=2, start_to_close_timeout=None)
        monkeypatch.setattr(base_module.activity, "info", lambda: info)

        assert base_module._start_transformed_asset_validation("/tmp/x", APP) is None
        assert base_module._current_activity_attempt() == 2

    async def test_first_attempt_starts_a_task_with_the_capped_budget(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from datetime import timedelta

        info = MagicMock(attempt=1, start_to_close_timeout=timedelta(seconds=100))
        monkeypatch.setattr(base_module.activity, "info", lambda: info)
        seen: dict[str, float | None] = {}

        async def fake_warn(local_path, app_name, *, timeout=None):
            seen["timeout"] = timeout

        monkeypatch.setattr(
            base_module, "_warn_on_invalid_transformed_assets", fake_warn
        )

        task = base_module._start_transformed_asset_validation(str(tmp_path), APP)

        assert task is not None
        await task
        assert seen["timeout"] == 50.0

    def test_budget_outside_an_activity_is_the_configured_value(
        self, monkeypatch
    ) -> None:
        from application_sdk.constants import VALIDATE_ASSETS_TIMEOUT_SECONDS

        def not_in_activity():
            raise RuntimeError("Not in activity context")

        monkeypatch.setattr(base_module.activity, "info", not_in_activity)

        assert (
            base_module._transformed_asset_validation_budget()
            == VALIDATE_ASSETS_TIMEOUT_SECONDS
        )
        assert base_module._current_activity_attempt() == 1

    def test_budget_is_the_configured_value_under_a_generous_activity_bound(
        self, monkeypatch
    ) -> None:
        from datetime import timedelta

        from application_sdk.constants import VALIDATE_ASSETS_TIMEOUT_SECONDS

        info = MagicMock(attempt=1, start_to_close_timeout=timedelta(seconds=10_000))
        monkeypatch.setattr(base_module.activity, "info", lambda: info)

        assert (
            base_module._transformed_asset_validation_budget()
            == VALIDATE_ASSETS_TIMEOUT_SECONDS
        )

    async def test_explicit_timeout_reaches_the_isolated_scan(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        out = tmp_path / "transformed" / "table"
        out.mkdir(parents=True)
        (out / "entities.json").write_bytes(b"{}\n")
        runner = MagicMock()

        async def fake_run_best_effort(*args, **kwargs):
            runner(*args, **kwargs)
            return None

        monkeypatch.setattr(base_module, "run_best_effort", fake_run_best_effort)

        await _warn_on_invalid_transformed_assets(str(tmp_path), APP, timeout=7.5)

        assert runner.call_args.kwargs["timeout"] == 7.5

    async def test_default_timeout_is_the_configured_value(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        from application_sdk.constants import VALIDATE_ASSETS_TIMEOUT_SECONDS

        out = tmp_path / "transformed" / "table"
        out.mkdir(parents=True)
        (out / "entities.json").write_bytes(b"{}\n")
        runner = MagicMock()

        async def fake_run_best_effort(*args, **kwargs):
            runner(*args, **kwargs)
            return None

        monkeypatch.setattr(base_module, "run_best_effort", fake_run_best_effort)

        await _warn_on_invalid_transformed_assets(str(tmp_path), APP)

        assert runner.call_args.kwargs["timeout"] == VALIDATE_ASSETS_TIMEOUT_SECONDS
