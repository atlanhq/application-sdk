"""Unit tests for Temporal activity creation from tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from unittest import mock

import pytest

from application_sdk.app.base import App, NonRetryableError
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference

# These tests intentionally import private Temporal helpers because they verify
# the task-to-activity adapter used internally by the SDK.
from application_sdk.execution._temporal import activities as activities_module
from application_sdk.execution._temporal.activities import (
    TaskContext,
    _track_file_refs,
    create_activity_from_task,
    get_activity_options,
    get_all_task_activities,
)
from application_sdk.execution.retry import RetryPolicy as SdkRetryPolicy


# Module-level types so get_type_hints can resolve them
@dataclass
class _ActInput(Input, allow_unbounded_fields=True):
    name: str = "default"


@dataclass
class _ActOutput(Output, allow_unbounded_fields=True):
    greeting: str = ""


@dataclass
class _AppAInput(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _AppAOutput(Output, allow_unbounded_fields=True):
    y: str = ""


@dataclass
class _AppBInput(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _AppBOutput(Output, allow_unbounded_fields=True):
    y: str = ""


@dataclass
class _In1(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _Out1(Output, allow_unbounded_fields=True):
    y: str = ""


@dataclass
class _In2(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _Out2(Output, allow_unbounded_fields=True):
    y: str = ""


@dataclass
class _PrimaryIn(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _PrimaryOut(Output, allow_unbounded_fields=True):
    y: str = ""


@dataclass
class _SecondaryIn(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _SecondaryOut(Output, allow_unbounded_fields=True):
    y: str = ""


@dataclass
class _UnrelatedIn(Input, allow_unbounded_fields=True):
    x: str = ""


@dataclass
class _UnrelatedOut(Output, allow_unbounded_fields=True):
    y: str = ""


class TestCreateActivityFromTask:
    """Tests for create_activity_from_task()."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _make_app_with_task(self) -> type[App]:
        class _GreeterApp(App):
            @task(timeout_seconds=60)
            async def greet(self, input: _ActInput) -> _ActOutput:
                return _ActOutput(greeting=f"Hello {input.name}")

            async def run(self, input: _ActInput) -> _ActOutput:
                return await self.greet(input)

        return _GreeterApp

    def test_returns_callable(self) -> None:
        self._make_app_with_task()
        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_greeter-app")
        assert len(tasks) > 0
        activity_fn = create_activity_from_task(tasks[0])
        assert callable(activity_fn)

    def test_has_activity_defn_name(self) -> None:
        self._make_app_with_task()
        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_greeter-app")
        activity_fn = create_activity_from_task(tasks[0])
        # Temporal sets __temporal_activity_definition on decorated activities
        assert hasattr(activity_fn, "__temporal_activity_definition")

    def test_has_task_metadata_attribute(self) -> None:
        self._make_app_with_task()
        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_greeter-app")
        activity_fn = create_activity_from_task(tasks[0])
        assert hasattr(activity_fn, "_task_metadata")

    def test_annotations_match_task_types(self) -> None:
        self._make_app_with_task()
        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_greeter-app")
        # Find the user-defined greet task (not the framework upload/download tasks)
        greet_task = next(t for t in tasks if t.name == "greet")
        create_activity_from_task(greet_task)
        # Check that input type matches
        assert greet_task.input_type is _ActInput
        assert greet_task.output_type is _ActOutput

    def test_activity_name_is_app_qualified(self) -> None:
        self._make_app_with_task()
        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_greeter-app")
        task_meta = tasks[0]
        activity_fn = create_activity_from_task(task_meta)
        defn = getattr(activity_fn, "__temporal_activity_definition")
        # Activity names are now qualified as '{app_name}:{task_name}'
        assert defn.name == f"{task_meta.app_name}:{task_meta.name}"


