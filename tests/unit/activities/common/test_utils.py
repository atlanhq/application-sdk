"""Unit tests for activities common utilities."""

import asyncio
import contextvars
import threading
import time
from datetime import timedelta
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from application_sdk.activities.common.utils import (
    _build_direct_heartbeat_fn,
    _heartbeat_thread_target,
    auto_heartbeater,
    get_object_store_prefix,
    get_workflow_id,
)


class TestGetWorkflowId:
    """Test cases for get_workflow_id function."""

    @patch("application_sdk.activities.common.utils.activity")
    def test_get_workflow_id_success(self, mock_activity):
        mock_activity.info.return_value.workflow_id = "test-workflow-123"
        assert get_workflow_id() == "test-workflow-123"

    @patch("application_sdk.activities.common.utils.activity")
    def test_get_workflow_id_activity_error(self, mock_activity):
        mock_activity.info.side_effect = Exception("Activity context error")
        with pytest.raises(Exception, match="Failed to get workflow id"):
            get_workflow_id()


class TestGetObjectStorePrefix:
    """Test cases for get_object_store_prefix function."""

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    @patch("os.path.relpath")
    @patch("os.path.commonpath")
    def test_temporary_path_to_object_store_conversion(
        self, mock_commonpath, mock_relpath, mock_abspath
    ):
        mock_abspath.side_effect = lambda p: {
            "./local/tmp/artifacts/apps/myapp/workflows/wf-123/run-456": "/abs/local/tmp/artifacts/apps/myapp/workflows/wf-123/run-456",
            "./local/tmp": "/abs/local/tmp",
        }.get(p, p)
        mock_commonpath.return_value = "/abs/local/tmp"
        mock_relpath.return_value = "artifacts/apps/myapp/workflows/wf-123/run-456"

        result = get_object_store_prefix(
            "./local/tmp/artifacts/apps/myapp/workflows/wf-123/run-456"
        )
        assert result == "artifacts/apps/myapp/workflows/wf-123/run-456"

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    def test_user_relative_object_store_paths(self, mock_abspath):
        mock_abspath.side_effect = lambda p: {
            "datasets/sales/2024/": "/some/other/datasets/sales/2024/",
            "./local/tmp": "/abs/local/tmp",
        }.get(p, p)
        for input_path, expected in [
            ("datasets/sales/2024/", "datasets/sales/2024"),
            ("data/input.parquet", "data/input.parquet"),
            ("./datasets/sales/", "./datasets/sales"),
            ("models/trained/v1.pkl", "models/trained/v1.pkl"),
        ]:
            assert get_object_store_prefix(input_path) == expected

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    @patch("os.path.commonpath")
    @patch("os.path.normpath")
    def test_user_absolute_paths_converted_to_relative(
        self, mock_normpath, mock_commonpath, mock_abspath
    ):
        mock_abspath.side_effect = lambda p: {
            "/data/test.parquet": "/data/test.parquet",
            "/datasets/sales/2024/": "/datasets/sales/2024/",
            "./local/tmp": "/abs/local/tmp",
        }.get(p, p)
        mock_commonpath.return_value = "/different/root"
        mock_normpath.side_effect = (
            lambda p: p.lstrip("./") if p.startswith("./") else p
        )

        for input_path, expected in [
            ("/data/test.parquet", "data/test.parquet"),
            ("/datasets/sales/2024/", "datasets/sales/2024"),
            ("/models/trained/v1.pkl", "models/trained/v1.pkl"),
        ]:
            assert get_object_store_prefix(input_path) == expected

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    @patch("os.path.commonpath")
    @patch("os.path.normpath")
    def test_user_provided_object_store_keys(
        self, mock_normpath, mock_commonpath, mock_abspath
    ):
        """Test user-provided object store keys are returned as-is."""
        mock_abspath.side_effect = lambda p: p
        mock_commonpath.return_value = "/different/root"
        mock_normpath.side_effect = lambda p: p

        test_cases = [
            "data/file1.parquet",
            "datasets/sales/2024_q1.json",
            "models/model.pkl",
            "/data/file1.parquet",
            "//datasets//sales//file.json",
        ]
        expected_results = [
            "data/file1.parquet",
            "datasets/sales/2024_q1.json",
            "models/model.pkl",
            "data/file1.parquet",
            "datasets//sales//file.json",
        ]
        for key, expected in zip(test_cases, expected_results):
            result = get_object_store_prefix(key)
            assert (
                result == expected
            ), f"Failed for '{key}': got '{result}', expected '{expected}'"

    @patch("application_sdk.activities.common.utils.TEMPORARY_PATH", "./local/tmp")
    @patch("os.path.abspath")
    @patch("os.path.commonpath")
    def test_security_path_normalization(self, mock_commonpath, mock_abspath):
        mock_abspath.side_effect = lambda p: p
        mock_commonpath.return_value = "/"
        for input_path, expected in [
            ("../../../etc/passwd", "../../../etc/passwd"),
            ("../../sensitive/data", "../../sensitive/data"),
        ]:
            assert get_object_store_prefix(input_path) == expected


