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

    async def test_push_passes_http_timeout(self, mock_to_thread):
        """The configured http_timeout_s must reach push_to_gateway so a hung
        gateway can't tie up the worker thread for the prometheus_client
        default of 30s."""
        c = PushGatewayClient(
            url="http://localhost:9091",
            job="j",
            task_queue="tq",
            http_timeout_s=7.5,
        )
        with patch(_PUSH_TARGET) as mock_push:
            await c.push_now()
        assert mock_push.call_args.kwargs.get("timeout") == 7.5

    async def test_delete_on_shutdown_passes_http_timeout(self, mock_to_thread):
        """Same timeout knob must apply to DELETE_ON_SHUTDOWN; otherwise a
        down gateway at shutdown burns the prometheus_client default 30s
        and pushes us past terminationGracePeriodSeconds."""
        c = PushGatewayClient(
            url="http://localhost:9091",
            job="j",
            task_queue="tq",
            delete_on_shutdown=True,
            http_timeout_s=7.5,
        )
        with patch(_PUSH_TARGET), patch(_DELETE_TARGET) as mock_delete:
            await c.stop()
        assert mock_delete.call_args.kwargs.get("timeout") == 7.5


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
        with (
            patch(_PUSH_TARGET, side_effect=lambda *a, **kw: call_order.append("push")),
            patch(
                _DELETE_TARGET, side_effect=lambda *a, **kw: call_order.append("delete")
            ),
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
        with patch(_PUSH_TARGET), patch(_DELETE_TARGET) as mock_delete:
            await c.stop()
        mock_delete.assert_called_once()
        args, kwargs = mock_delete.call_args
        url_arg = args[0] if args else kwargs.get("gateway")
        assert "localhost:9091" in str(url_arg)

    async def test_stop_does_not_call_delete_when_flag_false(
        self, client, mock_to_thread
    ):
        with patch(_PUSH_TARGET), patch(_DELETE_TARGET) as mock_delete:
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


class TestPushGatewayClientSweep:
    """Startup-sweep behavior: reap stale ``{job=mine, instance=other}`` groups
    that previous OOM/eviction-killed pods couldn't DELETE themselves."""

    @staticmethod
    def _gateway_text(*entries: tuple[str, str, float]) -> str:
        """Build a fake Pushgateway /metrics response with the given
        ``(job, instance, push_time)`` tuples."""
        lines = [
            "# HELP push_time_seconds Last accept time.",
            "# TYPE push_time_seconds gauge",
        ]
        for job, instance, ts in entries:
            lines.append(f'push_time_seconds{{instance="{instance}",job="{job}"}} {ts}')
        return "\n".join(lines) + "\n"

    @staticmethod
    def _mock_httpx(text: str, status_code: int = 200):
        resp = MagicMock(status_code=status_code, text=text)
        # Match real httpx.Response.raise_for_status semantics: no-op on 2xx,
        # raises HTTPStatusError on 4xx/5xx. Without this, MagicMock's
        # auto-mocked raise_for_status would silently return a Mock for any
        # status, masking the failure path.
        if status_code >= 400:
            resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                f"HTTP {status_code}", request=MagicMock(), response=resp
            )
        else:
            resp.raise_for_status.return_value = None
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = resp
        return client

    @staticmethod
    def _make_client(
        *, job: str = "my-app-worker", my_instance: str = "me", staleness: float = 300.0
    ) -> PushGatewayClient:
        return PushGatewayClient(
            url="http://pg:9091",
            job=job,
            grouping_key={"instance": my_instance},
            sweep_stale_on_start=True,
            sweep_staleness_seconds=staleness,
        )

    def test_deletes_only_stale_predecessors_in_own_job(self):
        c = self._make_client()
        text = self._gateway_text(
            ("my-app-worker", "live-sibling", 9_999_999_700),  # 300s old (boundary)
            ("my-app-worker", "live-sibling-recent", 9_999_999_950),  # 50s old
            ("my-app-worker", "dead-pred", 1.0),  # ancient → must be reaped
            ("automation-engine-worker", "other-app", 1.0),  # other job → never touched
        )
        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=self._mock_httpx(text),
            ),
            patch(_DELETE_TARGET) as mock_del,
            patch("time.time", return_value=10_000_000_000),
        ):
            c._sweep_stale_predecessors_blocking()
        # Exactly one DELETE — the dead-pred only.
        assert mock_del.call_count == 1
        kwargs = mock_del.call_args.kwargs
        assert kwargs["job"] == "my-app-worker"
        assert kwargs["grouping_key"] == {"instance": "dead-pred"}

    def test_never_deletes_other_apps_jobs(self):
        c = self._make_client()
        text = self._gateway_text(
            ("automation-engine-worker", "ancient-other", 1.0),
            ("argo-worker", "ancient-argo", 1.0),
        )
        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=self._mock_httpx(text),
            ),
            patch(_DELETE_TARGET) as mock_del,
            patch("time.time", return_value=10_000_000_000),
        ):
            c._sweep_stale_predecessors_blocking()
        mock_del.assert_not_called()

    def test_never_deletes_own_instance(self):
        c = self._make_client(my_instance="me")
        text = self._gateway_text(("my-app-worker", "me", 1.0))  # ancient self
        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=self._mock_httpx(text),
            ),
            patch(_DELETE_TARGET) as mock_del,
            patch("time.time", return_value=10_000_000_000),
        ):
            c._sweep_stale_predecessors_blocking()
        mock_del.assert_not_called()

    def test_respects_staleness_threshold(self):
        c = self._make_client(staleness=300.0)
        # 250s old — under threshold, must be left alone.
        text = self._gateway_text(("my-app-worker", "recent", 9_999_999_750))
        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=self._mock_httpx(text),
            ),
            patch(_DELETE_TARGET) as mock_del,
            patch("time.time", return_value=10_000_000_000),
        ):
            c._sweep_stale_predecessors_blocking()
        mock_del.assert_not_called()

    def test_skips_when_get_fails(self):
        c = self._make_client()
        client_mock = MagicMock()
        client_mock.__enter__ = MagicMock(return_value=client_mock)
        client_mock.__exit__ = MagicMock(return_value=False)
        client_mock.get.side_effect = httpx.ConnectError("boom")
        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=client_mock,
            ),
            patch(_DELETE_TARGET) as mock_del,
        ):
            c._sweep_stale_predecessors_blocking()
        mock_del.assert_not_called()

    def test_skips_when_get_returns_5xx(self):
        c = self._make_client()
        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=self._mock_httpx("", status_code=503),
            ),
            patch(_DELETE_TARGET) as mock_del,
        ):
            c._sweep_stale_predecessors_blocking()
        mock_del.assert_not_called()

    def test_one_delete_failure_does_not_abort_rest(self):
        c = self._make_client()
        text = self._gateway_text(
            ("my-app-worker", "fails", 1.0),
            ("my-app-worker", "succeeds", 1.0),
        )
        delete_calls = []

        def fake_delete(*_a, **kwargs):
            instance = kwargs["grouping_key"]["instance"]
            delete_calls.append(instance)
            if instance == "fails":
                raise ConnectionError("transient")

        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=self._mock_httpx(text),
            ),
            patch(_DELETE_TARGET, side_effect=fake_delete),
            patch("time.time", return_value=10_000_000_000),
        ):
            c._sweep_stale_predecessors_blocking()
        # Both attempts made; one failure didn't stop the next.
        assert sorted(delete_calls) == ["fails", "succeeds"]

    def test_sweep_passes_http_timeout_to_get_and_per_group_delete(self):
        """The configured http_timeout_s must reach both the GET that
        discovers stale groups and every per-group DELETE. Without the
        DELETE timeout, a hung gateway during sweep could tie up startup
        for prometheus_client's default 30s × N stale groups."""
        c = PushGatewayClient(
            url="http://pg:9091",
            job="my-app-worker",
            grouping_key={"instance": "me"},
            sweep_stale_on_start=True,
            sweep_staleness_seconds=300.0,
            http_timeout_s=7.5,
        )
        text = self._gateway_text(("my-app-worker", "dead-pred", 1.0))
        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=self._mock_httpx(text),
            ) as mock_httpx,
            patch(_DELETE_TARGET) as mock_del,
            patch("time.time", return_value=10_000_000_000),
        ):
            c._sweep_stale_predecessors_blocking()
        # GET timeout
        assert mock_httpx.call_args.kwargs.get("timeout") == 7.5
        # Per-group DELETE timeout
        assert mock_del.call_args.kwargs.get("timeout") == 7.5

    def test_sweep_disabled_when_flag_false(self):
        c = PushGatewayClient(
            url="http://pg:9091",
            job="my-app-worker",
            sweep_stale_on_start=False,
        )
        # Even with stale data sitting in the gateway, sweep is skipped.
        text = self._gateway_text(("my-app-worker", "ancient", 1.0))
        with (
            patch(
                "application_sdk.observability.pushgateway.httpx.Client",
                return_value=self._mock_httpx(text),
            ) as mock_httpx,
            patch(_DELETE_TARGET) as mock_del,
        ):
            # Calling start would normally trigger sweep; since flag=False
            # the sweep method shouldn't be called at all.
            import asyncio

            async def _drive():
                with patch(_PUSH_TARGET):
                    await c.start()
                    await c.stop()

            asyncio.run(_drive())
        mock_httpx.assert_not_called()
        mock_del.assert_not_called()
