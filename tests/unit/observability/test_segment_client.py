"""Unit tests for application_sdk.observability.segment_client.

CRITICAL CONSTRAINTS:
- No real HTTP. Only ``test_worker_init_builds_a_bounded_queue`` runs a real
  worker, on the shared observability loop; everything else mocks it.
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
from tests.unit.private_stdlib import private_stdlib

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
    """Construct a client with no write key — no worker is started."""
    return SegmentClient(write_key="")


def _enabled_client_no_worker() -> SegmentClient:
    """Construct an enabled client while patching out _start_worker.

    The resulting client has enabled=True but no worker on any loop.
    Tests can manually attach mocks for _client / _queue / _loop / _worker.
    """
    with mock.patch.object(SegmentClient, "_start_worker", return_value=None):
        client = SegmentClient(write_key="fake-key")
    # Sanity: worker was not started.
    assert client._worker is None
    return client


def _running_worker() -> mock.MagicMock:
    """A stand-in ``BackgroundTask`` handle for a worker that is running."""
    worker = mock.MagicMock()
    worker.done.return_value = False
    return worker


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
        assert client._worker is None
        assert client._loop is None

    def test_send_metric_noop_when_disabled(self):
        with mock.patch.object(sc_module, "SEGMENT_WRITE_KEY", ""):
            client = SegmentClient(write_key="")
        # Should silently no-op — even if metric has send_to_segment=true.
        record = _make_record(labels={"send_to_segment": "true"})
        # No worker is ever started on the shared loop.
        with mock.patch.object(sc_module, "observability_loop") as shared_loop:
            client.send_metric(record)
        shared_loop.start.assert_not_called()

    def test_close_noop_when_disabled(self):
        with mock.patch.object(sc_module, "SEGMENT_WRITE_KEY", ""):
            client = SegmentClient(write_key="")
        # Should not raise even though there's no loop/worker/client.
        client.close()


# ---------------------------------------------------------------------------
# Enabled construction — but no real worker
# ---------------------------------------------------------------------------


class TestEnabledConstruction:
    def test_construction_starts_worker_once(self):
        """__init__ should invoke _start_worker when enabled."""
        with mock.patch.object(SegmentClient, "_start_worker", autospec=True) as start:
            client = SegmentClient(write_key="fake-key")
        assert client.enabled is True
        assert start.call_count == 1

    def test_explicit_overrides_used(self):
        with mock.patch.object(SegmentClient, "_start_worker", return_value=None):
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

    def test_worker_runs_on_the_shared_observability_loop(self):
        """FND-4648: the worker is a task on the shared observability loop,
        not a thread with a private loop, and its queue is ready the moment
        construction returns (no init wait).

        BLDX-1129 anchor: this also exercises the path where the inline import
        target `_HTTP_POOL_TIMEOUT_SECONDS` is resolved.
        """
        with mock.patch.object(sc_module, "observability_loop") as shared_loop:
            shared_loop.closed = False
            client = SegmentClient(write_key="fake-key")
        shared_loop.start.assert_called_once()
        coro = shared_loop.start.call_args.args[0]
        # Stopped after the periodic flushes, whose metrics drain uses it.
        assert shared_loop.start.call_args.kwargs == {
            "name": "segment-worker",
            "stop_order": 1,
        }
        coro.close()
        handle = shared_loop.start.return_value
        assert client._worker is handle
        assert client._loop is handle.loop
        assert client._queue is not None
        assert client._client is not None
        asyncio.run(client._client.aclose())

    def test_shared_loop_shut_down_starts_nothing(self):
        """After the observability loop's exit shutdown, no worker starts and
        ``send_metric`` has nowhere to hand off to."""
        with mock.patch.object(sc_module, "observability_loop") as shared_loop:
            shared_loop.closed = True
            client = SegmentClient(write_key="fake-key")
            client.send_metric(_make_record(labels={"send_to_segment": "true"}))
        shared_loop.start.assert_not_called()
        assert client._worker is None
        assert not client._ingress


# ---------------------------------------------------------------------------
# BLDX-1129 anchor: inline import target survives
# ---------------------------------------------------------------------------


class TestInlineImportAnchor:
    def test_constants_module_exposes_inline_import_target(self):
        """``_start_worker`` imports _HTTP_POOL_TIMEOUT_SECONDS lazily.

        If that symbol disappears, the bug only surfaces at runtime. This
        test catches the regression class without driving the closure.
        """
        constants_module = importlib.import_module("application_sdk.constants")
        assert hasattr(constants_module, "_HTTP_POOL_TIMEOUT_SECONDS"), (
            "BLDX-1129 anchor: SegmentClient._start_worker imports "
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

    def test_userid_uses_tenant_name_label_when_present(self):
        """tenant_name takes precedence over other tenant identifiers and is
        appended to the default namespace prefix."""
        client = _disabled_client()
        record = _make_record(
            labels={
                "tenant_name": "acme-tenant-01",
                "atlan_base_url": "https://acme.example.com",
                "tenant_id": "default",
            }
        )
        event = client._build_track_event(record)
        assert event.userId == f"{client._default_user_id}:acme-tenant-01"

    def test_userid_falls_back_to_atlan_base_url(self):
        """When tenant_name is absent, atlan_base_url is appended."""
        client = _disabled_client()
        record = _make_record(
            labels={
                "atlan_base_url": "https://acme.example.com",
                "tenant_id": "default",
            }
        )
        event = client._build_track_event(record)
        assert event.userId == f"{client._default_user_id}:https://acme.example.com"

    def test_userid_falls_back_to_tenant_id(self):
        """When tenant_name and atlan_base_url absent, tenant_id is appended."""
        client = _disabled_client()
        record = _make_record(labels={"tenant_id": "some-tenant"})
        event = client._build_track_event(record)
        assert event.userId == f"{client._default_user_id}:some-tenant"

    def test_userid_falls_back_to_default_when_no_tenant_labels(self):
        """With no tenant labels, the bare default user id is used (no suffix)."""
        client = _disabled_client()
        record = _make_record(labels={"unrelated": "x"})
        event = client._build_track_event(record)
        assert event.userId == client._default_user_id

    def test_userid_ignores_empty_string_tenant_labels(self):
        """Empty string tenant labels fall through to the next candidate."""
        client = _disabled_client()
        record = _make_record(
            labels={
                "tenant_name": "",
                "atlan_base_url": "https://acme.example.com",
            }
        )
        event = client._build_track_event(record)
        assert event.userId == f"{client._default_user_id}:https://acme.example.com"


# ---------------------------------------------------------------------------
# send_metric — sync interface, no real thread
# ---------------------------------------------------------------------------


class TestSendMetricEnabled:
    def test_filtered_metric_does_not_schedule(self):
        """A metric without send_to_segment=true should be dropped before
        touching the worker or asyncio.run_coroutine_threadsafe."""
        client = _enabled_client_no_worker()
        record = _make_record(labels={})  # no send_to_segment label
        with (
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "run_coroutine_threadsafe") as mrun,
        ):
            client.send_metric(record)
        mrun.assert_not_called()

    def test_no_worker_loop_bails(self):
        """If no worker could be started (``_loop``/``_queue`` unset),
        send_metric must bail without scheduling anything."""
        client = _enabled_client_no_worker()
        record = _make_record(labels={"send_to_segment": "true"})
        with (
            mock.patch.object(client, "_start_worker") as start,
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "run_coroutine_threadsafe") as mrun,
        ):
            client.send_metric(record)
        start.assert_called_once_with()
        mrun.assert_not_called()
        assert not client._ingress

    def test_happy_path_schedules_via_loop(self):
        """When everything is initialized, send_metric puts the record in the
        bounded hand-off and schedules the worker loop's ``_drain_ingress`` via
        ``call_soon_threadsafe`` — never a parked ``await queue.put`` coroutine,
        which a bounded queue would turn into an unbounded backlog of its own."""
        client = _enabled_client_no_worker()
        client._worker = _running_worker()
        client._loop = mock.MagicMock()
        client._queue = mock.MagicMock()

        record = _make_record(labels={"send_to_segment": "true"})
        with (
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "run_coroutine_threadsafe") as mrun,
        ):
            client.send_metric(record)
        mrun.assert_not_called()
        client._loop.call_soon_threadsafe.assert_called_once_with(client._drain_ingress)
        assert list(client._ingress) == [record]

    def test_scheduling_failure_swallowed(self):
        """Any exception while scheduling must be swallowed, not propagated."""
        client = _enabled_client_no_worker()
        client._worker = _running_worker()
        client._loop = mock.MagicMock()
        client._loop.call_soon_threadsafe.side_effect = RuntimeError("boom")
        client._queue = mock.MagicMock()

        # Must not raise.
        client.send_metric(_make_record(labels={"send_to_segment": "true"}))


# ---------------------------------------------------------------------------
# _send_batch_to_segment — async, mock httpx
# ---------------------------------------------------------------------------


# The timeout(30) marks only turn a hang into a failure. They also count fixture
# setup, and on Windows pytest-timeout's thread method kills the whole xdist
# worker, so a bound a passing test could miss on a loaded runner is a crash.
class TestSendBatchToSegment:
    @pytest.mark.timeout(30)
    async def test_no_client_returns_quickly(self):
        client = _enabled_client_no_worker()
        client._client = None
        # Should not raise.
        await client._send_batch_to_segment([_make_record()])

    @pytest.mark.timeout(30)
    async def test_empty_list_returns_quickly(self):
        client = _enabled_client_no_worker()
        client._client = mock.AsyncMock()
        await client._send_batch_to_segment([])
        client._client.post.assert_not_called()

    @pytest.mark.timeout(30)
    async def test_post_invoked_with_basic_auth_and_payload(self):
        client = _enabled_client_no_worker()
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

    @pytest.mark.timeout(30)
    async def test_http_error_swallowed(self):
        import httpx

        client = _enabled_client_no_worker()
        client._write_key = "k"
        async_client = mock.AsyncMock()
        async_client.post = mock.AsyncMock(side_effect=httpx.HTTPError("nope"))
        client._client = async_client
        # Must not raise — error is logged, not propagated.
        await client._send_batch_to_segment([_make_record()])

    @pytest.mark.timeout(30)
    async def test_unexpected_exception_swallowed(self):
        client = _enabled_client_no_worker()
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
    @pytest.mark.timeout(30)
    async def test_cancelled_with_pending_batch_flushes(self):
        """When the worker task is cancelled while batch is non-empty,
        _send_batch_to_segment is called once with the pending batch.

        We drive the cancellation path deterministically by patching
        asyncio.wait_for to raise CancelledError on its first call (after we
        seed the batch) — this lands us directly on the cancellation branch
        without ever blocking on the queue.
        """
        client = _enabled_client_no_worker()
        client._batch_timeout_seconds = 0.1
        client._batch_size = 100
        client._queue = asyncio.Queue()
        client._client = mock.AsyncMock()

        record = _make_record()

        sent: list[list[MetricRecord]] = []

        async def _capture(batch, client=None):
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
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "wait_for", _fake_wait_for),
        ):
            # Mark the seeded record as task_done-able by first putting it.
            await client._queue.put(record)
            try:
                await client._process_queue(client._queue, client._client)
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

    def test_close_cancels_worker_then_waits(self):
        """close() cancels the worker and waits for it, bounded at 10 s.

        The httpx client is closed inside ``_run_worker``'s finally block, so
        close() must not touch it — that would race the CancelledError
        handler's final batch send.
        """
        client = _enabled_client_no_worker()
        worker = _running_worker()
        client._worker = worker
        client._client = mock.AsyncMock()

        client.close()

        worker.cancel.assert_called_once_with()
        worker.wait.assert_called_once_with(timeout=10.0)
        client._client.aclose.assert_not_called()
        assert client._closed

    def test_close_already_done_worker_skips_cancel(self):
        """If the worker already finished, there is nothing to cancel."""
        client = _enabled_client_no_worker()
        worker = mock.MagicMock()
        worker.done.return_value = True
        client._worker = worker

        client.close()

        worker.cancel.assert_not_called()
        worker.wait.assert_not_called()


# ---------------------------------------------------------------------------
# _flush_queue — async, drains queue and sends batch
# ---------------------------------------------------------------------------


class TestFlushQueue:
    @pytest.mark.timeout(30)
    async def test_no_queue_or_client_early_return(self):
        client = _enabled_client_no_worker()
        client._queue = None
        client._client = None
        await client._flush_queue()  # must not raise

    @pytest.mark.timeout(30)
    async def test_empty_queue_sends_nothing(self):
        client = _enabled_client_no_worker()
        client._queue = asyncio.Queue()
        client._client = mock.AsyncMock()
        with mock.patch.object(client, "_send_batch_to_segment") as msend:
            await client._flush_queue()
        msend.assert_not_called()

    @pytest.mark.timeout(30)
    async def test_drains_all_pending_records(self):
        client = _enabled_client_no_worker()
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
    @pytest.mark.timeout(30)
    async def test_flush_disabled_is_noop(self):
        client = _disabled_client()
        await client.flush()  # must not raise

    @pytest.mark.timeout(30)
    async def test_flush_no_loop_is_noop(self):
        client = _enabled_client_no_worker()
        client._loop = None
        await client.flush()  # must not raise

    @pytest.mark.timeout(30)
    async def test_flush_loop_not_running_is_noop(self):
        client = _enabled_client_no_worker()
        fake_loop = mock.MagicMock()
        fake_loop.is_running.return_value = False
        client._loop = fake_loop
        client._queue = asyncio.Queue()
        with (
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "run_coroutine_threadsafe") as mrun,
        ):
            await client.flush()
        mrun.assert_not_called()

    @pytest.mark.timeout(30)
    async def test_flush_schedules_flush_queue_on_worker_loop(self):
        """flush() must schedule _flush_queue on the worker loop and await it."""
        import concurrent.futures

        client = _enabled_client_no_worker()
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

        with (
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(
                local_asyncio, "run_coroutine_threadsafe", side_effect=_capture
            ),
        ):
            await client.flush()

        assert captured.get("loop") is fake_loop
        # The coroutine handed off should be _flush_queue's coroutine object.
        assert hasattr(captured.get("coro"), "close")
        captured["coro"].close()

    @pytest.mark.timeout(30)
    async def test_flush_times_out_and_cancels_future(self):
        """flush() cancels the concurrent future and logs warning on timeout."""
        import concurrent.futures

        client = _enabled_client_no_worker()
        fake_loop = mock.MagicMock()
        fake_loop.is_running.return_value = True
        client._loop = fake_loop
        client._queue = asyncio.Queue()

        cf_future: concurrent.futures.Future = concurrent.futures.Future()

        def _stub(coro, loop):
            coro.close()  # prevent "coroutine was never awaited" warning
            return cf_future

        with (
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(
                local_asyncio, "run_coroutine_threadsafe", side_effect=_stub
            ),
            mock.patch.object(
                local_asyncio, "wait_for", side_effect=asyncio.TimeoutError
            ),
        ):
            await client.flush()  # must not raise

        assert cf_future.cancelled()

    @pytest.mark.timeout(30)
    async def test_flush_inside_temporal_workflow_does_not_bridge_worker_loop(self):
        """Avoid the unsupported asyncio bridge from a Temporal workflow.

        Temporal's workflow loop raises NotImplementedError from is_closed() when
        asyncio.wrap_future wires the worker future back to the workflow context.
        """
        client = _enabled_client_no_worker()
        fake_loop = mock.MagicMock()
        fake_loop.is_running.return_value = True
        client._loop = fake_loop
        client._queue = asyncio.Queue()

        with (
            mock.patch.object(sc_module, "in_temporal_workflow", return_value=True),
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "run_coroutine_threadsafe") as mrun,
        ):
            await client.flush()

        mrun.assert_not_called()


class TestFlushOnWorkerLoop:
    @pytest.mark.timeout(30)
    async def test_flush_on_the_worker_loop_awaits_directly(self):
        """FND-4648: the metrics flush runs on the shared loop the worker
        runs on, so flush() awaits ``_flush_queue`` there instead of bridging
        to itself through ``run_coroutine_threadsafe``."""
        client = _enabled_client_no_worker()
        client._loop = asyncio.get_running_loop()
        client._queue = asyncio.Queue()
        with (
            mock.patch.object(client, "_flush_queue", new=mock.AsyncMock()) as fq,
            private_stdlib(sc_module, "asyncio") as local_asyncio,
            mock.patch.object(local_asyncio, "run_coroutine_threadsafe") as mrun,
        ):
            await client.flush()
        fq.assert_awaited_once_with()
        mrun.assert_not_called()


# ---------------------------------------------------------------------------
# Bounded queue, chunked flush, serialised start, closed flag
# ---------------------------------------------------------------------------


def _ready_client(max_queue_size: int) -> SegmentClient:
    """An enabled client that looks initialized, with a mock worker loop whose
    ``call_soon_threadsafe`` records callbacks but never runs them — a worker
    loop that has fallen behind."""
    with mock.patch.object(SegmentClient, "_start_worker", return_value=None):
        client = SegmentClient(write_key="fake-key", max_queue_size=max_queue_size)
    client._worker = _running_worker()
    client._loop = mock.MagicMock()
    client._queue = asyncio.Queue(maxsize=max_queue_size)
    return client


class TestBoundedQueue:
    def test_lagging_loop_holds_one_callback_and_a_capped_handoff(self):
        """While the worker loop runs nothing, events beyond ``max_queue_size``
        are dropped at admission and only one drain callback is scheduled — no
        per-event callback holding its record outside the cap."""
        client = _ready_client(max_queue_size=3)
        with mock.patch.object(sc_module.logging, "warning") as warn:
            for i in range(50):
                client.send_metric(
                    _make_record(value=float(i), labels={"send_to_segment": "true"})
                )

        assert client._loop.call_soon_threadsafe.call_count == 1
        assert len(client._ingress) == 3
        assert client.dropped_count == 47
        # One warning for the overflow episode, not one per dropped event.
        assert warn.call_count == 1

    async def test_drain_fills_queue_and_counts_overflow(self):
        """The drain moves the hand-off into the queue up to its cap, counts
        what does not fit, and re-arms scheduling for the next event."""
        client = _ready_client(max_queue_size=2)
        client._queue.put_nowait(_make_record())  # one slot already taken
        for _ in range(2):
            client.send_metric(_make_record(labels={"send_to_segment": "true"}))

        with mock.patch.object(sc_module.logging, "warning"):
            client._drain_ingress()

        assert client._queue.qsize() == 2
        assert client.dropped_count == 1
        assert not client._ingress
        client.send_metric(_make_record(labels={"send_to_segment": "true"}))
        assert client._loop.call_soon_threadsafe.call_count == 2

    async def test_drop_warning_rearms_after_an_event_is_queued(self):
        client = _ready_client(max_queue_size=1)

        def send() -> None:
            client.send_metric(_make_record(labels={"send_to_segment": "true"}))

        with mock.patch.object(sc_module.logging, "warning") as warn:
            send()
            send()  # hand-off full → drop, warn
            client._drain_ingress()  # queued → episode over
            client._queue.get_nowait()
            send()
            send()  # hand-off full → drop, warn again

        assert client.dropped_count == 2
        assert warn.call_count == 2

    def test_negative_max_queue_size_rejected(self):
        """``asyncio.Queue`` treats a non-positive size as unbounded, so a
        negative value would silently remove the cap."""
        with pytest.raises(ValueError, match="max_queue_size"):
            SegmentClient(write_key="fake-key", max_queue_size=-1)

    @pytest.mark.parametrize(
        ("configured", "expected"),
        [(7, 7), (0, sc_module._DEFAULT_MAX_QUEUE_SIZE)],
    )
    def test_worker_init_builds_a_bounded_queue(self, configured, expected):
        """The queue the real worker uses carries the cap — not one a test
        substituted. Starts and closes a real worker on the shared
        observability loop; no request is sent."""
        client = SegmentClient(write_key="fake-key", max_queue_size=configured)
        try:
            assert client._worker is not None
            assert client._queue is not None
            assert client._queue.maxsize == expected > 0
        finally:
            client.close()
        assert client._worker.done()


class TestFlushQueueChunking:
    async def test_backlog_is_sent_in_batch_size_chunks(self):
        """A post-outage backlog is posted ``_batch_size`` events per request,
        not as one oversized request Segment rejects."""
        client = _enabled_client_no_worker()
        client._batch_size = 4
        client._queue = asyncio.Queue()
        client._client = mock.AsyncMock()
        for i in range(10):
            client._queue.put_nowait(_make_record(value=float(i)))

        sent: list[int] = []

        async def _capture(batch):
            sent.append(len(batch))

        with mock.patch.object(client, "_send_batch_to_segment", side_effect=_capture):
            await client._flush_queue()

        assert sent == [4, 4, 2]
        assert client._queue.empty()


class TestWorkerStartAndClose:
    def test_concurrent_start_starts_one_worker(self):
        """Concurrent callers that all see no live worker start exactly one.

        Deterministic contention: the first caller holds the start (blocked on
        ``gate``) until every caller has reached ``_start_lock``, so the other
        seven are all waiting on the lock when it is released.
        """
        import threading  # noqa: PLC0415 — only this test needs real threads

        client = _enabled_client_no_worker()
        callers_n = 8
        started: list[int] = []
        gate = threading.Event()
        arrived = threading.Semaphore(0)
        real_lock = client._start_lock

        class _ArrivalLock:
            def __enter__(self):
                arrived.release()
                return real_lock.__enter__()

            def __exit__(self, *exc):
                return real_lock.__exit__(*exc)

        client._start_lock = _ArrivalLock()  # type: ignore[assignment]

        def _fake_start(coro, *, name, stop_order):
            coro.close()
            gate.wait(10.0)
            started.append(1)
            return _running_worker()

        with mock.patch.object(sc_module, "observability_loop") as shared_loop:
            shared_loop.closed = False
            shared_loop.start.side_effect = _fake_start
            callers = [
                threading.Thread(target=client._start_worker) for _ in range(callers_n)
            ]
            for t in callers:
                t.start()
            for _ in range(callers_n):
                assert arrived.acquire(timeout=10.0), "a caller never reached the lock"
            gate.set()
            for t in callers:
                t.join(10.0)

        assert started == [1]

    def test_send_metric_after_close_starts_no_worker(self):
        """A metric emitted after ``close()`` is dropped; it must not start a
        worker nothing will stop."""
        client = _enabled_client_no_worker()
        client.close()
        with mock.patch.object(sc_module, "observability_loop") as shared_loop:
            client.send_metric(_make_record(labels={"send_to_segment": "true"}))
            client._start_worker()
        shared_loop.start.assert_not_called()
