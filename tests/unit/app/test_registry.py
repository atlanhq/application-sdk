"""Tests for AppRegistry and TaskRegistry."""

from dataclasses import dataclass

import pytest

from application_sdk.app.registry import (
    AppMetadata,
    AppNotRegisteredError,
    AppRegistry,
    AppVersionAlreadyRegisteredError,
    TaskNotRegisteredError,
    TaskRegistry,
)
from application_sdk.app.task import task
from application_sdk.contracts.base import Input, Output

# =============================================================================
# Test fixtures
# =============================================================================


@dataclass
class TestInput(Input):
    value: str = ""


@dataclass
class TestOutput(Output):
    result: str = ""


class _FakeAppCls:
    """Fake app class for registry tests."""

    __module__ = "tests.fake"
    __name__ = "FakeApp"
    _input_type = TestInput
    _output_type = TestOutput


# =============================================================================
# AppRegistry tests
# =============================================================================


class TestAppRegistry:
    """Tests for AppRegistry singleton."""

    def test_singleton(self) -> None:
        """AppRegistry is a singleton."""
        r1 = AppRegistry.get_instance()
        r2 = AppRegistry.get_instance()
        assert r1 is r2

    def test_register_creates_metadata(self) -> None:
        """register() creates AppMetadata."""
        registry = AppRegistry.get_instance()
        metadata = registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        assert isinstance(metadata, AppMetadata)
        assert metadata.name == "my-app"
        assert metadata.version == "1.0.0"

    def test_get_retrieves_by_name(self) -> None:
        """get() retrieves metadata by name."""
        registry = AppRegistry.get_instance()
        registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        metadata = registry.get("my-app")
        assert metadata.name == "my-app"

    def test_get_raises_app_not_found_error(self) -> None:
        """get() raises AppNotRegisteredError if not found."""
        registry = AppRegistry.get_instance()
        with pytest.raises(AppNotRegisteredError):
            registry.get("nonexistent-app")

    def test_list_apps_returns_all_names(self) -> None:
        """list_apps() returns all registered names."""
        registry = AppRegistry.get_instance()
        registry.register(
            name="app-a",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        registry.register(
            name="app-b",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        names = registry.list_apps()
        assert "app-a" in names
        assert "app-b" in names

    def test_list_all_returns_all_metadata(self) -> None:
        """list_all() returns all AppMetadata."""
        registry = AppRegistry.get_instance()
        registry.register(
            name="app-a",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        registry.register(
            name="app-b",
            version="2.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        all_apps = registry.list_all()
        assert len(all_apps) == 2

    def test_reset_clears_registry(self) -> None:
        """reset() clears all registrations."""
        registry = AppRegistry.get_instance()
        registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        AppRegistry.reset()
        assert registry.list_apps() == []

    def test_duplicate_raises_app_already_registered_error(self) -> None:
        """AppVersionAlreadyRegisteredError raised on duplicate without allow_override."""
        registry = AppRegistry.get_instance()
        registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        with pytest.raises(AppVersionAlreadyRegisteredError):
            registry.register(
                name="my-app",
                version="1.0.0",
                app_cls=_FakeAppCls,
                input_type=TestInput,
                output_type=TestOutput,
            )

    def test_allow_override_works(self) -> None:
        """allow_override=True allows re-registration."""
        registry = AppRegistry.get_instance()
        registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        # Should not raise
        registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
            allow_override=True,
        )

    def test_get_latest_version(self) -> None:
        """get() without version returns latest."""
        registry = AppRegistry.get_instance()
        registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        registry.register(
            name="my-app",
            version="2.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
            allow_override=True,
        )
        metadata = registry.get("my-app")
        assert metadata.version == "2.0.0"

    def test_get_specific_version(self) -> None:
        """get() with version returns specific version."""
        registry = AppRegistry.get_instance()
        registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        registry.register(
            name="my-app",
            version="2.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
            allow_override=True,
        )
        metadata = registry.get("my-app", "1.0.0")
        assert metadata.version == "1.0.0"

    def test_metadata_qualified_name(self) -> None:
        """AppMetadata.qualified_name returns '{name}@{version}'."""
        registry = AppRegistry.get_instance()
        metadata = registry.register(
            name="my-app",
            version="1.0.0",
            app_cls=_FakeAppCls,
            input_type=TestInput,
            output_type=TestOutput,
        )
        assert metadata.qualified_name == "my-app@1.0.0"

    def test_non_dataclass_input_raises(self) -> None:
        """Non-dataclass input type raises ContractDeclarationError."""
        from application_sdk.contracts.base import ContractDeclarationError

        registry = AppRegistry.get_instance()
        with pytest.raises(ContractDeclarationError):
            registry.register(
                name="my-app",
                version="1.0.0",
                app_cls=_FakeAppCls,
                input_type=str,  # type: ignore[arg-type]
                output_type=TestOutput,
            )


# =============================================================================
# TaskRegistry tests
# =============================================================================


class TestTaskRegistry:
    """Tests for TaskRegistry singleton."""

    def test_singleton(self) -> None:
        """TaskRegistry is a singleton."""
        r1 = TaskRegistry.get_instance()
        r2 = TaskRegistry.get_instance()
        assert r1 is r2

    def _make_task_metadata(self, name: str) -> "object":
        """Create a minimal TaskMetadata for testing."""

        class FakeApp:
            @task
            async def my_method(self, input: TestInput) -> TestOutput:
                return TestOutput()

        from application_sdk.app.task import get_task_metadata

        meta = get_task_metadata(FakeApp.my_method)
        assert meta is not None
        meta.name = name
        return meta

    def test_register_and_get(self) -> None:
        """TaskRegistry.register() and get() work."""
        registry = TaskRegistry.get_instance()
        meta = self._make_task_metadata("my-task")
        registry.register("my-app", meta)  # type: ignore[arg-type]

        retrieved = registry.get("my-app", "my-task")
        assert retrieved.name == "my-task"  # type: ignore[union-attr]

    def test_get_tasks_for_app(self) -> None:
        """get_tasks_for_app() returns all tasks for an app."""
        registry = TaskRegistry.get_instance()
        meta1 = self._make_task_metadata("task-one")
        meta2 = self._make_task_metadata("task-two")

        registry.register("my-app", meta1)  # type: ignore[arg-type]
        registry.register("my-app", meta2)  # type: ignore[arg-type]

        tasks = registry.get_tasks_for_app("my-app")
        task_names = [t.name for t in tasks]
        assert "task-one" in task_names
        assert "task-two" in task_names

    def test_get_tasks_for_app_empty_when_not_registered(self) -> None:
        """get_tasks_for_app() returns empty list for unknown app."""
        registry = TaskRegistry.get_instance()
        tasks = registry.get_tasks_for_app("nonexistent-app")
        assert tasks == []

    def test_get_raises_task_not_found(self) -> None:
        """get() raises TaskNotRegisteredError if not found."""
        registry = TaskRegistry.get_instance()
        with pytest.raises(TaskNotRegisteredError):
            registry.get("my-app", "nonexistent-task")

    def test_get_activity_name_format(self) -> None:
        """get_activity_name() format is '{app_name}:{task_name}'."""
        registry = TaskRegistry.get_instance()
        activity_name = registry.get_activity_name("my-app", "my-task")
        assert activity_name == "my-app:my-task"

    def test_reset_clears_tasks(self) -> None:
        """reset() clears all task registrations."""
        registry = TaskRegistry.get_instance()
        meta = self._make_task_metadata("my-task")
        registry.register("my-app", meta)  # type: ignore[arg-type]

        TaskRegistry.reset()
        assert registry.get_tasks_for_app("my-app") == []

    def test_app_name_set_on_register(self) -> None:
        """TaskMetadata.app_name is set when registered."""
        registry = TaskRegistry.get_instance()
        meta = self._make_task_metadata("my-task")
        registry.register("my-app", meta)  # type: ignore[arg-type]

        retrieved = registry.get("my-app", "my-task")
        assert retrieved.app_name == "my-app"  # type: ignore[union-attr]


# =============================================================================
# Sandbox re-import
# =============================================================================


class _SandboxInput(Input):
    value: str = ""


class _SandboxOutput(Output):
    result: str = ""


class TestSandboxReimport:
    """Temporal's sandbox re-imports the app module for every workflow run.

    That re-runs ``App.__init_subclass__`` on a fresh copy of each class. The
    copy must not reach the host registries, and must carry its own generated
    workflow classes, which the sandbox imports from the module by name.
    """

    @staticmethod
    def _define_app() -> type:
        from application_sdk.app.base import App

        class SandboxReimportApp(App):
            name = "sandbox-reimport-app"

            @task
            async def echo(self, input: _SandboxInput) -> _SandboxOutput:
                return _SandboxOutput(result=input.value)

            async def run(self, input: _SandboxInput) -> _SandboxOutput:
                return await self.echo(input)

        return SandboxReimportApp

    @staticmethod
    def _define_app_in_sandbox() -> type:
        from temporalio import workflow

        workflow.unsafe._set_in_sandbox(True)
        try:
            return TestSandboxReimport._define_app()
        finally:
            workflow.unsafe._set_in_sandbox(False)

    def test_reimport_leaves_host_registries_alone(self) -> None:
        host_cls = self._define_app()
        tasks_before = TaskRegistry.get_instance().get_tasks_for_app(
            "sandbox-reimport-app"
        )

        sandbox_cls = self._define_app_in_sandbox()

        assert sandbox_cls is not host_cls
        registered = AppRegistry.get_instance().get("sandbox-reimport-app")
        assert registered.app_cls is host_cls
        assert (
            TaskRegistry.get_instance().get_tasks_for_app("sandbox-reimport-app")
            == tasks_before
        )
        # The copy is stamped like a registered class, from metadata of its own.
        assert sandbox_cls._app_metadata.app_cls is sandbox_cls
        assert sandbox_cls._app_name == "sandbox-reimport-app"

    def test_reimport_installs_workflow_classes_built_from_the_copy(self) -> None:
        import sys

        from application_sdk.app.base import _workflow_class_cache

        self._define_app()
        cache_before = dict(_workflow_class_cache)

        sandbox_cls = self._define_app_in_sandbox()

        wf_cls = getattr(sys.modules[__name__], "_Workflow_sandbox_reimport_app")
        assert wf_cls.__module__ == __name__
        # The generated __init__ builds the run's App instance from the copy,
        # so run() executes as the sandbox's code, with restricted globals.
        assert type(wf_cls()._app_instance) is sandbox_cls
        # Host state keeps nothing from the run: an entry would pin the sandbox.
        assert _workflow_class_cache == cache_before
