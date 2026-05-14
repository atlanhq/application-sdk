"""Unit tests for application_sdk.observability.segment_client.

CRITICAL CONSTRAINTS:
- No real threads, no real asyncio loops in their own threads, no real HTTP.
- Each test < 0.5s, deterministic.
- Mock all side-effects HEAVILY.

Tests anchor BLDX-1129 (inline-import bug class) by importing the constants
module and asserting the symbols used inside the inline import survive.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
from typing import Any
from unittest import mock

import pytest

from application_sdk.observability import segment_client as sc_module
from application_sdk.observability.models import MetricRecord, MetricType
from application_sdk.observability.segment_client import (
    SegmentBatchPayload,
    SegmentClient,
    SegmentTrackEvent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    *,
    name: str = "test_metric",
    value: float = 42.0,
    metric_type: MetricType = MetricType.COUNTER,
    labels: dict[str, Any] | None = None,
    description: str | None = None,
    unit: str | None = None,
    timestamp: float = 1_700_000_000.0,
) -> MetricRecord:
    return MetricRecord(
        timestamp=timestamp,
        name=name,
        value=value,
        type=metric_type,
        labels=labels if labels is not None else {},
        description=description,
        unit=unit,
    )


def _disabled_client() -> SegmentClient:
    """Construct a client with no write key — no thread is started."""
    return SegmentClient(write_key="")


def _enabled_client_no_thread() -> SegmentClient:
    """Construct an enabled client while patching out _start_worker_thread.

    The resulting client has enabled=True but no real worker thread / loop.
    Tests can manually attach mocks for _client / _queue / _loop.
    """
    with mock.patch.object(SegmentClient, "_start_worker_thread", return_value=None):
        client = SegmentClient(write_key="fake-key")
    # Sanity: thread was not started.
    assert client._worker_thread is None
    return client


# ---------------------------------------------------------------------------
# Pydantic models — basic sanity
# ---------------------------------------------------------------------------


class TestSegmentTrackEvent:
    def test_defaults_and_required_fields(self):
        ev = SegmentTrackEvent(
            userId="u",
            event="e",
            timestamp="2024-01-01T00:00:00",
        )
        assert ev.type == "track"
        assert ev.properties == {}

    def test_explicit_fields_round_trip(self):
        ev = SegmentTrackEvent(
            userId="u",
            event="e",
            properties={"a": 1},
            timestamp="2024-01-01T00:00:00",
        )
        dumped = ev.model_dump()
        assert dumped["userId"] == "u"
        assert dumped["properties"] == {"a": 1}
        assert dumped["type"] == "track"


class TestSegmentBatchPayload:
    def test_batch_serialization(self):
        ev = SegmentTrackEvent(userId="u", event="e", timestamp="2024-01-01T00:00:00")
        payload = SegmentBatchPayload(batch=[ev])
        dumped = payload.model_dump()
        assert isinstance(dumped["batch"], list)
        assert dumped["batch"][0]["userId"] == "u"


# ---------------------------------------------------------------------------
# Disabled construction (no write key)
# ---------------------------------------------------------------------------


class TestDisabledClient:
    def test_no_write_key_disables_client(self):
        # Empty write key (passed explicitly) and ensure module-level default
        # is also empty so the override path holds.
        with mock.patch.object(sc_module, "SEGMENT_WRITE_KEY", ""):
            client = SegmentClient(write_key="")
        assert client.enabled is False
        assert client._worker_thread is None
        assert client._loop is None

    def test_send_metric_noop_when_disabled(self):
        with mock.patch.object(sc_module, "SEGMENT_WRITE_KEY", ""):
            client = SegmentClient(write_key="")
        # Should silently no-op — even if metric has send_to_segment=true.
        record = _make_record(labels={"send_to_segment": "true"})
        # Patch threading.Thread to ensure no thread is ever spawned.
        with mock.patch.object(sc_module.threading, "Thread") as mthread:
            client.send_metric(record)
        mthread.assert_not_called()

    def test_close_noop_when_disabled(self):
        with mock.patch.object(sc_module, "SEGMENT_WRITE_KEY", ""):
            client = SegmentClient(write_key="")
        # Should not raise even though there's no loop/thread/client.
        client.close()


# ---------------------------------------------------------------------------
# Enabled construction — but no real thread
# ---------------------------------------------------------------------------


class TestEnabledConstruction:
    def test_construction_starts_worker_thread_once(self):
        """__init__ should invoke _start_worker_thread when enabled."""
        with mock.patch.object(
            SegmentClient, "_start_worker_thread", autospec=True
        ) as start:
            client = SegmentClient(write_key="fake-key")
        assert client.enabled is True
        # Called exactly once with the instance as the only positional arg.
        assert start.call_count == 1

    def test_explicit_overrides_used(self):
        with mock.patch.object(
            SegmentClient, "_start_worker_thread", return_value=None
        ):
            client = SegmentClient(
                write_key="k",
                api_url="https://example.test/v1/batch",
                default_user_id="custom.user",
                batch_size=10,
                batch_timeout_seconds=2.5,
            )
        assert client._write_key == "k"
        assert client._api_url == "https://example.test/v1/batch"
        assert client._default_user_id == "custom.user"
        assert client._batch_size == 10
        assert client._batch_timeout_seconds == 2.5

    def test_thread_target_intercepted_does_not_actually_run(self):
        """Patch threading.Thread to verify __init__ wires up a daemon thread
        target without starting any real worker.

        BLDX-1129 anchor: this also exercises the path where the inline import
        target `_HTTP_POOL_TIMEOUT_SECONDS` would be resolved.
        """
        fake_thread = mock.MagicMock()
        fake_thread.is_alive.return_value = False
        with mock.patch.object(
            sc_module.threading, "Thread", return_value=fake_thread
        ) as mthread:
            # Patch Event so the 5s wait short-circuits.
            fake_event = mock.MagicMock()
            fake_event.wait.return_value = True
            with mock.patch.object(
                sc_module.threading, "Event", return_value=fake_event
            ):
                client = SegmentClient(write_key="fake-key")
        assert client.enabled is True
        # Verify Thread was constructed with daemon=True (important for clean exit).
        mthread.assert_called_once()
        kwargs = mthread.call_args.kwargs
        assert kwargs.get("daemon") is True
        # And start() was called (but on the mock, so no real thread).
        fake_thread.start.assert_called_once()


# ---------------------------------------------------------------------------
# BLDX-1129 anchor: inline import target survives
# ---------------------------------------------------------------------------


class TestInlineImportAnchor:
    def test_constants_module_exposes_inline_import_target(self):
        """The closure run_worker imports _HTTP_POOL_TIMEOUT_SECONDS lazily.

        If that symbol disappears, the bug only surfaces at runtime. This
        test catches the regression class without driving the closure.
        """
        constants_module = importlib.import_module("application_sdk.constants")
        assert hasattr(constants_module, "_HTTP_POOL_TIMEOUT_SECONDS"), (
            "BLDX-1129 anchor: SegmentClient.run_worker imports "
            "_HTTP_POOL_TIMEOUT_SECONDS lazily; symbol must exist."
        )
        # Sanity on type.
        value = constants_module._HTTP_POOL_TIMEOUT_SECONDS
        assert isinstance(value, (int, float))


# ---------------------------------------------------------------------------
# _should_send_metric
# ---------------------------------------------------------------------------


class TestShouldSendMetric:
    def test_send_to_segment_true_returns_true(self):
        client = _disabled_client()
        record = _make_record(labels={"send_to_segment": "true"})
        assert client._should_send_metric(record) is True

    def test_send_to_segment_true_case_insensitive(self):
        client = _disabled_client()
        record = _make_record(labels={"send_to_segment": "TRUE"})
        assert client._should_send_metric(record) is True

    def test_missing_label_returns_false(self):
        client = _disabled_client()
        record = _make_record(labels={})
        assert client._should_send_metric(record) is False

    def test_other_value_returns_false(self):
        client = _disabled_client()
        record = _make_record(labels={"send_to_segment": "false"})
        assert client._should_send_metric(record) is False

    def test_arbitrary_value_returns_false(self):
        client = _disabled_client()
        record = _make_record(labels={"send_to_segment": "yes"})
        assert client._should_send_metric(record) is False


# ---------------------------------------------------------------------------
# _build_track_event
# ---------------------------------------------------------------------------


class TestBuildTrackEvent:
    def test_minimal_record_shape(self):
        client = _disabled_client()
        record = _make_record(
            name="cool_metric",
            value=3.14,
            metric_type=MetricType.GAUGE,
            labels={"a": "b"},
        )
        event = client._build_track_event(record)
        assert isinstance(event, SegmentTrackEvent)
        assert event.event == "cool_metric"
        assert event.userId == client._default_user_id
        assert event.type == "track"
        # Properties contain value, metric_type, sdk_version, plus labels.
        assert event.properties["value"] == 3.14
        assert event.properties["metric_type"] == MetricType.GAUGE.value
        assert event.properties["a"] == "b"
        assert "sdk_version" in event.properties
        # No description or unit on a minimal record.
        assert "description" not in event.properties
        assert "unit" not in event.properties

    def test_optional_description_and_unit_included(self):
        client = _disabled_client()
        record = _make_record(description="desc!", unit="ms")
        event = client._build_track_event(record)
        assert event.properties["description"] == "desc!"
        assert event.properties["unit"] == "ms"

    def test_timestamp_is_iso_format(self):
        client = _disabled_client()
        record = _make_record(timestamp=1_700_000_000.0)
        event = client._build_track_event(record)
        # ISO 8601 format from datetime.fromtimestamp(...).isoformat().
        assert "T" in event.timestamp


# ---------------------------------------------------------------------------
# send_metric — sync interface, no real thread
# ---------------------------------------------------------------------------


class TestSendMetricEnabled:
    def test_filtered_metric_does_not_schedule(self):
        """A metric without send_to_segment=true should be dropped before
        touching the worker thread or asyncio.run_coroutine_threadsafe."""
        client = _enabled_client_no_thread()
        record = _make_record(labels={})  # no send_to_segment label
        with mock.patch.object(sc_module.asyncio, "run_coroutine_threadsafe") as mrun:
            client.send_metric(record)
        mrun.assert_not_called()

    def test_init_event_timeout_bails(self):
        """If the worker thread fails to initialize, send_metric must bail
        without touching the (uninitialized) loop or queue."""
        client = _enabled_client_no_thread()
        # Pretend the worker thread is alive so _start_worker_thread isn't
        # called inside send_metric.
        fake_thread = mock.MagicMock()
        fake_thread.is_alive.return_value = True
        client._worker_thread = fake_thread
        # Replace the initialized_event with a fake whose wait() returns False.
        fake_event = mock.MagicMock()
        fake_event.wait.return_value = False
        client._initialized_event = fake_event
        record = _make_record(labels={"send_to_segment": "true"})
        with mock.patch.object(sc_module.asyncio, "run_coroutine_threadsafe") as mrun:
            client.send_metric(record)
        mrun.assert_not_called()

    def test_happy_path_schedules_via_loop(self):
        """When everything is initialized, send_metric should hand the
        coroutine to asyncio.run_coroutine_threadsafe with the worker loop."""
        client = _enabled_client_no_thread()
        fake_thread = mock.MagicMock()
        fake_thread.is_alive.return_value = True
        client._worker_thread = fake_thread
        fake_event = mock.MagicMock()
        fake_event.wait.return_value = True
        client._initialized_event = fake_event
        # Fake loop and queue (queue.put returns a real coroutine that we will
        # close to avoid "coroutine was never awaited" warnings).
        client._loop = mock.MagicMock()

        async def _put(_record):
            return None

        client._queue = mock.MagicMock()
        client._queue.put = _put

        record = _make_record(labels={"send_to_segment": "true"})
        with mock.patch.object(sc_module.asyncio, "run_coroutine_threadsafe") as mrun:
            # capture the coroutine handed to run_coroutine_threadsafe so we can close it
            captured = {}

            def _capture(coro, loop):
                captured["coro"] = coro
                captured["loop"] = loop
                return mock.MagicMock()

            mrun.side_effect = _capture
            client.send_metric(record)
        assert mrun.called
        assert captured["loop"] is client._loop
        # Close coroutine to avoid "never awaited" warning.
        captured["coro"].close()

    def test_run_coroutine_threadsafe_failure_swallowed(self):
        """Any exception while scheduling must be swallowed, not propagated."""
        client = _enabled_client_no_thread()
        fake_thread = mock.MagicMock()
        fake_thread.is_alive.return_value = True
        client._worker_thread = fake_thread
        fake_event = mock.MagicMock()
        fake_event.wait.return_value = True
        client._initialized_event = fake_event
        client._loop = mock.MagicMock()

        async def _put(_r):
            return None

        client._queue = mock.MagicMock()
        client._queue.put = _put

        with mock.patch.object(
            sc_module.asyncio,
            "run_coroutine_threadsafe",
            side_effect=RuntimeError("boom"),
        ):
            # Must not raise.
            client.send_metric(_make_record(labels={"send_to_segment": "true"}))


# ---------------------------------------------------------------------------
# _send_batch_to_segment — async, mock httpx
# ---------------------------------------------------------------------------


class TestSendBatchToSegment:
    @pytest.mark.timeout(2)
    async def test_no_client_returns_quickly(self):
        client = _enabled_client_no_thread()
        client._client = None
        # Should not raise.
        await client._send_batch_to_segment([_make_record()])

    @pytest.mark.timeout(2)
    async def test_empty_list_returns_quickly(self):
        client = _enabled_client_no_thread()
        client._client = mock.AsyncMock()
        await client._send_batch_to_segment([])
        client._client.post.assert_not_called()

    @pytest.mark.timeout(2)
    async def test_post_invoked_with_basic_auth_and_payload(self):
        client = _enabled_client_no_thread()
        client._write_key = "fake-key"
        client._api_url = "https://example.test/v1/batch"
        fake_resp = mock.MagicMock()
        fake_resp.raise_for_status = mock.MagicMock()
        async_client = mock.AsyncMock()
        async_client.post = mock.AsyncMock(return_value=fake_resp)
        client._client = async_client

        record = _make_record(labels={"send_to_segment": "true"})
        await client._send_batch_to_segment([record])
        async_client.post.assert_awaited_once()
        kwargs = async_client.post.call_args.kwargs
        # URL is positional.
        args = async_client.post.call_args.args
        assert args[0] == "https://example.test/v1/batch"
        assert "json" in kwargs
        assert kwargs["json"]["batch"][0]["event"] == "test_metric"
        # Authorization header uses Basic + base64("fake-key:")
        expected_auth = base64.b64encode(b"fake-key:").decode()
        assert kwargs["headers"]["Authorization"] == f"Basic {expected_auth}"
        assert kwargs["headers"]["content-type"] == "application/json"
        fake_resp.raise_for_status.assert_called_once()

    @pytest.mark.timeout(2)
    async def test_http_error_swallowed(self):
        import httpx

        client = _enabled_client_no_thread()
        client._write_key = "k"
        async_client = mock.AsyncMock()
        async_client.post = mock.AsyncMock(side_effect=httpx.HTTPError("nope"))
        client._client = async_client
        # Must not raise — error is logged, not propagated.
        await client._send_batch_to_segment([_make_record()])

    @pytest.mark.timeout(2)
    async def test_unexpected_exception_swallowed(self):
        client = _enabled_client_no_thread()
        client._write_key = "k"
        async_client = mock.AsyncMock()
        async_client.post = mock.AsyncMock(side_effect=ValueError("boom"))
        client._client = async_client
        # Must not raise.
        await client._send_batch_to_segment([_make_record()])


# ---------------------------------------------------------------------------
# _process_queue — exercise CancelledError flush-final-batch branch
# ---------------------------------------------------------------------------


class TestProcessQueue:
    @pytest.mark.timeout(2)
    async def test_no_queue_or_client_early_return(self):
        client = _enabled_client_no_thread()
        client._queue = None
        client._client = None
        # Should return immediately without raising.
        await client._process_queue()

    @pytest.mark.timeout(2)
    async def test_cancelled_with_pending_batch_flushes(self):
        """When the worker task is cancelled while batch is non-empty,
        _send_batch_to_segment is called once with the pending batch.

        We drive the cancellation path deterministically by patching
        asyncio.wait_for to raise CancelledError on its first call (after we
        seed the batch) — this lands us directly on the cancellation branch
        without ever blocking on the queue.
        """
        client = _enabled_client_no_thread()
        client._batch_timeout_seconds = 0.1
        client._batch_size = 100
        client._queue = asyncio.Queue()
        client._client = mock.AsyncMock()

        record = _make_record()

        sent: list[list[MetricRecord]] = []

        async def _capture(batch):
            sent.append(list(batch))

        # First wait_for returns the pre-loaded record; second call raises
        # CancelledError to drive the except CancelledError branch which
        # flushes the in-flight batch.
        call_count = {"n": 0}

        async def _fake_wait_for(coro, timeout):
            call_count["n"] += 1
            # Make sure we don't leak the inner queue.get coroutine.
            try:
                coro.close()
            except Exception:  # noqa: S110 — closing dead test-scaffold coroutine; nothing to log
                pass
            if call_count["n"] == 1:
                # Simulate "queue.get returned a record".
                return record
            raise asyncio.CancelledError()

        with (
            mock.patch.object(client, "_send_batch_to_segment", side_effect=_capture),
            mock.patch.object(sc_module.asyncio, "wait_for", _fake_wait_for),
        ):
            # Mark the seeded record as task_done-able by first putting it.
            await client._queue.put(record)
            try:
                await client._process_queue()
            except asyncio.CancelledError:
                pass

        # Cancelled-path must flush the in-flight batch containing our record.
        assert sent, "expected a final-batch flush on cancellation"
        assert record in sent[-1]


# ---------------------------------------------------------------------------
# close()
# ---------------------------------------------------------------------------


class TestClose:
    def test_close_disabled_is_noop(self):
        with mock.patch.object(sc_module, "SEGMENT_WRITE_KEY", ""):
            client = SegmentClient(write_key="")
        # Must not raise; nothing to close.
        client.close()

    def test_close_cancels_task_then_joins_thread(self):
        """close() must signal cancellation then join the thread.

        The httpx client is now closed inside run_worker's finally block, so
        close() must NOT call run_until_complete or run_coroutine_threadsafe —
        that would race against the CancelledError handler's final batch send.
        """
        client = _enabled_client_no_thread()
        fake_loop = mock.MagicMock()
        client._loop = fake_loop
        fake_task = mock.MagicMock()
        fake_task.done.return_value = False
        client._worker_task = fake_task
        fake_thread = mock.MagicMock()
        fake_thread.is_alive.return_value = True
        client._worker_thread = fake_thread

        client.close()

        # 1. Cancellation must be signalled via call_soon_threadsafe.
        fake_loop.call_soon_threadsafe.assert_called_once()
        assert fake_loop.call_soon_threadsafe.call_args.args[0] is fake_task.cancel
        # 2. Thread join must be called (with the 10 s timeout).
        fake_thread.join.assert_called_once()
        join_timeout = (
            fake_thread.join.call_args.kwargs.get("timeout")
            or fake_thread.join.call_args.args[0]
        )
        assert join_timeout == 10.0
        # 3. close() must NOT touch the httpx client directly — that's run_worker's job.
        fake_loop.run_until_complete.assert_not_called()
        fake_loop.run_coroutine_threadsafe.assert_not_called()

    def test_close_already_done_task_skips_cancel(self):
        """If the worker task is already done, call_soon_threadsafe is skipped."""
        client = _enabled_client_no_thread()
        fake_loop = mock.MagicMock()
        client._loop = fake_loop
        fake_task = mock.MagicMock()
        fake_task.done.return_value = True  # already finished
        client._worker_task = fake_task
        client._worker_thread = None

        client.close()

        fake_loop.call_soon_threadsafe.assert_not_called()


# ---------------------------------------------------------------------------
# _flush_queue — async, drains queue and sends batch
# ---------------------------------------------------------------------------


class TestFlushQueue:
    @pytest.mark.timeout(2)
    async def test_no_queue_or_client_early_return(self):
        client = _enabled_client_no_thread()
        client._queue = None
        client._client = None
        await client._flush_queue()  # must not raise

    @pytest.mark.timeout(2)
    async def test_empty_queue_sends_nothing(self):
        client = _enabled_client_no_thread()
        client._queue = asyncio.Queue()
        client._client = mock.AsyncMock()
        with mock.patch.object(client, "_send_batch_to_segment") as msend:
            await client._flush_queue()
        msend.assert_not_called()

    @pytest.mark.timeout(2)
    async def test_drains_all_pending_records(self):
        client = _enabled_client_no_thread()
        client._queue = asyncio.Queue()
        records = [_make_record(name=f"m{i}") for i in range(3)]
        for r in records:
            await client._queue.put(r)
        client._client = mock.AsyncMock()

        sent: list[list] = []

        async def _capture(batch):
            sent.append(list(batch))

        with mock.patch.object(client, "_send_batch_to_segment", side_effect=_capture):
            await client._flush_queue()

        assert len(sent) == 1
        assert sent[0] == records
        assert client._queue.empty()


# ---------------------------------------------------------------------------
# flush() — async public interface, bridges to worker loop
# ---------------------------------------------------------------------------


class TestFlush:
    @pytest.mark.timeout(2)
    async def test_flush_disabled_is_noop(self):
        client = _disabled_client()
        await client.flush()  # must not raise

    @pytest.mark.timeout(2)
    async def test_flush_no_loop_is_noop(self):
        client = _enabled_client_no_thread()
        client._loop = None
        await client.flush()  # must not raise

    @pytest.mark.timeout(2)
    async def test_flush_loop_not_running_is_noop(self):
        client = _enabled_client_no_thread()
        fake_loop = mock.MagicMock()
        fake_loop.is_running.return_value = False
        client._loop = fake_loop
        client._queue = asyncio.Queue()
        with mock.patch.object(sc_module.asyncio, "run_coroutine_threadsafe") as mrun:
            await client.flush()
        mrun.assert_not_called()

    @pytest.mark.timeout(2)
    async def test_flush_schedules_flush_queue_on_worker_loop(self):
        """flush() must schedule _flush_queue on the worker loop and await it."""
        import concurrent.futures

        client = _enabled_client_no_thread()
        fake_loop = mock.MagicMock()
        fake_loop.is_running.return_value = True
        client._loop = fake_loop
        client._queue = asyncio.Queue()

        # Use a real concurrent.futures.Future that's already resolved so
        # asyncio.wrap_future completes immediately.
        cf_future: concurrent.futures.Future = concurrent.futures.Future()
        cf_future.set_result(None)

        captured: dict = {}

        def _capture(coro, loop):
            captured["coro"] = coro
            captured["loop"] = loop
            return cf_future

        with mock.patch.object(
            sc_module.asyncio, "run_coroutine_threadsafe", side_effect=_capture
        ):
            await client.flush()

        assert captured.get("loop") is fake_loop
        # The coroutine handed off should be _flush_queue's coroutine object.
        assert hasattr(captured.get("coro"), "close")
        captured["coro"].close()

    @pytest.mark.timeout(2)
    async def test_flush_times_out_and_cancels_future(self):
        """flush() cancels the concurrent future and logs warning on timeout."""
        import concurrent.futures

        client = _enabled_client_no_thread()
        fake_loop = mock.MagicMock()
        fake_loop.is_running.return_value = True
        client._loop = fake_loop
        client._queue = asyncio.Queue()

        cf_future: concurrent.futures.Future = concurrent.futures.Future()

        def _stub(coro, loop):
            coro.close()  # prevent "coroutine was never awaited" warning
            return cf_future

        with (
            mock.patch.object(
                sc_module.asyncio, "run_coroutine_threadsafe", side_effect=_stub
            ),
            mock.patch.object(
                sc_module.asyncio, "wait_for", side_effect=asyncio.TimeoutError
            ),
        ):
            await client.flush()  # must not raise

        assert cf_future.cancelled()
