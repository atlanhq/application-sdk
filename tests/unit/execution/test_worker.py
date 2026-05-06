"""Unit tests for Temporal worker creation and configuration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.worker import AppWorker, create_worker

DRAIN_DELAY_PATCH = (
    "application_sdk.execution._temporal.worker.SHUTDOWN_DRAIN_DELAY_SECONDS"
)

_MINIMAL_START_PARAMS = {
    "task_queue": "test-queue",
    "app_name": "test-app",
    "workflow_count": 0,
    "activity_count": 0,
    "max_concurrent_activities": 1,
}


# Module-level types so get_type_hints can resolve them
@dataclass
class _WorkerInput(Input, allow_unbounded_fields=True):
    name: str = "test"


@dataclass
class _WorkerOutput(Output, allow_unbounded_fields=True):
    result: str = ""


@dataclass
class _FilterIn1(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _FilterOut1(Output, allow_unbounded_fields=True):
    y: str = ""


@dataclass
class _FilterIn2(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _FilterOut2(Output, allow_unbounded_fields=True):
    y: str = ""


def _make_mock_client() -> mock.MagicMock:
    """Create a mock Temporal client."""
    client = mock.MagicMock()
    client.namespace = "default"
    # Mock service_client.config.target_host
    client.service_client = mock.MagicMock()
    client.service_client.config = mock.MagicMock()
    client.service_client.config.target_host = "localhost:7233"
    return client


class TestCreateWorker:
    """Tests for create_worker()."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_returns_app_worker_instance(self) -> None:
        class _WorkerTestApp(App):
            @task(timeout_seconds=60)
            async def do_work(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker"
        ) as MockWorker:
            MockWorker.return_value = mock.MagicMock()
            result = create_worker(client)

        assert isinstance(result, AppWorker)

    def test_create_worker_includes_observability_trio(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """LogInterceptor, MetricsInterceptor, TraceInterceptor are
        unconditional and the EventInterceptor stays gated by env var."""
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "true")
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "true")

        class _InterceptorApp(App):
            @task(timeout_seconds=60)
            async def some_task(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        interceptors_used: list = []

        def capture_worker(*args, **kwargs):
            interceptors_used.extend(kwargs.get("interceptors", []))
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client)

        interceptor_types = [type(i).__name__ for i in interceptors_used]
        assert "LogInterceptor" in interceptor_types
        assert "MetricsInterceptor" in interceptor_types
        assert "TraceInterceptor" in interceptor_types
        assert "EventInterceptor" in interceptor_types
        # CleanupInterceptor is no longer registered — cleanup is via App.on_complete()
        assert "CleanupInterceptor" not in interceptor_types

    def test_event_interceptor_disabled_via_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "false")
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "true")

        class _NoEventApp(App):
            @task(timeout_seconds=60)
            async def no_event_task(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        interceptors_used: list = []

        def capture_worker(*args, **kwargs):
            interceptors_used.extend(kwargs.get("interceptors", []))
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client)

        interceptor_types = [type(i).__name__ for i in interceptors_used]
        assert "EventInterceptor" not in interceptor_types
        # The observability trio still runs.
        assert "LogInterceptor" in interceptor_types
        assert "MetricsInterceptor" in interceptor_types
        assert "TraceInterceptor" in interceptor_types

    def test_all_registered_apps_activities_included(self) -> None:
        class _FilterAppA(App):
            @task(timeout_seconds=60)
            async def task_alpha(self, input: _FilterIn1) -> _FilterOut1:
                return _FilterOut1()

            async def run(self, input: _FilterIn1) -> _FilterOut1:
                return _FilterOut1()

        class _FilterAppB(App):
            @task(timeout_seconds=60)
            async def task_beta(self, input: _FilterIn2) -> _FilterOut2:
                return _FilterOut2()

            async def run(self, input: _FilterIn2) -> _FilterOut2:
                return _FilterOut2()

        client = _make_mock_client()
        activities_used: list = []

        def capture_worker(*args, **kwargs):
            activities_used.extend(kwargs.get("activities", []))
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client)

        # Activities from both apps should be present
        user_app_names = {
            a._task_metadata.app_name  # type: ignore[attr-defined]
            for a in activities_used
            if hasattr(a, "_task_metadata")
        }
        assert "_filter-app-a" in user_app_names
        assert "_filter-app-b" in user_app_names

    def test_rejects_caller_supplied_log_interceptor(self) -> None:
        """``create_worker(interceptors=[LogInterceptor()])`` must fail loudly:
        the SDK adds the observability trio automatically and a duplicate would
        double-count metrics and emit duplicate lifecycle log lines."""
        from application_sdk.execution._temporal.interceptors.log import LogInterceptor

        class _DupApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        with pytest.raises(ValueError, match="LogInterceptor"):
            create_worker(client, interceptors=[LogInterceptor()])

    def test_rejects_caller_supplied_metrics_interceptor(self) -> None:
        from application_sdk.execution._temporal.interceptors.metrics import (
            MetricsInterceptor,
        )

        class _DupApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        with pytest.raises(ValueError, match="MetricsInterceptor"):
            create_worker(client, interceptors=[MetricsInterceptor()])

    def test_rejects_caller_supplied_trace_interceptor(self) -> None:
        from application_sdk.execution._temporal.interceptors.trace import (
            TraceInterceptor,
        )

        class _DupApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        with pytest.raises(ValueError, match="TraceInterceptor"):
            create_worker(client, interceptors=[TraceInterceptor()])

    def test_sdr_workflows_skipped_when_no_handler(self) -> None:
        """SDR registration is silently skipped when no Handler is provided."""

        class _NoHandlerApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        captured: dict = {}

        def capture_worker(*args, **kwargs):
            captured["workflows"] = list(kwargs.get("workflows", []))
            captured["activities"] = list(kwargs.get("activities", []))
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client)

        from application_sdk.execution._temporal.sdr import SDR_WORKFLOWS

        for sdr_wf in SDR_WORKFLOWS:
            assert sdr_wf not in captured["workflows"]
        activity_names = [
            getattr(a, "__temporal_activity_definition", None).name  # type: ignore[union-attr]
            for a in captured["activities"]
            if hasattr(a, "__temporal_activity_definition")
        ]
        assert not any(n.startswith("sdr:") for n in activity_names)

    def test_sdr_workflows_registered_when_handler_provided(self) -> None:
        """When ``handler`` is provided, SDR workflows + activities are appended."""

        from application_sdk.handler.base import DefaultHandler

        class _WithHandlerApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        captured: dict = {}

        def capture_worker(*args, **kwargs):
            captured["workflows"] = list(kwargs.get("workflows", []))
            captured["activities"] = list(kwargs.get("activities", []))
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client, handler=DefaultHandler())

        from application_sdk.execution._temporal.sdr import SDR_WORKFLOWS

        for sdr_wf in SDR_WORKFLOWS:
            assert sdr_wf in captured["workflows"]
        activity_names = {
            getattr(a, "__temporal_activity_definition").name  # type: ignore[union-attr]
            for a in captured["activities"]
            if hasattr(a, "__temporal_activity_definition")
        }
        assert {
            "sdr:test_auth",
            "sdr:preflight_check",
            "sdr:fetch_metadata",
        }.issubset(activity_names)

    def test_sdr_opt_out_via_enable_sdr_flag(self) -> None:
        """``enable_sdr=False`` suppresses SDR even when a handler is provided."""

        from application_sdk.handler.base import DefaultHandler

        class _OptOutApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        captured: dict = {}

        def capture_worker(*args, **kwargs):
            captured["workflows"] = list(kwargs.get("workflows", []))
            captured["activities"] = list(kwargs.get("activities", []))
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client, handler=DefaultHandler(), enable_sdr=False)

        from application_sdk.execution._temporal.sdr import SDR_WORKFLOWS

        for sdr_wf in SDR_WORKFLOWS:
            assert sdr_wf not in captured["workflows"]

    def test_passthrough_modules_included_in_sandbox(self) -> None:
        class _SandboxApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        sandbox_configs: list = []

        def capture_worker(*args, **kwargs):
            runner = kwargs.get("workflow_runner")
            if runner is not None:
                sandbox_configs.append(runner)
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client, passthrough_modules={"my_custom_module"})

        assert len(sandbox_configs) == 1


