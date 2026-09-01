"""Unit tests for Temporal worker creation and configuration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from unittest import mock

import pydantic
import pytest
from temporalio.exceptions import ActivityError

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.constants import SHUTDOWN_DRAIN_DELAY_SECONDS
from application_sdk.contracts.base import Input, Output
from application_sdk.errors.leaves import (
    AppTimeoutError,
    DependencyUnavailableError,
    RateLimitedError,
)
from application_sdk.execution._temporal import preflight_gate as _preflight_gate_module
from application_sdk.execution._temporal._activity_errors import (
    WorkerActivityNameCollisionError,
    WorkerInterceptorDuplicateError,
)
from application_sdk.execution._temporal.worker import (
    _MAX_FATAL_CHAIN_DEPTH,
    AppWorker,
    _log_worker_fatal_error,
    _resolve_gate_enforcement,
    _resolve_verify_storage,
    create_worker,
    describe_exception_chain,
    read_core_poller_counts,
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


def _WorkerWrapperForDrain(*, shutdown_drain_delay_seconds: float = 0.0):
    """A real ``AppWorker`` with its worker and pusher stubbed, so the test drives
    the production ``__aexit__`` — a fake would pass while shutdown stayed broken.

    The drain delay defaults to zero here. These tests are about what
    ``__aexit__`` flushes, not about the yield that precedes it, and the
    production default of five seconds is five real seconds of sleep per test
    (FND-962). The delay itself is asserted by
    ``TestShutdownDrainDelay`` below, which is the only place that cares.
    """
    from application_sdk.execution._temporal.worker import AppWorker

    w = object.__new__(AppWorker)

    class _NullWorker:
        async def __aexit__(self, *a: object) -> None:
            return None

    w._worker = _NullWorker()
    w._pusher = None
    w._start_event_params = {}
    w._shutdown_drain_delay_seconds = shutdown_drain_delay_seconds
    return w


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

    def test_hard_app_registers_gate_and_logs_boot_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An app declaring ``preflight_gate_mode = "hard"`` drives the
        ``create_worker`` glue end to end: the ``name -> app_cls`` map resolves
        ``enforce=True``, the gate activity registers under ``{app}:preflight``,
        and the boot INFO line fires so the hard posture is visible in logs."""
        # _resolve_gate_enforcement reads env first, so a non-"hard" ambient
        # value would resolve this app to soft and suppress the boot line —
        # clear it to isolate the test from the environment (sibling pattern).
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _HardGateApp(App):
            preflight_gate_mode = "hard"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        app_name = AppRegistry.get_instance().list_all()[0].name

        client = _make_mock_client()
        captured: dict = {}

        def capture_worker(*args, **kwargs):
            captured["activities"] = list(kwargs.get("activities", []))
            return mock.MagicMock()

        with (
            mock.patch(
                "application_sdk.execution._temporal.worker.Worker",
                side_effect=capture_worker,
            ),
            mock.patch(
                "application_sdk.execution._temporal.worker.logger"
            ) as mock_logger,
        ):
            create_worker(client)

        gate_names = [
            getattr(a, "__temporal_activity_definition").name
            for a in captured["activities"]
            if hasattr(a, "__temporal_activity_definition")
            and getattr(a, "__temporal_activity_definition").name.endswith(":preflight")
        ]
        assert f"{app_name}:preflight" in gate_names

        hard_boot_calls = [
            call
            for call in mock_logger.info.call_args_list
            if call.args and "HARD" in call.args[0] and app_name in call.args
        ]
        assert len(hard_boot_calls) == 1

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

    # ── sizing telemetry wiring ───────────────────────────────────────────

    def _interceptors_for(self, monkeypatch, **env) -> list:
        """Return the interceptor list ``create_worker`` hands to Temporal."""

        class _SizingApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        for key in (
            "APPLICATION_SDK_ENABLE_SIZING_TELEMETRY",
            "APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES",
        ):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)

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
        return list(captured.get("interceptors") or [])

    def _has_sizing(self, interceptors: list) -> bool:
        return any(
            type(i).__name__ == "SizingTelemetryInterceptor" for i in interceptors
        )

    def test_sizing_interceptor_absent_by_default(self, monkeypatch) -> None:
        """A version bump alone must not start measuring anything."""
        assert self._has_sizing(self._interceptors_for(monkeypatch)) is False

    def test_sizing_interceptor_absent_when_enabled_with_no_allow_list(
        self, monkeypatch
    ) -> None:
        """Enabled but unnamed collects nothing — and is not even attached."""
        interceptors = self._interceptors_for(
            monkeypatch, APPLICATION_SDK_ENABLE_SIZING_TELEMETRY="true"
        )
        assert self._has_sizing(interceptors) is False

    def test_sizing_interceptor_attached_for_named_activities(
        self, monkeypatch
    ) -> None:
        interceptors = self._interceptors_for(
            monkeypatch,
            APPLICATION_SDK_ENABLE_SIZING_TELEMETRY="true",
            APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES="merge,fetch_entities",
        )
        sizing = [
            i for i in interceptors if type(i).__name__ == "SizingTelemetryInterceptor"
        ]
        assert len(sizing) == 1
        assert sizing[0]._activities == frozenset({"merge", "fetch_entities"})

    # ── sizing drain on shutdown ──────────────────────────────────────────

    @pytest.mark.asyncio
    async def test_shutdown_drains_buffered_sizing_rows(self, monkeypatch) -> None:
        """The last batch of a pod's life must not die with the process: these pools
        scale to zero, so the tail of every pod would be lost.
        """
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_SIZING_TELEMETRY", "true")
        monkeypatch.setenv("APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES", "merge")

        drained = []

        async def fake_drain() -> None:
            drained.append(True)

        monkeypatch.setattr(
            "application_sdk.observability.sizing_sink.drain", fake_drain
        )

        wrapper = _WorkerWrapperForDrain()
        await wrapper.__aexit__(None)
        assert drained == [True], "shutdown did not drain the sizing sink"

    @pytest.mark.asyncio
    async def test_shutdown_skips_drain_when_collection_is_off(
        self, monkeypatch
    ) -> None:
        """The default path must not import or touch the sink at all."""
        monkeypatch.delenv("APPLICATION_SDK_ENABLE_SIZING_TELEMETRY", raising=False)

        drained = []

        async def fake_drain() -> None:
            drained.append(True)

        monkeypatch.setattr(
            "application_sdk.observability.sizing_sink.drain", fake_drain
        )

        wrapper = _WorkerWrapperForDrain()
        await wrapper.__aexit__(None)
        assert drained == []

    @pytest.mark.asyncio
    async def test_a_failing_drain_does_not_break_shutdown(self, monkeypatch) -> None:
        """Telemetry must never hold up or fail a shutdown."""
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_SIZING_TELEMETRY", "true")
        monkeypatch.setenv("APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES", "merge")

        async def boom() -> None:
            raise RuntimeError("object store unreachable")

        monkeypatch.setattr("application_sdk.observability.sizing_sink.drain", boom)

        wrapper = _WorkerWrapperForDrain()
        await wrapper.__aexit__(None)  # must not raise

    def test_sizing_interceptor_absent_when_list_set_but_switch_off(
        self, monkeypatch
    ) -> None:
        """The master switch wins, so collection stops without editing lists."""
        interceptors = self._interceptors_for(
            monkeypatch, APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES="merge"
        )
        assert self._has_sizing(interceptors) is False

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
    def _make_app_worker(inner: mock.AsyncMock, *, drain_delay: float) -> AppWorker:
        """Build the wrapper with the delay under test.

        Set through the constructor rather than by patching
        ``SHUTDOWN_DRAIN_DELAY_SECONDS``: the delay is a parameter
        ``AppWorker`` binds at construction (FND-962), so a patch of the module
        constant would leave every one of these tests running on the production
        five-second default.
        """
        return AppWorker(
            inner,
            start_event_params=_MINIMAL_START_PARAMS,
            shutdown_drain_delay_seconds=drain_delay,
        )

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
        app_worker = self._make_app_worker(inner, drain_delay=0)

        activity_completed = False

        async def inflight_activity() -> None:
            nonlocal activity_completed
            await asyncio.sleep(0)
            activity_completed = True

        asyncio.create_task(inflight_activity())

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
        app_worker = self._make_app_worker(inner, drain_delay=0.01)

        activity_completed = False

        async def inflight_activity() -> None:
            nonlocal activity_completed
            await asyncio.sleep(0)
            activity_completed = True

        asyncio.create_task(inflight_activity())

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
        app_worker = self._make_app_worker(inner, drain_delay=0.01)

        completions: list[str] = []

        async def inflight_activity(activity_id: str) -> None:
            await asyncio.sleep(0)
            completions.append(activity_id)

        asyncio.create_task(inflight_activity("activity_1"))
        asyncio.create_task(inflight_activity("activity_2"))
        asyncio.create_task(inflight_activity("activity_3"))

        await app_worker.__aexit__(None, None, None)

        assert set(completions) == {"activity_1", "activity_2", "activity_3"}

    @pytest.mark.asyncio
    async def test_aexit_completes_even_with_zero_delay(self) -> None:
        """Shutdown doesn't hang even when drain delay is 0 and there are
        no pending completions."""
        inner = mock.AsyncMock()
        inner.__aexit__ = mock.AsyncMock(return_value=None)
        app_worker = self._make_app_worker(inner, drain_delay=0)

        await app_worker.__aexit__(None, None, None)

        inner.__aexit__.assert_called_once()

    def test_the_default_delay_is_the_configured_one(self) -> None:
        """Every test above sets the delay, so nothing else pins what a worker
        built by ``create_worker`` actually waits. Without this, dropping the
        default to zero would be invisible — and the deadlock this whole class
        documents would be back with a green suite.
        """
        app_worker = AppWorker(
            mock.AsyncMock(), start_event_params=_MINIMAL_START_PARAMS
        )
        assert app_worker._shutdown_drain_delay_seconds == SHUTDOWN_DRAIN_DELAY_SECONDS
        assert SHUTDOWN_DRAIN_DELAY_SECONDS > 0


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
    """Gate posture resolution: env > App.preflight_gate_mode > soft default.

    Only the literal "hard" enforces; anything unknown falls back to soft so
    a run is never blocked by a typo — blocking is always a deliberate opt-in.
    """

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_default_soft_when_nothing_declared(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _Plain(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Plain) is False
        assert _resolve_gate_enforcement(None) is False

    def test_declared_hard_enforces(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _Hard(App):
            preflight_gate_mode = "hard"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Hard) is True

    def test_declared_value_case_and_whitespace_insensitive(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _Loud(App):
            preflight_gate_mode = "  HARD  "

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Loud) is True

    def test_malformed_declared_falls_back_to_soft(self, monkeypatch) -> None:
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)

        class _Typo(App):
            preflight_gate_mode = "on"  # not "hard" -> soft

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Typo) is False

    def test_env_hard_wins_over_declared_soft(self, monkeypatch) -> None:
        # ops can force the net up without waiting on an app release
        monkeypatch.setenv("ATLAN_PREFLIGHT_GATE_MODE", "hard")

        class _Soft(App):
            preflight_gate_mode = "soft"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Soft) is True

    def test_env_soft_wins_over_declared_hard(self, monkeypatch) -> None:
        # ops can drop the net without waiting on an app release
        monkeypatch.setenv("ATLAN_PREFLIGHT_GATE_MODE", "soft")

        class _Hard(App):
            preflight_gate_mode = "hard"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Hard) is False

    def test_malformed_env_falls_back_to_soft(self, monkeypatch) -> None:
        monkeypatch.setenv("ATLAN_PREFLIGHT_GATE_MODE", "enabled")

        class _Hard(App):
            preflight_gate_mode = "hard"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        # a set-but-unknown env value decides (falls back to soft), it does not
        # fall through to the declared attribute
        assert _resolve_gate_enforcement(_Hard) is False

    def test_empty_env_defers_to_declared(self, monkeypatch) -> None:
        # An empty env value (a blank ConfigMap entry) is falsy under `if val:`,
        # so it is not treated as an override — resolution falls through to the
        # declared attribute rather than forcing soft.
        monkeypatch.setenv("ATLAN_PREFLIGHT_GATE_MODE", "")

        class _Hard(App):
            preflight_gate_mode = "hard"

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_gate_enforcement(_Hard) is True


