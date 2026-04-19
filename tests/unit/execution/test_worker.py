"""Unit tests for Temporal worker creation and configuration."""

from __future__ import annotations

from dataclasses import dataclass
from unittest import mock

import pytest

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.worker import AppWorker, create_worker


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

    def test_create_worker_with_all_interceptors_enabled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "true")
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CORRELATION_INTERCEPTOR", "true")
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
        assert "CorrelationContextInterceptor" in interceptor_types
        assert "EventInterceptor" in interceptor_types
        # CleanupInterceptor is no longer registered — cleanup is via App.on_complete()
        assert "CleanupInterceptor" not in interceptor_types
        assert "TaskFailureLoggingInterceptor" in interceptor_types

    def test_event_interceptor_disabled_via_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_EVENT_INTERCEPTOR", "false")
        monkeypatch.setenv("APPLICATION_SDK_ENABLE_CORRELATION_INTERCEPTOR", "true")
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
        # Others should still be present
        assert "TaskFailureLoggingInterceptor" in interceptor_types

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
