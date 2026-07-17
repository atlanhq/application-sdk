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
from application_sdk.execution._temporal._activity_errors import (
    WorkerActivityNameCollisionError,
    WorkerInterceptorDuplicateError,
)
from application_sdk.execution._temporal.worker import (
    AppWorker,
    _resolve_gate_enforcement,
    create_worker,
)

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

    def test_gate_registration_deduped_across_versions(self) -> None:
        """An app registered under multiple versions must register the gate
        activity ONCE — list_all() returns one entry per version, and two
        activities named {app}:preflight crash the worker at boot."""

        class _MultiVersionApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        meta = AppRegistry.get_instance().list_all()[0]
        AppRegistry.get_instance().register(
            name=meta.name,
            version="9.9.9",
            app_cls=meta.app_cls,
            input_type=meta.input_type,
            output_type=meta.output_type,
        )
        assert len(AppRegistry.get_instance().list_all()) == 2

        client = _make_mock_client()
        captured: dict = {}

        def capture_worker(*args, **kwargs):
            captured["activities"] = list(kwargs.get("activities", []))
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client)

        gate_names = [
            getattr(a, "__temporal_activity_definition").name
            for a in captured["activities"]
            if hasattr(a, "__temporal_activity_definition")
            and getattr(a, "__temporal_activity_definition").name.endswith(":preflight")
        ]
        assert gate_names == list(dict.fromkeys(gate_names))  # no duplicate names
        assert len(gate_names) == 1

    def test_task_named_preflight_collides_with_gate(self) -> None:
        """A bare @task named `preflight` registers as {app}:preflight, colliding
        with the injected gate. create_worker must fail with a descriptive error,
        not the opaque temporalio duplicate-activity ValueError."""

        class _CollidingApp(App):
            @task(timeout_seconds=60)
            async def preflight(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        with pytest.raises(WorkerActivityNameCollisionError) as excinfo:
            create_worker(client)
        assert "preflight" in str(excinfo.value)

    def test_rejects_caller_supplied_log_interceptor(self) -> None:
        """``create_worker(interceptors=[LogInterceptor()])`` must fail loudly:
        the SDK adds the observability trio automatically and a duplicate would
        double-count metrics and emit duplicate lifecycle log lines."""
        from application_sdk.execution._temporal.interceptors.log import LogInterceptor

        class _DupApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        with pytest.raises(WorkerInterceptorDuplicateError):
            create_worker(client, interceptors=[LogInterceptor()])

    def test_rejects_caller_supplied_metrics_interceptor(self) -> None:
        from application_sdk.execution._temporal.interceptors.metrics import (
            MetricsInterceptor,
        )

        class _DupApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        with pytest.raises(WorkerInterceptorDuplicateError):
            create_worker(client, interceptors=[MetricsInterceptor()])

    def test_rejects_caller_supplied_trace_interceptor(self) -> None:
        from application_sdk.execution._temporal.interceptors.trace import (
            TraceInterceptor,
        )

        class _DupApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        with pytest.raises(WorkerInterceptorDuplicateError):
            create_worker(client, interceptors=[TraceInterceptor()])

    # ── max_concurrent_workflow_tasks (BLDX-1282) ─────────────────────────

    def test_max_concurrent_workflow_tasks_forwarded_when_set(self) -> None:
        """When set, max_concurrent_workflow_tasks reaches Temporal's Worker(...)."""

        class _MCWTApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        captured: dict = {}

        def capture_worker(*args, **kwargs):
            captured.update(kwargs)
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client, max_concurrent_workflow_tasks=5)

        assert captured["max_concurrent_workflow_tasks"] == 5

    def test_max_concurrent_workflow_tasks_omitted_when_none(self) -> None:
        """When None (default), the kwarg is NOT forwarded — leaves Temporal's default in effect."""

        class _MCWTDefaultApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        captured: dict = {}

        def capture_worker(*args, **kwargs):
            captured.update(kwargs)
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client)

        # Critical: passing None would override Temporal's default with None
        # and break worker construction. The param must be absent entirely.
        assert "max_concurrent_workflow_tasks" not in captured

    def test_max_concurrent_workflow_tasks_in_start_event_params(self) -> None:
        """The configured value lands in worker_start observability params."""

        class _MCWTObsApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            return_value=mock.MagicMock(),
        ):
            app_worker = create_worker(client, max_concurrent_workflow_tasks=3)

        assert app_worker._start_event_params["max_concurrent_workflow_tasks"] == 3

    def test_gate_registered_but_sdr_skipped_when_no_handler(
        self,
    ) -> None:
        """With no app Handler: the mandatory gate ({app}:preflight) is still
        registered (bound to DefaultHandler, no-op safe), but SDR is NOT — binding
        DefaultHandler to sdr:test_auth would fake a green auth check for an app
        that implements none. SDR exposes only capabilities the app actually has."""

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

        activity_names = {
            getattr(a, "__temporal_activity_definition").name  # type: ignore[union-attr]
            for a in captured["activities"]
            if hasattr(a, "__temporal_activity_definition")
        }
        # Gate is always registered.
        assert any(n.endswith(":preflight") for n in activity_names)
        # SDR is skipped entirely — no workflows, no sdr:* activities.
        for sdr_wf in SDR_WORKFLOWS:
            assert sdr_wf not in captured["workflows"]
        assert not any(n.startswith("sdr:") for n in activity_names)

    def test_sdr_workflows_registered_when_real_handler_provided(self) -> None:
        """When a REAL handler is provided (a DefaultHandler subclass counts),
        SDR workflows + activities are appended."""

        from application_sdk.handler.base import DefaultHandler

        class _RealHandler(DefaultHandler):
            """A real handler — not the bare DefaultHandler sentinel."""

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
            create_worker(client, handler=_RealHandler())

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

    def test_sdr_skipped_for_bare_default_handler(self) -> None:
        """Combined mode passes a bare DefaultHandler() (to also serve HTTP) for a
        handler-less app. SDR must still be skipped — binding it would fake a green
        sdr:test_auth — while the mandatory gate is registered."""

        from application_sdk.handler.base import DefaultHandler

        class _BareApp(App):
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

        activity_names = {
            getattr(a, "__temporal_activity_definition").name  # type: ignore[union-attr]
            for a in captured["activities"]
            if hasattr(a, "__temporal_activity_definition")
        }
        assert any(n.endswith(":preflight") for n in activity_names)  # gate present
        for sdr_wf in SDR_WORKFLOWS:
            assert sdr_wf not in captured["workflows"]
        assert not any(n.startswith("sdr:") for n in activity_names)

    def test_sdr_opt_out_via_enable_sdr_flag(self) -> None:
        """``enable_sdr=False`` suppresses SDR even when a real handler is provided."""

        from application_sdk.handler.base import DefaultHandler

        class _RealHandler(DefaultHandler):
            """A real handler — so the skip is attributable to enable_sdr=False."""

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
            create_worker(client, handler=_RealHandler(), enable_sdr=False)

        from application_sdk.execution._temporal.sdr import SDR_WORKFLOWS

        for sdr_wf in SDR_WORKFLOWS:
            assert sdr_wf not in captured["workflows"]
        activity_names = {
            getattr(a, "__temporal_activity_definition").name  # type: ignore[union-attr]
            for a in captured["activities"]
            if hasattr(a, "__temporal_activity_definition")
        }
        # SDR activities suppressed, but the mandatory preflight gate is a core
        # lifecycle activity — it must register regardless of the SDR opt-out.
        assert not any(n.startswith("sdr:") for n in activity_names)
        assert any(n.endswith(":preflight") for n in activity_names)

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