class TestGetAllTaskActivities:
    """Tests for get_all_task_activities()."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_returns_empty_when_no_apps(self) -> None:
        activities = get_all_task_activities()
        assert activities == []

    def test_returns_activities_for_registered_tasks(self) -> None:
        class _MultiApp(App):
            @task(timeout_seconds=60)
            async def step_one(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

            @task(timeout_seconds=60)
            async def step_two(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

            async def run(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

        activities = get_all_task_activities()
        # 2 user tasks + 4 framework tasks (upload, download, cleanup_files, cleanup_storage) = 6
        assert len(activities) == 6

    def test_returns_activities_for_all_apps(self) -> None:
        class _AppA(App):
            @task(timeout_seconds=60)
            async def task_a(self, input: _AppAInput) -> _AppAOutput:
                return _AppAOutput()

            async def run(self, input: _AppAInput) -> _AppAOutput:
                return _AppAOutput()

        class _AppB(App):
            @task(timeout_seconds=60)
            async def task_b(self, input: _AppBInput) -> _AppBOutput:
                return _AppBOutput()

            async def run(self, input: _AppBInput) -> _AppBOutput:
                return _AppBOutput()

        activities = get_all_task_activities()
        app_names_used = {
            a._task_metadata.app_name  # type: ignore[attr-defined]
            for a in activities
            if hasattr(a, "_task_metadata")
        }
        assert "_app-a" in app_names_used
        assert "_app-b" in app_names_used

    def test_all_returns_both_apps_tasks(self) -> None:
        class _App1(App):
            @task(timeout_seconds=60)
            async def do_one(self, input: _In1) -> _Out1:
                return _Out1()

            async def run(self, input: _In1) -> _Out1:
                return _Out1()

        class _App2(App):
            @task(timeout_seconds=60)
            async def do_two(self, input: _In2) -> _Out2:
                return _Out2()

            async def run(self, input: _In2) -> _Out2:
                return _Out2()

        activities = get_all_task_activities()
        # 1 user task per app (2) + 4 framework tasks per app (8) = 10
        # (no dedup — each app has its own qualified activity names)
        assert len(activities) == 10
        activity_names = [
            a._task_metadata.name  # type: ignore[attr-defined]
            for a in activities
            if hasattr(a, "_task_metadata")
        ]
        assert "do_one" in activity_names
        assert "do_two" in activity_names


class TestAllAppsActivities:
    """Tests for get_all_task_activities() — includes all registered apps' tasks."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_all_apps_tasks_are_included(self) -> None:
        """Tasks from all registered apps should be included."""

        class _PrimaryApp(App):
            @task(timeout_seconds=60)
            async def primary_task(self, input: _PrimaryIn) -> _PrimaryOut:
                return _PrimaryOut()

            async def run(self, input: _PrimaryIn) -> _PrimaryOut:
                return _PrimaryOut()

        class _UnrelatedApp(App):
            @task(timeout_seconds=60)
            async def unrelated_task(self, input: _UnrelatedIn) -> _UnrelatedOut:
                return _UnrelatedOut()

            async def run(self, input: _UnrelatedIn) -> _UnrelatedOut:
                return _UnrelatedOut()

        activities = get_all_task_activities()
        activity_names = [
            a._task_metadata.name  # type: ignore[attr-defined]
            for a in activities
            if hasattr(a, "_task_metadata")
        ]
        assert "primary_task" in activity_names
        assert "unrelated_task" in activity_names

    def test_multi_app_all_tasks_included(self) -> None:
        """Tasks from all registered apps are included."""

        class _AppPrimary(App):
            @task(timeout_seconds=60)
            async def extract(self, input: _PrimaryIn) -> _PrimaryOut:
                return _PrimaryOut()

            async def run(self, input: _PrimaryIn) -> _PrimaryOut:
                return _PrimaryOut()

        class _AppSecondary(App):
            @task(timeout_seconds=60)
            async def lineage(self, input: _SecondaryIn) -> _SecondaryOut:
                return _SecondaryOut()

            async def run(self, input: _SecondaryIn) -> _SecondaryOut:
                return _SecondaryOut()

        activities = get_all_task_activities()
        activity_names = [
            a._task_metadata.name  # type: ignore[attr-defined]
            for a in activities
            if hasattr(a, "_task_metadata")
        ]
        assert "extract" in activity_names
        assert "lineage" in activity_names

    def test_activity_names_are_app_qualified(self) -> None:
        """Activity names use '{app_name}:{task_name}' format, eliminating name collisions."""

        class _DedupPrimary(App):
            @task(timeout_seconds=60)
            async def task_p(self, input: _PrimaryIn) -> _PrimaryOut:
                return _PrimaryOut()

            async def run(self, input: _PrimaryIn) -> _PrimaryOut:
                return _PrimaryOut()

        class _DedupSecondary(App):
            @task(timeout_seconds=60)
            async def task_s(self, input: _SecondaryIn) -> _SecondaryOut:
                return _SecondaryOut()

            async def run(self, input: _SecondaryIn) -> _SecondaryOut:
                return _SecondaryOut()

        activities = get_all_task_activities()
        # Verify qualified names exist in Temporal definitions
        defn_names = {
            getattr(a, "__temporal_activity_definition").name
            for a in activities
            if hasattr(a, "__temporal_activity_definition")
        }
        assert "_dedup-primary:task_p" in defn_names
        assert "_dedup-secondary:task_s" in defn_names