class TestAppWorker:
    """Tests for AppWorker wrapper."""

    @pytest.mark.asyncio
    async def test_run_emits_worker_start_event_before_delegating(self) -> None:
        mock_inner = mock.AsyncMock()
        mock_inner.run = mock.AsyncMock(return_value=None)

        app_worker = AppWorker(
            mock_inner,
            start_event_params={
                "task_queue": "test-queue",
                "app_name": "test-app",
                "workflow_count": 1,
                "activity_count": 2,
                "max_concurrent_activities": 100,
                "host": "localhost:7233",
                "namespace": "default",
            },
        )

        with mock.patch(
            "application_sdk.execution._temporal.worker._emit_worker_start_event"
        ) as mock_emit:
            mock_emit.return_value = None
            await app_worker.run()

        mock_emit.assert_called_once()
        mock_inner.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_emit_worker_start_event_suppresses_binding_error(
        self,
    ) -> None:
        """BindingError from missing eventstore binding is suppressed; worker starts normally."""
        from application_sdk.infrastructure.bindings import BindingError

        mock_inner = mock.AsyncMock()
        mock_inner.run = mock.AsyncMock(return_value=None)

        app_worker = AppWorker(
            mock_inner,
            start_event_params={
                "task_queue": "test-queue",
                "app_name": "test-app",
                "workflow_count": 1,
                "activity_count": 2,
                "max_concurrent_activities": 100,
                "host": "localhost:7233",
                "namespace": "default",
            },
        )

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
            new=mock.AsyncMock(side_effect=BindingError("binding not found")),
        ):
            # Should NOT raise — BindingError is caught inside _emit_worker_start_event
            await app_worker.run()

        mock_inner.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_emit_worker_start_event_suppresses_unexpected_errors(
        self,
    ) -> None:
        """All exceptions from event emission are suppressed; worker starts normally."""
        mock_inner = mock.AsyncMock()
        mock_inner.run = mock.AsyncMock(return_value=None)

        app_worker = AppWorker(
            mock_inner,
            start_event_params={
                "task_queue": "test-queue",
                "app_name": "test-app",
                "workflow_count": 1,
                "activity_count": 2,
                "max_concurrent_activities": 100,
                "host": "localhost:7233",
                "namespace": "default",
            },
        )

        with mock.patch(
            "application_sdk.execution._temporal.interceptors.events._publish_event_via_binding",
            new=mock.AsyncMock(side_effect=RuntimeError("unexpected")),
        ):
            # Should NOT raise — event emission is never on the critical path
            await app_worker.run()

        mock_inner.run.assert_called_once()

    @pytest.mark.asyncio
    async def test_aenter_emits_event_and_returns_inner_worker(self) -> None:
        mock_inner = mock.AsyncMock()
        mock_inner.__aenter__ = mock.AsyncMock(return_value=mock_inner)
        mock_inner.__aexit__ = mock.AsyncMock(return_value=None)

        app_worker = AppWorker(
            mock_inner,
            start_event_params={
                "task_queue": "test-queue",
                "app_name": "test-app",
                "workflow_count": 0,
                "activity_count": 0,
                "max_concurrent_activities": 100,
            },
        )

        with mock.patch(
            "application_sdk.execution._temporal.worker._emit_worker_start_event"
        ) as mock_emit:
            mock_emit.return_value = None
            result = await app_worker.__aenter__()

        mock_emit.assert_called_once()
        assert result is mock_inner