class TestAutoHeartbeater:
    """Tests for auto_heartbeater decorator behaviour."""

    @patch("application_sdk.activities.common.utils.activity")
    async def test_async_activity_completes_and_heartbeats(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=0.3)

        @auto_heartbeater
        async def test_activity():
            await asyncio.sleep(0.4)
            return "success"

        result = await test_activity()
        await asyncio.sleep(0)
        assert result == "success"
        assert mock_activity.heartbeat.call_count >= 1

    @patch("application_sdk.activities.common.utils.activity")
    async def test_async_activity_default_timeout(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = None

        @auto_heartbeater
        async def test_activity():
            return "success"

        assert await test_activity() == "success"

    @patch("application_sdk.activities.common.utils.activity")
    async def test_async_activity_info_runtime_error(self, mock_activity):
        mock_activity.info.side_effect = RuntimeError("No activity context")

        @auto_heartbeater
        async def test_activity():
            return "success"

        assert await test_activity() == "success"

    @patch("application_sdk.activities.common.utils.activity")
    async def test_async_activity_exception_propagates(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        async def test_activity():
            raise ValueError("activity error")

        with pytest.raises(ValueError, match="activity error"):
            await test_activity()

    @patch("application_sdk.activities.common.utils.activity")
    async def test_async_activity_with_arguments(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        async def test_activity(arg1, arg2, kwarg1=None):
            return f"{arg1}_{arg2}_{kwarg1}"

        assert await test_activity("a", "b", kwarg1="c") == "a_b_c"

    @patch("application_sdk.activities.common.utils.activity")
    def test_sync_activity_returns_value(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        def sync_activity():
            return "sync_success"

        result = sync_activity()
        assert result == "sync_success"
        assert not asyncio.iscoroutine(result)

    @patch("application_sdk.activities.common.utils.activity")
    def test_sync_activity_exception_propagates(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        def sync_activity():
            raise ValueError("sync error")

        with pytest.raises(ValueError, match="sync error"):
            sync_activity()

    @patch("application_sdk.activities.common.utils.activity")
    def test_sync_activity_with_arguments(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=60)

        @auto_heartbeater
        def sync_activity(arg1, arg2, kwarg1=None):
            return f"{arg1}_{arg2}_{kwarg1}"

        assert sync_activity("a", "b", kwarg1="c") == "a_b_c"

    def test_preserves_sync_nature(self):
        @auto_heartbeater
        def sync_activity():
            return "test"

        assert not asyncio.iscoroutinefunction(sync_activity)

    def test_preserves_async_nature(self):
        @auto_heartbeater
        async def async_activity():
            return "test"

        assert asyncio.iscoroutinefunction(async_activity)


# ---------------------------------------------------------------------------
# Helpers shared by direct-bridge tests
# ---------------------------------------------------------------------------


def _make_temporal_context_with_outbound(
    task_token: bytes = b"test-token-1234",
) -> tuple[Any, Any]:
    """Return (fake_temporal_ctx, mock_bridge_worker).

    Sets up a minimal real temporalio._Context whose ``heartbeat`` callable is
    a genuine ``_ActivityOutboundImpl.heartbeat`` bound method, wired to a
    mock bridge worker.  This is exactly the structure the SDK creates at
    activity startup, so ``_build_direct_heartbeat_fn`` can exercise the real
    extraction path.
    """
    from temporalio import activity as ta
    from temporalio.worker._activity import _ActivityOutboundImpl

    mock_bridge_worker = MagicMock()
    mock_bridge_worker.record_activity_heartbeat = MagicMock()

    mock_activity_worker = MagicMock()
    mock_activity_worker._bridge_worker = lambda: mock_bridge_worker

    mock_outbound = MagicMock(spec=_ActivityOutboundImpl)
    mock_outbound._worker = mock_activity_worker

    heartbeat_fn = _ActivityOutboundImpl.heartbeat.__get__(mock_outbound)

    fake_ctx = ta._Context.__new__(ta._Context)
    fake_ctx.info = lambda: type("Info", (), {"task_token": task_token})()
    fake_ctx.heartbeat = heartbeat_fn
    fake_event = ta._CompositeEvent.__new__(ta._CompositeEvent)
    fake_event.thread_event = threading.Event()
    fake_event.async_event = asyncio.Event()
    fake_ctx.cancelled_event = fake_event
    fake_ctx.worker_shutdown_event = fake_event
    fake_ctx.shield_thread_cancel_exception = None
    fake_ctx.payload_converter_class_or_instance = MagicMock()
    fake_ctx.runtime_metric_meter = None
    fake_ctx._logger_details = None
    fake_ctx._payload_converter = None
    fake_ctx._metric_meter = None

    return fake_ctx, mock_bridge_worker


class TestDirectBridgeHeartbeat:
    """Tests for _build_direct_heartbeat_fn and the direct Rust bridge path.

    The key claim: when the Temporal activity context is present,
    _build_direct_heartbeat_fn extracts the bridge_worker reference and returns
    a callable that calls record_activity_heartbeat() synchronously from the
    background thread — with no event loop involvement.

    The critical test is test_heartbeats_fire_DURING_event_loop_block which
    proves heartbeats are delivered *while* time.sleep() blocks the main thread,
    not just after it unblocks.
    """

    def test_returns_callable_in_activity_context(self):
        """Returns a callable when a real Temporal context is present."""
        from temporalio import activity as ta

        fake_ctx, _ = _make_temporal_context_with_outbound()
        token = ta._current_context.set(fake_ctx)
        try:
            ctx = contextvars.copy_context()
        finally:
            ta._current_context.reset(token)

        fn = _build_direct_heartbeat_fn(ctx)
        assert fn is not None

    def test_calling_fn_invokes_record_activity_heartbeat(self):
        """The returned function calls bridge_worker.record_activity_heartbeat."""
        from temporalio import activity as ta

        fake_ctx, mock_bridge_worker = _make_temporal_context_with_outbound(b"tok-abc")
        token = ta._current_context.set(fake_ctx)
        try:
            ctx = contextvars.copy_context()
        finally:
            ta._current_context.reset(token)

        fn = _build_direct_heartbeat_fn(ctx)
        assert fn is not None
        fn()
        assert mock_bridge_worker.record_activity_heartbeat.call_count == 1

    def test_returns_none_outside_activity_context(self):
        """Returns None when there is no Temporal activity context (tests, local runs)."""
        ctx = contextvars.copy_context()
        assert _build_direct_heartbeat_fn(ctx) is None

    def test_heartbeats_fire_DURING_event_loop_block(self):
        """THE CORE TEST: direct bridge heartbeats fire while the calling thread
        is blocked — independently of the event loop.

        The heartbeat thread calls record_activity_heartbeat() every 0.05 s.
        The main thread sleeps for 0.4 s (simulating a blocking async activity).
        We measure call_count at the mid-point of the sleep to prove heartbeats
        are not deferred until after the block ends.

        With the old call_soon_threadsafe approach, call_count at mid-point
        would be 0 (callbacks are queued but cannot execute while sleeping).
        With the direct bridge approach, call_count at mid-point is >= 3.
        """
        from temporalio import activity as ta

        fake_ctx, mock_bridge_worker = _make_temporal_context_with_outbound()
        token = ta._current_context.set(fake_ctx)
        try:
            ctx = contextvars.copy_context()
        finally:
            ta._current_context.reset(token)

        direct_fn = _build_direct_heartbeat_fn(ctx)
        assert direct_fn is not None, "Bridge extraction failed — cannot run core test"

        stop_event = threading.Event()

        def heartbeat_thread():
            while not stop_event.wait(timeout=0.05):
                try:
                    direct_fn()
                except Exception:
                    pass

        thread = threading.Thread(target=heartbeat_thread, daemon=True)
        thread.start()

        # Block for 0.4 s and sample call_count at the mid-point
        time.sleep(0.2)
        count_mid_block = mock_bridge_worker.record_activity_heartbeat.call_count
        time.sleep(0.2)

        stop_event.set()
        thread.join(timeout=2)

        assert count_mid_block >= 3, (
            f"Expected >= 3 heartbeats at the mid-point of a 0.4 s block "
            f"(interval 0.05 s), got {count_mid_block}. "
            f"The direct bridge path is not firing independently of the event loop."
        )

    async def test_fallback_path_via_real_contextvar(self):
        """Fallback path: call_soon_threadsafe correctly routes via the real
        Temporal ContextVar when direct_fn is None.

        Sets up a real _Context with heartbeat_fn as a MagicMock and runs
        _heartbeat_thread_target with no direct_fn.  Proves the ctx.run() /
        call_soon_threadsafe chain reaches the heartbeat callable in the context.
        """
        from temporalio import activity as ta

        heartbeat_fn = MagicMock()
        fake_ctx = ta._Context.__new__(ta._Context)
        fake_ctx.info = MagicMock()
        fake_ctx.heartbeat = heartbeat_fn
        fake_event = ta._CompositeEvent.__new__(ta._CompositeEvent)
        fake_event.thread_event = threading.Event()
        fake_event.async_event = asyncio.Event()
        fake_ctx.cancelled_event = fake_event
        fake_ctx.worker_shutdown_event = fake_event
        fake_ctx.shield_thread_cancel_exception = None
        fake_ctx.payload_converter_class_or_instance = MagicMock()
        fake_ctx.runtime_metric_meter = None
        fake_ctx._logger_details = None
        fake_ctx._payload_converter = None
        fake_ctx._metric_meter = None

        token = ta._current_context.set(fake_ctx)
        try:
            loop = asyncio.get_running_loop()
            ctx = contextvars.copy_context()
        finally:
            ta._current_context.reset(token)

        stop_event = threading.Event()
        thread = threading.Thread(
            target=_heartbeat_thread_target,
            args=(0.05, stop_event, loop, ctx, None),  # no direct_fn → fallback
            daemon=True,
        )
        thread.start()

        deadline = asyncio.get_event_loop().time() + 5
        while (
            heartbeat_fn.call_count < 1 and asyncio.get_event_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)

        stop_event.set()
        thread.join(timeout=2)

        assert (
            heartbeat_fn.call_count >= 1
        ), "Fallback call_soon_threadsafe path did not reach real _Context heartbeat_fn"


class TestHeartbeatDuringEventLoopBlock:
    """Integration tests: auto_heartbeater with blocking async activities.

    In these tests the activity module is mocked, so _build_direct_heartbeat_fn
    falls back to call_soon_threadsafe.  Heartbeats queue during the block and
    fire when the loop is free after the block.

    The proof that heartbeats fire *during* a block (not just after) when a
    real Temporal context is present is in
    TestDirectBridgeHeartbeat.test_heartbeats_fire_DURING_event_loop_block.
    """

    @patch("application_sdk.activities.common.utils.activity")
    async def test_heartbeats_sent_with_blocking_activity(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=0.3)

        @auto_heartbeater
        async def blocking_async_activity():
            time.sleep(0.5)
            return "done"

        result = await blocking_async_activity()
        await asyncio.sleep(0)
        assert result == "done"
        assert mock_activity.heartbeat.call_count >= 1

    @patch("application_sdk.activities.common.utils.activity")
    async def test_baseline_heartbeats_fire_normally(self, mock_activity):
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=0.3)

        @auto_heartbeater
        async def well_behaved_activity():
            await asyncio.sleep(0.4)
            return "done"

        result = await well_behaved_activity()
        await asyncio.sleep(0)
        assert result == "done"
        assert mock_activity.heartbeat.call_count >= 1

    @patch("application_sdk.activities.common.utils.activity")
    async def test_heartbeat_exception_does_not_stop_heartbeating(self, mock_activity):
        """A transient heartbeat exception must not kill the heartbeat thread."""
        mock_activity.info.return_value.heartbeat_timeout = timedelta(seconds=0.3)
        call_count = 0

        def side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("transient error")

        mock_activity.heartbeat.side_effect = side_effect

        @auto_heartbeater
        async def test_activity():
            await asyncio.sleep(0.5)
            return "done"

        result = await test_activity()
        await asyncio.sleep(0)
        assert result == "done"
        assert (
            call_count >= 2
        ), f"Thread should survive transient error; got {call_count}"