class TestGetActivityOptions:
    """Tests for get_activity_options()."""

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def test_returns_start_to_close_timeout(self) -> None:
        class _TimeoutApp(App):
            @task(timeout_seconds=300)
            async def my_task(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

            async def run(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_timeout-app")
        my_task = next(t for t in tasks if t.name == "my_task")
        options = get_activity_options(my_task)

        assert "start_to_close_timeout" in options
        assert options["start_to_close_timeout"] == timedelta(seconds=300)

    def test_returns_retry_policy(self) -> None:
        class _RetryApp(App):
            @task(timeout_seconds=60, retry_max_attempts=5)
            async def retryable(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

            async def run(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_retry-app")
        retryable_task = next(t for t in tasks if t.name == "retryable")
        options = get_activity_options(retryable_task)

        assert "retry_policy" in options
        assert options["retry_policy"].maximum_attempts == 5

    def test_default_retry_policy_has_max_attempts(self) -> None:
        class _DefaultRetryApp(App):
            @task(timeout_seconds=60)
            async def simple(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

            async def run(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_default-retry-app")
        simple_task = next(t for t in tasks if t.name == "simple")
        options = get_activity_options(simple_task)
        assert options["retry_policy"] is not None

    def test_explicit_retry_policy_passed_through(self) -> None:
        """When task_metadata.retry_policy is set, get_activity_options uses it.

        This exercises the inline import of temporalio.common.RetryPolicy and
        the rp-is-not-None branch (line ~287-295).
        """

        class _ExplicitRetryApp(App):
            @task(
                timeout_seconds=60,
                retry_policy=SdkRetryPolicy(
                    max_attempts=7,
                    initial_interval=timedelta(seconds=2),
                    max_interval=timedelta(seconds=42),
                    backoff_coefficient=3.0,
                    non_retryable_errors=("ValueError",),
                ),
            )
            async def explicit(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

            async def run(self, input: _ActInput) -> _ActOutput:
                return _ActOutput()

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_explicit-retry-app")
        explicit_task = next(t for t in tasks if t.name == "explicit")
        options = get_activity_options(explicit_task)
        rp = options["retry_policy"]
        assert rp.maximum_attempts == 7
        assert rp.initial_interval == timedelta(seconds=2)
        assert rp.maximum_interval == timedelta(seconds=42)
        assert rp.backoff_coefficient == 3.0
        assert "ValueError" in rp.non_retryable_error_types


# ---------------------------------------------------------------------------
# Additional activity wrapper coverage.
# These exercise the activity_fn body, which holds 11 inline imports.
# ---------------------------------------------------------------------------


class _AfnIn(Input, allow_unbounded_fields=True):
    name: str = "x"


class _AfnOut(Output, allow_unbounded_fields=True):
    greeting: str = ""


class TestTrackFileRefs:
    """Tests for _track_file_refs (exercises inline import of _app_state*)."""

    def test_noop_when_no_refs(self) -> None:
        # No refs → does not import _app_state, does not raise.
        _track_file_refs("wf-1")

    def test_adds_refs_to_app_state(self) -> None:
        from application_sdk.app.base import _app_state

        ref = FileReference(local_path="/tmp/foo")
        _track_file_refs("wf-test-track", ref)
        from application_sdk.constants import TRACKED_FILE_REFS_KEY

        assert ref in _app_state["wf-test-track"][TRACKED_FILE_REFS_KEY]


class TestActivityFnExecution:
    """Tests for the activity_fn returned by create_activity_from_task.

    These exercise the full happy path through the activity body, hitting
    11 inline imports — a regression in any of them fails these tests.
    """

    def setup_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def teardown_method(self) -> None:
        AppRegistry.reset()
        TaskRegistry.reset()

    def _make_app_and_get_activity(self) -> tuple[type[App], object]:
        class _AfnApp(App):
            @task(timeout_seconds=60)
            async def greet(self, input: _AfnIn) -> _AfnOut:
                return _AfnOut(greeting=f"hi {input.name}")

            async def run(self, input: _AfnIn) -> _AfnOut:
                return await self.greet(input)

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_afn-app")
        greet_task = next(t for t in tasks if t.name == "greet")
        activity_fn = create_activity_from_task(greet_task)
        return _AfnApp, activity_fn

    @pytest.mark.asyncio
    async def test_happy_path_executes_task_method(self) -> None:
        _, activity_fn = self._make_app_and_get_activity()
        ctx = TaskContext(
            app_name="_afn-app",
            task_name="greet",
            run_id="run-1",
            heartbeat_timeout_seconds=None,  # disable heartbeat path
            auto_heartbeat_seconds=None,
        )
        info_mock = mock.MagicMock(workflow_id="wf-1")
        with (
            mock.patch.object(
                activities_module.activity, "info", return_value=info_mock
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
        ):
            result = await activity_fn(ctx, _AfnIn(name="vee"))
        assert isinstance(result, _AfnOut)
        assert result.greeting == "hi vee"

    @pytest.mark.asyncio
    async def test_non_retryable_error_wrapped_as_application_error(self) -> None:
        """A NonRetryableError raised inside a task is converted to ApplicationError."""

        class _FailApp(App):
            @task(timeout_seconds=60)
            async def boom(self, input: _AfnIn) -> _AfnOut:
                raise NonRetryableError("bad input", app_name="_fail-app")

            async def run(self, input: _AfnIn) -> _AfnOut:
                return await self.boom(input)

        from application_sdk.execution.errors import ApplicationError

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_fail-app")
        boom_task = next(t for t in tasks if t.name == "boom")
        activity_fn = create_activity_from_task(boom_task)

        ctx = TaskContext(
            app_name="_fail-app",
            task_name="boom",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-2"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
        ):
            with pytest.raises(ApplicationError) as exc_info:
                await activity_fn(ctx, _AfnIn(name="x"))
        # ApplicationError should be flagged non-retryable.
        assert exc_info.value.non_retryable is True

    @pytest.mark.asyncio
    async def test_retryable_exception_propagates_unchanged(self) -> None:
        class _PlainErrApp(App):
            @task(timeout_seconds=60)
            async def fail(self, input: _AfnIn) -> _AfnOut:
                raise ValueError("ordinary failure")

            async def run(self, input: _AfnIn) -> _AfnOut:
                return await self.fail(input)

        task_registry = TaskRegistry.get_instance()
        tasks = task_registry.get_tasks_for_app("_plain-err-app")
        fail_task = next(t for t in tasks if t.name == "fail")
        activity_fn = create_activity_from_task(fail_task)

        ctx = TaskContext(
            app_name="_plain-err-app",
            task_name="fail",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-3"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
        ):
            with pytest.raises(ValueError, match="ordinary failure"):
                await activity_fn(ctx, _AfnIn(name="x"))

    @pytest.mark.asyncio
    async def test_starts_and_stops_auto_heartbeat_loop(self) -> None:
        """Heartbeat path runs when heartbeat_timeout_seconds and auto_heartbeat_seconds are set."""
        _, activity_fn = self._make_app_and_get_activity()
        ctx = TaskContext(
            app_name="_afn-app",
            task_name="greet",
            run_id="run-1",
            heartbeat_timeout_seconds=60,
            auto_heartbeat_seconds=10,
        )

        # Patch auto_heartbeat_loop so it returns immediately when stop_event set.
        async def fake_loop(*, interval_seconds, heartbeat_fn, stop_event, task_name):
            await stop_event.wait()

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-hb"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=None,
            ),
            mock.patch(
                "application_sdk.execution.heartbeat.auto_heartbeat_loop",
                new=fake_loop,
            ),
        ):
            result = await activity_fn(ctx, _AfnIn(name="hb"))
        assert result.greeting == "hi hb"

    @pytest.mark.asyncio
    async def test_uses_infrastructure_when_available(self) -> None:
        """Validates the get_infrastructure() inline import path.

        When infrastructure is configured, app_context fields should be wired
        from it. We don't assert internals — only that the activity completes
        without error when infra is non-None.
        """
        _, activity_fn = self._make_app_and_get_activity()
        ctx = TaskContext(
            app_name="_afn-app",
            task_name="greet",
            run_id="run-1",
            heartbeat_timeout_seconds=None,
            auto_heartbeat_seconds=None,
        )

        infra = mock.MagicMock()
        infra.state_store = mock.MagicMock()
        infra.secret_store = mock.MagicMock()
        infra.storage = None  # Avoid file_ref_sync paths

        with (
            mock.patch.object(
                activities_module.activity,
                "info",
                return_value=mock.MagicMock(workflow_id="wf-infra"),
            ),
            mock.patch(
                "application_sdk.infrastructure.context.get_infrastructure",
                return_value=infra,
            ),
        ):
            result = await activity_fn(ctx, _AfnIn(name="i"))
        assert result.greeting == "hi i"
