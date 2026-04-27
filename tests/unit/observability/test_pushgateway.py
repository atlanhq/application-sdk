"""Unit tests for TemporalCoreCollector and PushGatewayClient."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from application_sdk.observability.pushgateway import (
    PushGatewayClient,
    TemporalCoreCollector,
)

_PUSH_TARGET = "application_sdk.observability.pushgateway.push_to_gateway"
_DELETE_TARGET = "application_sdk.observability.pushgateway.delete_from_gateway"
_TO_THREAD_TARGET = "application_sdk.observability.pushgateway.asyncio.to_thread"

_PROMETHEUS_TEXT = (
    "# HELP test_counter A test counter\n"
    "# TYPE test_counter counter\n"
    "test_counter 1.0\n"
)


@pytest.fixture
def mock_to_thread():
    async def inline(func, *args, **kwargs):
        return func(*args, **kwargs)

    with patch(_TO_THREAD_TARGET, side_effect=inline):
        yield


@pytest.fixture
def client():
    return PushGatewayClient(
        url="http://localhost:9091",
        job="test-job",
        task_queue="tq",
    )


class TestTemporalCoreCollector:
    def _make_collector(self):
        return TemporalCoreCollector(url="http://127.0.0.1:9464/metrics")

    def _mock_httpx_response(
        self, status_code: int = 200, text: str = _PROMETHEUS_TEXT
    ):
        mock_resp = MagicMock()
        mock_resp.status_code = status_code
        mock_resp.text = text
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        return mock_client

    def test_collect_yields_metrics_on_200(self):
        collector = self._make_collector()
        mock_client = self._mock_httpx_response(status_code=200)
        with patch(
            "application_sdk.observability.pushgateway.httpx.Client",
            return_value=mock_client,
        ):
            result = list(collector.collect())
        assert len(result) > 0

    def test_collect_returns_empty_on_non_200(self):
        collector = self._make_collector()
        mock_client = self._mock_httpx_response(status_code=503)
        with patch(
            "application_sdk.observability.pushgateway.httpx.Client",
            return_value=mock_client,
        ):
            result = list(collector.collect())
        assert result == []

    def test_collect_returns_empty_on_connect_error(self):
        collector = self._make_collector()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = httpx.ConnectError("refused")
        with patch(
            "application_sdk.observability.pushgateway.httpx.Client",
            return_value=mock_client,
        ):
            result = list(collector.collect())
        assert result == []

    def test_collect_swallows_any_exception(self):
        collector = self._make_collector()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = RuntimeError("unexpected")
        with patch(
            "application_sdk.observability.pushgateway.httpx.Client",
            return_value=mock_client,
        ):
            result = list(collector.collect())
        assert result == []


class TestPushGatewayClientInit:
    def test_empty_url_raises(self):
        with pytest.raises(ValueError, match="url"):
            PushGatewayClient(url="", job="job")

    def test_empty_job_raises(self):
        with pytest.raises(ValueError, match="job"):
            PushGatewayClient(url="http://localhost:9091", job="")

    def test_default_grouping_key_excludes_task_queue_to_avoid_label_conflict(self):
        # task_queue is intentionally absent from the default grouping key:
        # Temporal metrics already carry task_queue as a metric label, and
        # the Pushgateway rejects pushes where a label appears in both the
        # metric body and the grouping-key URL path (400 Bad Request).
        c = PushGatewayClient(url="http://localhost:9091", job="j", task_queue="my-q")
        assert "task_queue" not in c._grouping_key
        assert "instance" in c._grouping_key
        assert c._grouping_key["instance"]  # non-empty hostname

    def test_custom_grouping_key_overrides_default(self):
        custom = {"region": "us-east-1"}
        c = PushGatewayClient(url="http://localhost:9091", job="j", grouping_key=custom)
        assert c._grouping_key is custom


class TestPushGatewayClientPushNow:
    async def test_push_now_calls_push_to_gateway(self, client, mock_to_thread):
        with patch(_PUSH_TARGET) as mock_push:
            await client.push_now()
        mock_push.assert_called_once()

    async def test_push_now_passes_correct_url_and_job(self, client, mock_to_thread):
        with patch(_PUSH_TARGET) as mock_push:
            await client.push_now()
        _, kwargs = mock_push.call_args
        assert (
            kwargs.get("job") == "test-job" or mock_push.call_args[0][1] == "test-job"
        )
        # push_to_gateway(url, job=..., registry=..., grouping_key=...)
        args, kwargs = mock_push.call_args
        url_arg = args[0] if args else kwargs.get("gateway")
        assert "localhost:9091" in str(url_arg)

    async def test_push_now_propagates_push_exception(self, client, mock_to_thread):
        with patch(_PUSH_TARGET, side_effect=ConnectionError("refused")):
            with pytest.raises(ConnectionError):
                await client.push_now()


class TestPushGatewayClientStop:
    async def test_stop_makes_final_push(self, client, mock_to_thread):
        with patch(_PUSH_TARGET) as mock_push:
            await client.stop()
        mock_push.assert_called_once()

    async def test_stop_calls_push_before_delete_when_flag_set(self, mock_to_thread):
        c = PushGatewayClient(
            url="http://localhost:9091",
            job="j",
            task_queue="tq",
            delete_on_shutdown=True,
        )
        call_order: list[str] = []
        with patch(
            _PUSH_TARGET, side_effect=lambda *a, **kw: call_order.append("push")
        ):
            with patch(
                _DELETE_TARGET, side_effect=lambda *a, **kw: call_order.append("delete")
            ):
                await c.stop()
        assert call_order == ["push", "delete"]

    async def test_stop_calls_delete_when_delete_on_shutdown_true(self, mock_to_thread):
        c = PushGatewayClient(
            url="http://localhost:9091",
            job="my-job",
            task_queue="tq",
            delete_on_shutdown=True,
        )
        with patch(_PUSH_TARGET):
            with patch(_DELETE_TARGET) as mock_delete:
                await c.stop()
        mock_delete.assert_called_once()
        args, kwargs = mock_delete.call_args
        url_arg = args[0] if args else kwargs.get("gateway")
        assert "localhost:9091" in str(url_arg)

    async def test_stop_does_not_call_delete_when_flag_false(
        self, client, mock_to_thread
    ):
        with patch(_PUSH_TARGET):
            with patch(_DELETE_TARGET) as mock_delete:
                await client.stop()
        mock_delete.assert_not_called()

    async def test_stop_swallows_push_failure(self, client, mock_to_thread):
        with patch(_PUSH_TARGET, side_effect=ConnectionError("gateway down")):
            await client.stop()  # must not raise


class TestPushGatewayClientLifecycle:
    async def test_start_creates_background_task(self, client, mock_to_thread):
        with patch(_PUSH_TARGET):
            await client.start()
            assert client._task is not None
            await client.stop()

    async def test_start_is_idempotent(self, client, mock_to_thread):
        with patch(_PUSH_TARGET):
            await client.start()
            first_task = client._task
            await client.start()
            assert client._task is first_task
            await client.stop()
