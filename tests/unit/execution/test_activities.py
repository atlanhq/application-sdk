"""Unit tests for Temporal activity creation from tasks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from application_sdk.app.base import App
from application_sdk.app.registry import AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output
from application_sdk.execution._temporal.activities import (
    create_activity_from_task,
    get_activity_options,
    get_all_task_activities,
)


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