class TestWorkflowFailureExceptionTypes:
    """Pre-user-code failures must fail the run, not retry the task forever."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    @staticmethod
    def _declared_types() -> tuple[type[BaseException], ...]:
        class _FailureTypesApp(App):
            @task(timeout_seconds=60)
            async def do_work(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        captured: dict = {}

        def capture_worker(*args, **kwargs):
            captured.update(kwargs)
            return mock.MagicMock()

        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker",
            side_effect=capture_worker,
        ):
            create_worker(_make_mock_client())

        return tuple(captured["workflow_failure_exception_types"])

    def test_undecodable_input_fails_the_run_terminally(self) -> None:
        assert issubclass(pydantic.ValidationError, self._declared_types())

    def test_retryable_errors_are_not_declared(self) -> None:
        declared = self._declared_types()
        for retryable in (
            RateLimitedError,
            DependencyUnavailableError,
            AppTimeoutError,
        ):
            assert not issubclass(retryable, declared), (
                f"{retryable.__name__} has default_retryable=True and must stay "
                "retryable; workflow_failure_exception_types is matched by type "
                "and cannot consult effective_retryable"
            )

    def test_cancellation_is_not_declared(self) -> None:
        declared = self._declared_types()
        assert not issubclass(asyncio.CancelledError, declared)
        assert not issubclass(ActivityError, declared)


class TestDescribeExceptionChain:
    """Tests for ``describe_exception_chain`` (ARUN-1127 instrumentation)."""

    def test_renders_the_real_shape_of_a_temporal_poll_fatal(self) -> None:
        """The gRPC status only exists below the wrapper, so the chain must be walked.

        This is the exact shape temporalio produces: the Rust bridge raises
        ``RuntimeError("Poll failure: ... Status { code: PermissionDenied ... }")``
        and ``temporalio.worker`` re-wraps it as ``RuntimeError("Activity worker
        failed") from err``. ``str(exc)`` alone therefore carries no diagnosis at
        all — which is why classifying on it cannot work.
        """
        bridge_error = RuntimeError(
            "Poll failure: Unhandled grpc error when polling: "
            'Status { code: PermissionDenied, message: "Request unauthorized." }'
        )
        try:
            try:
                raise bridge_error
            except RuntimeError as err:
                raise RuntimeError("Activity worker failed") from err
        except RuntimeError as top:
            chain = describe_exception_chain(top)

        assert chain[0] == "RuntimeError: Activity worker failed"
        assert "PermissionDenied" in chain[1]
        assert "PermissionDenied" not in chain[0]

    def test_follows_context_when_there_is_no_explicit_cause(self) -> None:
        try:
            try:
                raise ValueError("inner")
            except ValueError:
                raise RuntimeError("outer")  # noqa: B904 — implicit chain is the point
        except RuntimeError as top:
            chain = describe_exception_chain(top)

        assert chain == ["RuntimeError: outer", "ValueError: inner"]

    def test_cycle_does_not_hang(self) -> None:
        first = RuntimeError("first")
        second = RuntimeError("second")
        first.__cause__ = second
        second.__cause__ = first

        chain = describe_exception_chain(first)

        assert chain == ["RuntimeError: first", "RuntimeError: second"]

    def test_depth_is_capped(self) -> None:
        head = RuntimeError("link-0")
        current = head
        for index in range(1, 30):
            nxt = RuntimeError(f"link-{index}")
            current.__cause__ = nxt
            current = nxt

        chain = describe_exception_chain(head)

        assert len(chain) == _MAX_FATAL_CHAIN_DEPTH + 1
        assert chain[-1] == "... chain truncated"


class TestLogWorkerFatalError:
    """Tests for the ``on_fatal_error`` diagnostic log."""

    @pytest.mark.asyncio
    async def test_passes_the_exception_object_as_exc_info(self) -> None:
        """``exc_info=True`` renders "NoneType: None" on this path.

        ``Worker.run`` retrieves the fatal with ``task.exception()`` and invokes
        the hook outside any ``except`` block, so ``sys.exc_info()`` is empty and
        only passing the object itself yields a traceback. Pinning the value
        keeps a future edit from quietly reverting to ``True`` and dropping the
        traceback this log exists to capture.
        """
        exc = RuntimeError("Activity worker failed")

        with mock.patch(
            "application_sdk.execution._temporal.worker.logger"
        ) as mock_logger:
            await _log_worker_fatal_error(exc)

        assert mock_logger.error.call_args.kwargs["exc_info"] is exc


class TestReadCorePollerCounts:
    """Tests for ``read_core_poller_counts`` (the in-process poll-liveness read)."""

    _EXPOSITION = (
        "# HELP temporal_num_pollers Current number of pollers\n"
        "# TYPE temporal_num_pollers gauge\n"
        'temporal_num_pollers{poller_type="workflow_task",task_queue="q"} 2.0\n'
        'temporal_num_pollers{poller_type="activity_task",task_queue="q"} 3.0\n'
        "# HELP temporal_request_total Requests\n"
        "# TYPE temporal_request_total counter\n"
        'temporal_request_total{operation="PollActivityTaskQueue"} 41.0\n'
    )

    def _response(
        self,
        status_code: int = 200,
        text: str | None = None,
        content_length: str | None = None,
        force_chunk_size: int | None = None,
    ) -> mock.Mock:
        body = (self._EXPOSITION if text is None else text).encode()
        response = mock.Mock()
        response.status_code = status_code
        headers = {}
        if content_length is not None:
            headers["content-length"] = content_length
        response.headers = headers

        # ``aiter_bytes`` yields the body in chunks so the incremental,
        # byte-capped accumulation path is exercised (not a single bulk read).
        # The implementation passes ``chunk_size=cap``; ``force_chunk_size``
        # overrides it so a test can force many small chunks regardless.
        async def _aiter(chunk_size: int | None = None):
            step = force_chunk_size or chunk_size or len(body) or 1
            for offset in range(0, len(body), step):
                yield body[offset : offset + step]

        response.aiter_bytes = _aiter
        # ``client.stream`` returns an async context manager yielding the response.
        stream_ctx = mock.AsyncMock()
        stream_ctx.__aenter__ = mock.AsyncMock(return_value=response)
        stream_ctx.__aexit__ = mock.AsyncMock(return_value=False)
        response._stream_ctx = stream_ctx
        return response

    def _patch_client(self, response: mock.Mock) -> mock.patch:
        client = mock.AsyncMock()
        client.stream = mock.Mock(return_value=response._stream_ctx)
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)
        return mock.patch("httpx.AsyncClient", return_value=client)

    @pytest.mark.asyncio
    async def test_sums_the_gauge_by_poller_type(self) -> None:
        with self._patch_client(self._response()):
            counts = await read_core_poller_counts()

        assert counts == {"workflow_task": 2.0, "activity_task": 3.0}

    @pytest.mark.asyncio
    async def test_zero_pollers_reads_as_zero_not_unknown(self) -> None:
        """A dead poll loop must be distinguishable from an unreadable endpoint."""
        exposition = (
            "# TYPE temporal_num_pollers gauge\n"
            'temporal_num_pollers{poller_type="workflow_task"} 0.0\n'
        )
        with self._patch_client(self._response(text=exposition)):
            counts = await read_core_poller_counts()

        assert counts == {"workflow_task": 0.0}

    @pytest.mark.asyncio
    async def test_oversize_content_length_is_unknown_not_zero(self) -> None:
        """A declared-oversize exposition is unknown, never a zero count."""
        with self._patch_client(self._response(content_length=str(100 * 1024 * 1024))):
            assert await read_core_poller_counts() is None

    @pytest.mark.asyncio
    async def test_oversize_body_is_unknown_not_zero(self) -> None:
        """An actually-oversize body (no/!accurate Content-Length) is unknown."""
        body = "x" * (1024 * 1024 + 1)
        with self._patch_client(self._response(text=body)):
            assert await read_core_poller_counts() is None

    @pytest.mark.asyncio
    async def test_oversize_chunked_stream_is_bounded(self) -> None:
        """A chunked response with no Content-Length must bail on the running
        byte total, not after an unbounded bulk read of the full body."""
        # 1 MiB + 1 byte, delivered as many small chunks: the cap must trip on
        # the accumulated total, proving the read is genuinely bounded.
        body = "x" * (1024 * 1024 + 1)
        with self._patch_client(self._response(text=body, force_chunk_size=4096)):
            assert await read_core_poller_counts() is None

    @pytest.mark.asyncio
    async def test_unreachable_endpoint_is_unknown_not_zero(self) -> None:
        client = mock.AsyncMock()
        client.stream = mock.Mock(side_effect=OSError("connection refused"))
        client.__aenter__ = mock.AsyncMock(return_value=client)
        client.__aexit__ = mock.AsyncMock(return_value=False)

        with mock.patch("httpx.AsyncClient", return_value=client):
            assert await read_core_poller_counts() is None

    @pytest.mark.asyncio
    async def test_non_200_is_unknown(self) -> None:
        with self._patch_client(self._response(status_code=503)):
            assert await read_core_poller_counts() is None

    @pytest.mark.asyncio
    async def test_absent_gauge_family_is_unknown(self) -> None:
        """Core registers the gauge only once polling starts; absent != zero."""
        exposition = (
            "# TYPE temporal_request_total counter\ntemporal_request_total 1.0\n"
        )
        with self._patch_client(self._response(text=exposition)):
            assert await read_core_poller_counts() is None


class TestArtifactValidationPostureAtBoot:
    """The boot-time denominator for artifact validation (FND-692, ADR-0020).

    Artifact validation rides the activity interceptor rather than a registered
    activity, so the worker's only job for it is this row — and the row has to fire
    for **every** app, because an app whose tasks hand off no artifacts emits no
    outcome row at all and is otherwise indistinguishable from one that never
    registered.
    """

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    @staticmethod
    def _posture_rows(logger: mock.MagicMock) -> list[dict]:
        from application_sdk.observability.events import (
            ARTIFACT_VALIDATION_POSTURE_EVENT,
        )

        return [
            call.kwargs
            for call in logger.info.call_args_list
            if call.args and call.args[0] == ARTIFACT_VALIDATION_POSTURE_EVENT
        ]

    def _build(self, monkeypatch: pytest.MonkeyPatch) -> list[dict]:
        from application_sdk.validation import interceptor as interceptor_module

        client = _make_mock_client()
        with mock.patch(
            "application_sdk.execution._temporal.worker.Worker"
        ) as MockWorker:
            MockWorker.return_value = mock.MagicMock()
            with mock.patch.object(interceptor_module, "logger") as logger:
                create_worker(client)
            return self._posture_rows(logger)

    def test_a_soft_app_still_emits_exactly_one_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_ARTIFACT_VALIDATION_MODE", raising=False)

        class _SoftPostureApp(App):
            @task(timeout_seconds=60)
            async def do_work(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        rows = self._build(monkeypatch)
        assert len(rows) == 1
        assert rows[0]["app_name"] == "_soft-posture-app"
        assert rows[0]["artifact_validation_mode"] == "soft"

    def test_a_hard_app_reports_hard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ATLAN_ARTIFACT_VALIDATION_MODE", raising=False)

        class _HardPostureApp(App):
            artifact_validation_mode = "hard"

            @task(timeout_seconds=60)
            async def do_work(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        rows = self._build(monkeypatch)
        assert [r["artifact_validation_mode"] for r in rows] == ["hard"]

    def test_the_env_override_is_what_the_row_reports(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Otherwise the boot row would advertise a posture the deployment had
        already stood down."""
        monkeypatch.setenv("ATLAN_ARTIFACT_VALIDATION_MODE", "soft")

        class _OverriddenPostureApp(App):
            artifact_validation_mode = "hard"

            @task(timeout_seconds=60)
            async def do_work(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        rows = self._build(monkeypatch)
        assert [r["artifact_validation_mode"] for r in rows] == ["soft"]

    def test_the_kill_switch_reports_off_rather_than_a_posture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ATLAN_ARTIFACT_VALIDATION_MODE", raising=False)
        monkeypatch.setattr(
            "application_sdk.constants.VALIDATE_ARTIFACTS", False, raising=False
        )

        class _DisabledPostureApp(App):
            artifact_validation_mode = "hard"

            @task(timeout_seconds=60)
            async def do_work(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        rows = self._build(monkeypatch)
        assert [r["artifact_validation_mode"] for r in rows] == ["off"]

    def test_one_row_per_app_not_per_task(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The posture is a property of the app; emitting it per task would
        multiply the denominator by an app's task count."""
        monkeypatch.delenv("ATLAN_ARTIFACT_VALIDATION_MODE", raising=False)

        class _MultiTaskPostureApp(App):
            @task(timeout_seconds=60)
            async def first(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            @task(timeout_seconds=60)
            async def second(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert len(self._build(monkeypatch)) == 1


class TestPreflightVerifyStorageWiring:
    """``App.preflight_verify_storage`` -> the gate activity's ``verify_storage``.

    The activity-level suite passes ``verify_storage=True`` straight to
    ``build_preflight_gate_activity``, so nothing there exercises the ClassVar or
    the worker glue that reads it: both could regress to permanently-off with
    every existing test still green.
    """

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    @staticmethod
    def _captured_verify_storage(monkeypatch) -> list[bool]:
        """Build a worker, returning the ``verify_storage`` of every gate built."""
        monkeypatch.delenv("ATLAN_PREFLIGHT_GATE_MODE", raising=False)
        seen: list[bool] = []
        real = _preflight_gate_module.build_preflight_gate_activity

        def spy(*args, **kwargs):
            seen.append(kwargs["verify_storage"])
            return real(*args, **kwargs)

        with (
            mock.patch.object(
                _preflight_gate_module, "build_preflight_gate_activity", spy
            ),
            mock.patch("application_sdk.execution._temporal.worker.Worker"),
        ):
            create_worker(_make_mock_client())
        return seen

    def test_declared_true_reaches_the_gate(self, monkeypatch) -> None:
        """The ClassVar is the opt-in: declaring it must switch the gate on."""

        class _VerifyingApp(App):
            preflight_verify_storage = True

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert self._captured_verify_storage(monkeypatch) == [True]

    def test_default_is_off(self, monkeypatch) -> None:
        """An app that declares nothing must not be probing storage."""

        class _PlainApp(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert self._captured_verify_storage(monkeypatch) == [False]

    def test_resolver_handles_an_unresolvable_app(self) -> None:
        """``app_cls`` is ``None`` when no app is SDR-registered.

        ``gate_app_names`` falls back to the resolved service name while
        ``name_to_app_cls`` stays empty, so the gate is still registered with no
        class to read. Reading the ClassVar directly off ``None`` would raise and
        take out worker boot.
        """
        assert _resolve_verify_storage(None) is False

    def test_resolver_reads_the_declared_classvar(self) -> None:
        class _On(App):
            preflight_verify_storage = True

            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        class _Off(App):
            async def run(self, input: _WorkerInput) -> _WorkerOutput:
                return _WorkerOutput()

        assert _resolve_verify_storage(_On) is True
        assert _resolve_verify_storage(_Off) is False