class TestLivenessInterceptorWiring:
    """create_worker wires the LivenessInterceptor only when on_activity given."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    @staticmethod
    def _interceptor_types(on_activity) -> list[str]:
        class _LivenessApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        client = _make_mock_client()
        captured: list = []

        def capture_worker(*args, **kwargs):
            captured.extend(kwargs.get("interceptors", []))
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(client, on_activity=on_activity)

        return [type(i).__name__ for i in captured]

    def test_liveness_interceptor_registered_when_callback_supplied(self) -> None:
        types = self._interceptor_types(lambda: None)
        assert "LivenessInterceptor" in types

    def test_no_liveness_interceptor_without_callback(self) -> None:
        types = self._interceptor_types(None)
        assert "LivenessInterceptor" not in types


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


class TestWorkerPoolQueueResolution:
    """ADR-0016 §3: startup pool→queue diagnostic log emitted by create_worker."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _register_pooled_app(self, pool: str) -> None:
        class _PoolApp(App):
            @task(pool=pool)
            async def heavy_work(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

    def _pool_map_from_info_calls(self, mock_logger: mock.MagicMock) -> dict | None:
        """Extract the pool-queue dict from logger.info('Pool queue map: %s', ...)."""
        for call in mock_logger.info.call_args_list:
            if call.args and "Pool queue map" in str(call.args[0]):
                return call.args[1]
        return None

    def test_explicit_env_var_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit ATLAN_POOL_HEAVY_QUEUE value appears in the startup info log."""
        monkeypatch.setenv("ATLAN_POOL_HEAVY_QUEUE", "dedicated-heavy")
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        self._register_pooled_app("heavy")
        client = _make_mock_client()
        mock_logger = mock.MagicMock()
        with (
            mock.patch(
                "application_sdk.execution._temporal.worker.Worker"
            ) as MockWorker,
            mock.patch(
                "application_sdk.execution._temporal.worker.logger", mock_logger
            ),
        ):
            MockWorker.return_value = mock.MagicMock()
            create_worker(client)
        pool_map = self._pool_map_from_info_calls(mock_logger)
        assert pool_map is not None
        assert pool_map.get("heavy") == "dedicated-heavy"

    def test_derived_queue_logged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When no explicit override is set, derived queue appears in startup info log."""
        monkeypatch.delenv("ATLAN_POOL_HEAVY_QUEUE", raising=False)
        monkeypatch.setenv("ATLAN_TASK_QUEUE", "base-queue")
        self._register_pooled_app("heavy")
        client = _make_mock_client()
        mock_logger = mock.MagicMock()
        with (
            mock.patch(
                "application_sdk.execution._temporal.worker.Worker"
            ) as MockWorker,
            mock.patch(
                "application_sdk.execution._temporal.worker.logger", mock_logger
            ),
        ):
            MockWorker.return_value = mock.MagicMock()
            create_worker(client)
        pool_map = self._pool_map_from_info_calls(mock_logger)
        assert pool_map is not None
        assert pool_map.get("heavy") == "base-queue-heavy"

    def test_no_queue_warns(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When neither env var is set, a warning naming the pool is emitted."""
        monkeypatch.delenv("ATLAN_POOL_HEAVY_QUEUE", raising=False)
        monkeypatch.delenv("ATLAN_TASK_QUEUE", raising=False)
        self._register_pooled_app("heavy")
        client = _make_mock_client()
        mock_logger = mock.MagicMock()
        with (
            mock.patch(
                "application_sdk.execution._temporal.worker.Worker"
            ) as MockWorker,
            mock.patch(
                "application_sdk.execution._temporal.worker.logger", mock_logger
            ),
        ):
            MockWorker.return_value = mock.MagicMock()
            create_worker(client)
        pool_warn = next(
            (
                c
                for c in mock_logger.warning.call_args_list
                if c.args and "no resolvable queue" in str(c.args[0])
            ),
            None,
        )
        assert pool_warn is not None
        assert pool_warn.args[1] == "heavy"


class TestResolveGateEnforcement:
    """Gate posture resolution: env > App.preflight_gate_mode > hard default.

    Only the literal "soft" softens; anything unknown fails safe to hard so
    the safety net is never dropped by a typo.
    """

    def test_default_hard_when_nothing_declared(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _Plain(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Plain) is True
        assert _resolve_gate_enforcement(None) is True

    def test_declared_soft_softens(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _Soft(App):
            preflight_gate_mode = "soft"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Soft) is False

    def test_declared_value_case_and_whitespace_insensitive(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _Loud(App):
            preflight_gate_mode = "  SOFT  "

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Loud) is False

    def test_malformed_declared_fails_safe_to_hard(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _Typo(App):
            preflight_gate_mode = "off"  # not "soft" -> hard

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Typo) is True

    def test_env_soft_wins_over_declared_hard(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_PREFLIGHT_GATE_MODE", "soft")

        class _Hard(App):
            preflight_gate_mode = "hard"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Hard) is False

    def test_env_hard_wins_over_declared_soft(self, monkeypatch) -> None:
        # ops can force the net back up without waiting on an app release
        monkeypatch.setenv("ATLAN_PREFLIGHT_GATE_MODE", "hard")

        class _Soft(App):
            preflight_gate_mode = "soft"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Soft) is True

    def test_malformed_env_fails_safe_to_hard(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_PREFLIGHT_GATE_MODE", "disabled")

        class _Soft(App):
            preflight_gate_mode = "soft"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        # a set-but-unknown env value decides (fail safe), it does not fall through
        assert _resolve_gate_enforcement(_Soft) is True