class TestShutdownDrainDelay:
    """Tests for the drain delay that prevents SIGTERM from preempting
    in-flight activity completion RPCs.

    Reproduces the real-world deadlock observed on 2026-03-27: a
    save_workflow_run_state activity failed (Atlas 100K char limit),
    SIGTERM arrived 3 seconds later, and the shutdown task preempted
    the SDK's _run_activity coroutine before it could call
    complete_activity_task(). The worker then held a phantom "in-use"
    task slot for the entire 12-hour graceful_shutdown_timeout.

    The drain delay (asyncio.sleep before worker.__aexit__) yields the
    event loop so the pending complete_activity_task() can flush.
    """

    @staticmethod
    def _make_app_worker(inner: mock.AsyncMock) -> AppWorker:
        return AppWorker(inner, start_event_params=_MINIMAL_START_PARAMS)

    @pytest.mark.asyncio
    async def test_without_drain_delay_activity_completion_preempted(self) -> None:
        """WITHOUT the drain delay, shutdown preempts the activity completion
        — reproducing the production deadlock.

        drain_delay=0 means asyncio.sleep(0) yields only once; the in-flight
        activity task gets one event-loop turn to start its own sleep(0) but
        does not complete before __aexit__ calls the inner worker shutdown.
        """
        inner = mock.AsyncMock()
        inner.__aexit__ = mock.AsyncMock(return_value=None)
        app_worker = self._make_app_worker(inner)

        activity_completed = False

        async def inflight_activity() -> None:
            nonlocal activity_completed
            await asyncio.sleep(0)
            activity_completed = True

        asyncio.create_task(inflight_activity())

        with mock.patch(DRAIN_DELAY_PATCH, 0):
            await app_worker.__aexit__(None, None, None)

        # PROVES THE BUG: activity completion never ran before shutdown
        assert activity_completed is False
        inner.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_with_drain_delay_activity_completes_before_shutdown(self) -> None:
        """WITH the drain delay, the activity completion runs before
        shutdown — the fix works.

        Any drain_delay > 0 yields the event loop long enough for the
        pending activity completion task to execute before the inner
        worker's __aexit__ is called.
        """
        inner = mock.AsyncMock()
        inner.__aexit__ = mock.AsyncMock(return_value=None)
        app_worker = self._make_app_worker(inner)

        activity_completed = False

        async def inflight_activity() -> None:
            nonlocal activity_completed
            await asyncio.sleep(0)
            activity_completed = True

        asyncio.create_task(inflight_activity())

        with mock.patch(DRAIN_DELAY_PATCH, 0.01):
            await app_worker.__aexit__(None, None, None)

        # PROVES THE FIX: activity completion ran before shutdown
        assert activity_completed is True
        inner.__aexit__.assert_called_once()

    @pytest.mark.asyncio
    async def test_drain_delay_flushes_multiple_pending_completions(self) -> None:
        """The drain delay flushes multiple pending activity completions,
        not just one."""
        inner = mock.AsyncMock()
        inner.__aexit__ = mock.AsyncMock(return_value=None)
        app_worker = self._make_app_worker(inner)

        completions: list[str] = []

        async def inflight_activity(activity_id: str) -> None:
            await asyncio.sleep(0)
            completions.append(activity_id)

        asyncio.create_task(inflight_activity("activity_1"))
        asyncio.create_task(inflight_activity("activity_2"))
        asyncio.create_task(inflight_activity("activity_3"))

        with mock.patch(DRAIN_DELAY_PATCH, 0.01):
            await app_worker.__aexit__(None, None, None)

        assert set(completions) == {"activity_1", "activity_2", "activity_3"}

    @pytest.mark.asyncio
    async def test_aexit_completes_even_with_zero_delay(self) -> None:
        """Shutdown doesn't hang even when drain delay is 0 and there are
        no pending completions."""
        inner = mock.AsyncMock()
        inner.__aexit__ = mock.AsyncMock(return_value=None)
        app_worker = self._make_app_worker(inner)

        with mock.patch(DRAIN_DELAY_PATCH, 0):
            await app_worker.__aexit__(None, None, None)

        inner.__aexit__.assert_called_once()
