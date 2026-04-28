"""Unit tests for application_sdk.observability.observability.

CRITICAL CONSTRAINTS:
- No real threads, no real asyncio loops in their own threads, no real HTTP.
- Each test < 0.5s, deterministic.
- All side-effects (file I/O, object stores, sockets, duckdb) heavily mocked.

BLDX-1129 anchor: tests assert that lazy/inline imports referenced inside
functions still resolve at the module/symbol level, even when we don't drive
the call path.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from datetime import datetime, timedelta
from typing import Any, Dict
from unittest import mock

import pytest

from application_sdk.observability import observability as obs_module
from application_sdk.observability.observability import (
    LOCAL_OBS_SUBDIR_MAP,
    OBSERVABILITY_S3_PREFIX_MAP,
    AtlanObservability,
    DuckDBUI,
)

# ---------------------------------------------------------------------------
# Concrete subclass for testing the abstract base class
# ---------------------------------------------------------------------------


class _Concrete(AtlanObservability[Dict[str, Any]]):
    """Minimal concrete subclass — exposes only what's needed for tests."""

    def process_record(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return dict(record)

    def export_record(self, record: Dict[str, Any]) -> None:
        pass


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset class-level state between tests to avoid pollution."""
    AtlanObservability._reset_for_testing()
    yield
    AtlanObservability._reset_for_testing()


@pytest.fixture
def make_instance(tmp_path):
    """Return a factory that builds a concrete observability instance."""

    def _factory(
        *,
        batch_size: int = 100,
        flush_interval: int = 60,
        retention_days: int = 7,
        cleanup_enabled: bool = False,
        file_name: str = "log.parquet",
    ) -> _Concrete:
        return _Concrete(
            batch_size=batch_size,
            flush_interval=flush_interval,
            retention_days=retention_days,
            cleanup_enabled=cleanup_enabled,
            data_dir=str(tmp_path),
            file_name=file_name,
        )

    return _factory


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------


class TestModuleConstants:
    def test_local_subdir_map_has_three_signals(self):
        assert set(LOCAL_OBS_SUBDIR_MAP.keys()) == {"logs", "metrics", "traces"}
        for v in LOCAL_OBS_SUBDIR_MAP.values():
            assert "/" in v  # mode/signal

    def test_s3_prefix_map_has_three_signals(self):
        assert set(OBSERVABILITY_S3_PREFIX_MAP.keys()) == {
            "logs",
            "metrics",
            "traces",
        }
        for v in OBSERVABILITY_S3_PREFIX_MAP.values():
            assert v.startswith("artifacts/apps/observability/")


# ---------------------------------------------------------------------------
# BLDX-1129 anchors — inline imports
# ---------------------------------------------------------------------------


class TestInlineImportAnchors:
    """Ensure symbols imported lazily inside functions still resolve.

    These tests catch the BLDX-1129 bug class (missing inline imports) without
    driving the full call path.
    """

    def test_temporalio_workflow_unsafe_symbol_path(self):
        """observability._flush_records does:
        from temporalio.workflow import unsafe as _wf_unsafe
        """
        try:
            mod = importlib.import_module("temporalio.workflow")
        except ImportError:
            pytest.skip("temporalio not installed in this environment")
        assert hasattr(mod, "unsafe"), "temporalio.workflow.unsafe must exist"
        # The function used is `in_sandbox`.
        assert hasattr(mod.unsafe, "in_sandbox")

    def test_get_infrastructure_symbol_path(self):
        """observability._check_and_cleanup does:
        from application_sdk.infrastructure.context import get_infrastructure
        """
        mod = importlib.import_module("application_sdk.infrastructure.context")
        assert hasattr(
            mod, "get_infrastructure"
        ), "BLDX-1129: get_infrastructure must be importable"

    def test_shutil_module_available(self):
        """observability._cleanup_old_records does `import shutil`."""
        mod = importlib.import_module("shutil")
        assert hasattr(mod, "rmtree")

    def test_socket_module_available(self):
        """DuckDBUI._is_duckdb_ui_running does `import socket`."""
        mod = importlib.import_module("socket")
        assert hasattr(mod, "AF_INET")

    def test_duckdb_module_available(self):
        """DuckDBUI.start_ui does `import duckdb` (optional dep)."""
        try:
            mod = importlib.import_module("duckdb")
        except ImportError:
            pytest.skip("duckdb not installed in this environment")
        assert hasattr(mod, "connect")


# ---------------------------------------------------------------------------
# AtlanObservability.__init__ and registration
# ---------------------------------------------------------------------------


class TestInit:
    def test_init_registers_instance(self, make_instance):
        inst = make_instance()
        assert inst in AtlanObservability._instances

    def test_init_creates_data_dir(self, tmp_path, make_instance):
        target = tmp_path / "subdir"
        inst = _Concrete(
            batch_size=10,
            flush_interval=5,
            retention_days=1,
            cleanup_enabled=False,
            data_dir=str(target),
            file_name="log.parquet",
        )
        assert os.path.isdir(target)
        # And parquet path is computed.
        assert inst.parquet_path.endswith("data.parquet")

    def test_buffer_starts_empty(self, make_instance):
        inst = make_instance()
        assert inst._buffer == []

    def test_reset_for_testing_clears_state(self, make_instance):
        make_instance()
        assert len(AtlanObservability._instances) == 1
        AtlanObservability._reset_for_testing()
        assert AtlanObservability._instances == []
        assert AtlanObservability._deployment_store is None
        assert AtlanObservability._upstream_store is None


# ---------------------------------------------------------------------------
# _get_signal_type / _get_partition_path
# ---------------------------------------------------------------------------


class TestSignalAndPartition:
    def test_get_signal_type_logs(self, make_instance):
        inst = make_instance(file_name="log.parquet")
        # file_name comparison uses LOG_FILE_NAME, METRICS_FILE_NAME, TRACES_FILE_NAME.
        from application_sdk import constants

        inst.file_name = constants.LOG_FILE_NAME
        assert inst._get_signal_type() == "logs"

    def test_get_signal_type_metrics(self, make_instance):
        inst = make_instance()
        from application_sdk import constants

        inst.file_name = constants.METRICS_FILE_NAME
        assert inst._get_signal_type() == "metrics"

    def test_get_signal_type_traces(self, make_instance):
        inst = make_instance()
        from application_sdk import constants

        inst.file_name = constants.TRACES_FILE_NAME
        assert inst._get_signal_type() == "traces"

    def test_get_signal_type_other(self, make_instance):
        inst = make_instance(file_name="random.parquet")
        inst.file_name = "random.parquet"
        assert inst._get_signal_type() == "other"

    def test_partition_path_uses_year_month_day_hour(self, make_instance):
        inst = make_instance()
        ts = datetime(2024, 7, 4, 13, 30, 0)
        path = inst._get_partition_path(ts)
        assert "year=2024" in path
        assert "month=07" in path
        assert "day=04" in path
        assert "hour=13" in path


# ---------------------------------------------------------------------------
# Classmethod stores
# ---------------------------------------------------------------------------


class TestStoreFactories:
    def test_get_deployment_store_lazy_init(self, monkeypatch):
        sentinel = mock.MagicMock(name="deployment_store")
        monkeypatch.setattr(obs_module, "create_store_from_binding", lambda _: sentinel)
        AtlanObservability._reset_for_testing()
        s1 = AtlanObservability._get_deployment_store()
        s2 = AtlanObservability._get_deployment_store()
        assert s1 is sentinel
        # Cached.
        assert s2 is s1

    def test_get_upstream_store_lazy_init(self, monkeypatch):
        sentinel = mock.MagicMock(name="upstream_store")
        monkeypatch.setattr(obs_module, "create_store_from_binding", lambda _: sentinel)
        AtlanObservability._reset_for_testing()
        s1 = AtlanObservability._get_upstream_store()
        assert s1 is sentinel


# ---------------------------------------------------------------------------
# _flush_buffer
# ---------------------------------------------------------------------------


class TestFlushBuffer:
    @pytest.mark.timeout(2)
    async def test_flush_buffer_with_records_calls_flush_records(self, make_instance):
        inst = make_instance()
        inst._buffer = [{"a": 1}, {"a": 2}]
        with mock.patch.object(
            inst, "_flush_records", new_callable=mock.AsyncMock
        ) as mflush:
            await inst._flush_buffer(force=True)
        assert inst._buffer == []
        mflush.assert_awaited_once()
        # Buffer copy was passed.
        copy_arg = mflush.call_args.args[0]
        assert copy_arg == [{"a": 1}, {"a": 2}]

    @pytest.mark.timeout(2)
    async def test_flush_buffer_empty_does_not_call(self, make_instance):
        inst = make_instance()
        inst._buffer = []
        with mock.patch.object(
            inst, "_flush_records", new_callable=mock.AsyncMock
        ) as mflush:
            await inst._flush_buffer(force=True)
        mflush.assert_not_called()


# ---------------------------------------------------------------------------
# _periodic_flush
# ---------------------------------------------------------------------------


class TestPeriodicFlush:
    @pytest.mark.timeout(2)
    async def test_periodic_flush_cancellation_runs_final_flush(self, make_instance):
        inst = make_instance()
        flush_calls = []

        async def _fake_flush(force=False):  # noqa: ARG001
            flush_calls.append(force)

        # Make asyncio.sleep raise CancelledError on first call so we exit
        # the while loop quickly and trigger the cancellation branch.
        async def _fake_sleep(_seconds):
            raise asyncio.CancelledError()

        with (
            mock.patch.object(inst, "_flush_buffer", side_effect=_fake_flush),
            mock.patch.object(obs_module.asyncio, "sleep", _fake_sleep),
        ):
            await inst._periodic_flush()

        # Initial flush + final cancel-handler flush.
        assert len(flush_calls) >= 2

    @pytest.mark.timeout(2)
    async def test_periodic_flush_other_exception_logged(self, make_instance):
        """A non-CancelledError must be caught and logged, not re-raised."""
        inst = make_instance()

        async def _bad_flush(force=False):  # noqa: ARG001
            raise RuntimeError("boom")

        with mock.patch.object(inst, "_flush_buffer", side_effect=_bad_flush):
            # Must not raise.
            await inst._periodic_flush()


# ---------------------------------------------------------------------------
# _flush_records — patch module-level ENABLE_OBSERVABILITY_STORE_SINK
# ---------------------------------------------------------------------------


class TestFlushRecords:
    @pytest.mark.timeout(2)
    async def test_skipped_when_sink_disabled(self, make_instance, monkeypatch):
        inst = make_instance()
        monkeypatch.setattr(obs_module, "ENABLE_OBSERVABILITY_STORE_SINK", False)
        # Should return without touching anything — no upload calls.
        with mock.patch.object(obs_module, "upload_file") as up:
            await inst._flush_records([{"timestamp": 1.0, "data": "x"}])
        up.assert_not_called()

    @pytest.mark.timeout(2)
    async def test_skipped_when_no_records(self, make_instance, monkeypatch):
        inst = make_instance()
        monkeypatch.setattr(obs_module, "ENABLE_OBSERVABILITY_STORE_SINK", True)
        with mock.patch.object(obs_module, "upload_file") as up:
            await inst._flush_records([])
        up.assert_not_called()

    @pytest.mark.timeout(2)
    async def test_happy_path_writes_and_uploads(
        self, make_instance, monkeypatch, tmp_path
    ):
        inst = make_instance(cleanup_enabled=False)
        monkeypatch.setattr(obs_module, "ENABLE_OBSERVABILITY_STORE_SINK", True)
        # Prevent any real Atlan upload branch.
        monkeypatch.setattr(obs_module, "ENABLE_ATLAN_UPLOAD", False)

        # Stub the deployment store.
        fake_store = mock.MagicMock(name="deployment_store")
        monkeypatch.setattr(
            AtlanObservability, "_deployment_store", fake_store, raising=False
        )

        # Use real datetime; pass a timestamp.
        records = [{"timestamp": 1_700_000_000.0, "msg": "hi"}]

        upload_mock = mock.AsyncMock()
        monkeypatch.setattr(obs_module, "upload_file", upload_mock)

        await inst._flush_records(records)

        upload_mock.assert_awaited()
        # Check first call signature: (remote_key, local_path, store=...).
        first_call = upload_mock.call_args_list[0]
        remote_key = first_call.args[0]
        assert "year=" in remote_key and "month=" in remote_key
        assert remote_key.endswith(".json.gz")

    @pytest.mark.timeout(2)
    async def test_atlan_upload_branch_taken(self, make_instance, monkeypatch):
        inst = make_instance()
        monkeypatch.setattr(obs_module, "ENABLE_OBSERVABILITY_STORE_SINK", True)
        monkeypatch.setattr(obs_module, "ENABLE_ATLAN_UPLOAD", True)
        monkeypatch.setattr(
            AtlanObservability, "_deployment_store", mock.MagicMock(), raising=False
        )
        monkeypatch.setattr(
            AtlanObservability, "_upstream_store", mock.MagicMock(), raising=False
        )

        upload_mock = mock.AsyncMock()
        monkeypatch.setattr(obs_module, "upload_file", upload_mock)

        records = [{"timestamp": 1_700_000_000.0, "msg": "hi"}]
        await inst._flush_records(records)

        # Should be called twice: once for deployment, once for upstream.
        assert upload_mock.await_count == 2

    @pytest.mark.timeout(2)
    async def test_deployment_upload_failure_is_non_fatal(
        self, make_instance, monkeypatch
    ):
        inst = make_instance()
        monkeypatch.setattr(obs_module, "ENABLE_OBSERVABILITY_STORE_SINK", True)
        monkeypatch.setattr(obs_module, "ENABLE_ATLAN_UPLOAD", False)
        monkeypatch.setattr(
            AtlanObservability, "_deployment_store", mock.MagicMock(), raising=False
        )

        # First upload fails — must be swallowed.
        upload_mock = mock.AsyncMock(side_effect=RuntimeError("net down"))
        monkeypatch.setattr(obs_module, "upload_file", upload_mock)

        records = [{"timestamp": 1_700_000_000.0, "msg": "hi"}]
        # Must not raise.
        await inst._flush_records(records)


# ---------------------------------------------------------------------------
# _check_and_cleanup
# ---------------------------------------------------------------------------


class TestCheckAndCleanup:
    @pytest.mark.timeout(2)
    async def test_cleanup_when_no_last_cleanup(self, make_instance, monkeypatch):
        inst = make_instance()

        fake_state = mock.AsyncMock()
        fake_state.load = mock.AsyncMock(return_value=None)
        fake_state.save = mock.AsyncMock()
        fake_infra = mock.MagicMock()
        fake_infra.state_store = fake_state

        # Patch the inline import target.
        fake_ctx = mock.MagicMock()
        fake_ctx.get_infrastructure = mock.MagicMock(return_value=fake_infra)
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.infrastructure.context",
            fake_ctx,
        )

        with mock.patch.object(
            inst, "_cleanup_old_records", new_callable=mock.AsyncMock
        ) as cleanup:
            await inst._check_and_cleanup()
        cleanup.assert_awaited_once()
        fake_state.save.assert_awaited_once()

    @pytest.mark.timeout(2)
    async def test_cleanup_skipped_when_recent(self, make_instance, monkeypatch):
        inst = make_instance()
        recent = (datetime.now() - timedelta(hours=1)).isoformat()

        fake_state = mock.AsyncMock()
        fake_state.load = mock.AsyncMock(return_value={"value": recent})
        fake_state.save = mock.AsyncMock()
        fake_infra = mock.MagicMock()
        fake_infra.state_store = fake_state

        fake_ctx = mock.MagicMock()
        fake_ctx.get_infrastructure = mock.MagicMock(return_value=fake_infra)
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.infrastructure.context",
            fake_ctx,
        )

        with mock.patch.object(
            inst, "_cleanup_old_records", new_callable=mock.AsyncMock
        ) as cleanup:
            await inst._check_and_cleanup()
        cleanup.assert_not_called()
        fake_state.save.assert_not_called()

    @pytest.mark.timeout(2)
    async def test_cleanup_when_stale(self, make_instance, monkeypatch):
        inst = make_instance()
        stale = (datetime.now() - timedelta(days=2)).isoformat()

        fake_state = mock.AsyncMock()
        fake_state.load = mock.AsyncMock(return_value={"value": stale})
        fake_state.save = mock.AsyncMock()
        fake_infra = mock.MagicMock()
        fake_infra.state_store = fake_state

        fake_ctx = mock.MagicMock()
        fake_ctx.get_infrastructure = mock.MagicMock(return_value=fake_infra)
        monkeypatch.setitem(
            __import__("sys").modules,
            "application_sdk.infrastructure.context",
            fake_ctx,
        )

        with mock.patch.object(
            inst, "_cleanup_old_records", new_callable=mock.AsyncMock
        ) as cleanup:
            await inst._check_and_cleanup()
        cleanup.assert_awaited_once()


# ---------------------------------------------------------------------------
# _cleanup_old_records
# ---------------------------------------------------------------------------


class TestCleanupOldRecords:
    @pytest.mark.timeout(2)
    async def test_no_data_dir_returns_quickly(self, make_instance):
        inst = make_instance()
        # data_dir exists but the signal-specific subdir does not.
        # Should return without raising.
        await inst._cleanup_old_records()

    @pytest.mark.timeout(2)
    async def test_old_year_partition_deleted(
        self, make_instance, monkeypatch, tmp_path
    ):
        inst = make_instance(retention_days=7, cleanup_enabled=True)
        # Build a directory layout with an OLD year that should be removed.
        signal_subdir = LOCAL_OBS_SUBDIR_MAP.get(
            inst._get_signal_type(), "non-sdr/other"
        )
        old_dir = tmp_path / signal_subdir / "year=1999"
        old_dir.mkdir(parents=True)

        rmtree_mock = mock.MagicMock()
        monkeypatch.setattr(obs_module, "create_store_from_binding", mock.MagicMock())
        # Shutil is imported lazily inside _cleanup_old_records.
        with mock.patch("shutil.rmtree", rmtree_mock):
            with mock.patch.object(obs_module, "delete", new_callable=mock.AsyncMock):
                await inst._cleanup_old_records()
        rmtree_mock.assert_called()

    @pytest.mark.timeout(2)
    async def test_exception_swallowed(self, make_instance):
        inst = make_instance()
        # The signal subdir already exists (created by _update_parquet_path).
        # Force listdir to raise so the except-swallow branch runs.
        with (
            mock.patch.object(
                obs_module.os, "listdir", side_effect=OSError("permission denied")
            ),
            mock.patch.object(obs_module.os.path, "exists", return_value=True),
        ):
            # Must not raise.
            await inst._cleanup_old_records()


# ---------------------------------------------------------------------------
# add_record / flush_all
# ---------------------------------------------------------------------------


class TestAddRecord:
    def test_add_record_below_threshold_does_not_create_task(
        self, make_instance, monkeypatch
    ):
        inst = make_instance(batch_size=100, flush_interval=600)
        # Bump last_flush_time forward so flush_interval check is not met.
        inst._last_flush_time = __import__("time").time()

        # Patch asyncio.create_task so we can detect calls without needing a loop.
        ct = mock.MagicMock()
        monkeypatch.setattr(obs_module.asyncio, "create_task", ct)
        with mock.patch.object(inst, "export_record") as ex:
            inst.add_record({"timestamp": 1.0})
        ct.assert_not_called()
        ex.assert_called_once()
        assert len(inst._buffer) == 1

    def test_add_record_at_batch_size_triggers_flush_task(
        self, make_instance, monkeypatch
    ):
        inst = make_instance(batch_size=1, flush_interval=600)

        captured = {"coro": None}

        def _capture(coro):
            captured["coro"] = coro
            return mock.MagicMock()

        monkeypatch.setattr(obs_module.asyncio, "create_task", _capture)
        with mock.patch.object(inst, "export_record"):
            inst.add_record({"timestamp": 1.0})
        assert captured["coro"] is not None
        # Buffer should have been cleared because it hit batch size.
        assert inst._buffer == []
        # Close the captured coro to avoid "never awaited" warnings.
        try:
            captured["coro"].close()
        except Exception:
            pass

    def test_add_record_swallows_process_record_exception(
        self, make_instance, monkeypatch
    ):
        inst = make_instance()
        with mock.patch.object(
            inst, "process_record", side_effect=RuntimeError("boom")
        ):
            # Must not raise.
            inst.add_record({"timestamp": 1.0})


class TestFlushAll:
    @pytest.mark.timeout(2)
    async def test_flush_all_invokes_each_instance(self, make_instance):
        i1 = make_instance()
        i2 = make_instance()
        with (
            mock.patch.object(i1, "_flush_buffer", new_callable=mock.AsyncMock) as f1,
            mock.patch.object(i2, "_flush_buffer", new_callable=mock.AsyncMock) as f2,
        ):
            await AtlanObservability.flush_all()
        f1.assert_awaited_once_with(force=True)
        f2.assert_awaited_once_with(force=True)

    @pytest.mark.timeout(2)
    async def test_flush_all_continues_on_exception(self, make_instance):
        i1 = make_instance()
        i2 = make_instance()
        with mock.patch.object(
            i1,
            "_flush_buffer",
            new_callable=mock.AsyncMock,
            side_effect=RuntimeError("nope"),
        ):
            with mock.patch.object(
                i2, "_flush_buffer", new_callable=mock.AsyncMock
            ) as f2:
                # Must not raise — i1 fails but i2 should still be flushed.
                await AtlanObservability.flush_all()
            f2.assert_awaited_once()


# ---------------------------------------------------------------------------
# DuckDBUI
# ---------------------------------------------------------------------------


class TestDuckDBUI:
    def test_init_sets_paths(self, monkeypatch):
        monkeypatch.setattr(obs_module, "get_observability_dir", lambda: "/tmp/obs")
        ui = DuckDBUI()
        assert ui.observability_dir == "/tmp/obs"
        assert ui.db_path == "/tmp/obs/observability.db"
        assert ui._duckdb_ui_con is None

    def test_is_duckdb_ui_running_returns_true_when_port_open(self, monkeypatch):
        monkeypatch.setattr(obs_module, "get_observability_dir", lambda: "/tmp/obs")
        ui = DuckDBUI()

        # Patch socket.socket so connect_ex returns 0 (port open).
        fake_sock = mock.MagicMock()
        fake_sock.__enter__ = mock.MagicMock(return_value=fake_sock)
        fake_sock.__exit__ = mock.MagicMock(return_value=False)
        fake_sock.connect_ex = mock.MagicMock(return_value=0)

        with mock.patch("socket.socket", return_value=fake_sock):
            assert ui._is_duckdb_ui_running() is True

    def test_is_duckdb_ui_running_returns_false_when_port_closed(self, monkeypatch):
        monkeypatch.setattr(obs_module, "get_observability_dir", lambda: "/tmp/obs")
        ui = DuckDBUI()

        fake_sock = mock.MagicMock()
        fake_sock.__enter__ = mock.MagicMock(return_value=fake_sock)
        fake_sock.__exit__ = mock.MagicMock(return_value=False)
        fake_sock.connect_ex = mock.MagicMock(return_value=111)

        with mock.patch("socket.socket", return_value=fake_sock):
            assert ui._is_duckdb_ui_running() is False

    def test_start_ui_skipped_when_already_running(self, monkeypatch):
        monkeypatch.setattr(obs_module, "get_observability_dir", lambda: "/tmp/obs")
        ui = DuckDBUI()
        with mock.patch.object(ui, "_is_duckdb_ui_running", return_value=True):
            # Should not import duckdb or open any connection.
            with mock.patch("duckdb.connect") as dc:
                ui.start_ui()
            dc.assert_not_called()
        assert ui._duckdb_ui_con is None

    def test_start_ui_initializes_duckdb_when_not_running(self, monkeypatch, tmp_path):
        monkeypatch.setattr(obs_module, "get_observability_dir", lambda: str(tmp_path))
        ui = DuckDBUI()

        fake_con = mock.MagicMock()
        with (
            mock.patch.object(ui, "_is_duckdb_ui_running", return_value=False),
            mock.patch("duckdb.connect", return_value=fake_con) as connect,
        ):
            ui.start_ui()
        connect.assert_called_once()
        # Final start_ui call.
        fake_con.execute.assert_any_call("CALL start_ui();")
        assert ui._duckdb_ui_con is fake_con
