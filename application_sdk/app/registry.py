"""App and Task discovery and registration."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from application_sdk.app.entrypoint import EntryPointMetadata
    from application_sdk.app.task import TaskMetadata

from application_sdk.contracts.base import validate_is_contract
from application_sdk.errors import (
    APP_ALREADY_REGISTERED,
    APP_NOT_FOUND,
    TASK_NOT_FOUND,
    ErrorCode,
)


@dataclass(frozen=True)
class AppMetadata:
    """Metadata about a registered App."""

    name: str
    version: str
    app_cls: type
    input_type: type
    output_type: type
    module_path: str = ""
    class_name: str = ""
    description: str = ""
    tags: dict[str, str] = field(default_factory=dict)
    passthrough_modules: frozenset[str] = field(default_factory=frozenset)
    entry_points: "dict[str, EntryPointMetadata]" = field(default_factory=dict)
    deprecated: bool = False
    deprecation_message: str | None = None

    @property
    def qualified_name(self) -> str:
        """Fully qualified name including version."""
        return f"{self.name}@{self.version}"


class AppNotFoundError(Exception):
    """Raised when an App is not found in the registry."""

    def __init__(
        self,
        name: str,
        version: str | None = None,
        *,
        error_code: ErrorCode | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self.error_code = error_code or APP_NOT_FOUND
        if version:
            msg = f"App '{name}' version '{version}' not found"
        else:
            msg = f"App '{name}' not found"
        super().__init__(msg)

    def __str__(self) -> str:
        return f"[{self.error_code}] {super().__str__()}"


class AppAlreadyRegisteredError(Exception):
    """Raised when attempting to register an App that already exists."""

    def __init__(
        self, name: str, version: str, *, error_code: ErrorCode | None = None
    ) -> None:
        self.name = name
        self.version = version
        self.error_code = error_code or APP_ALREADY_REGISTERED
        super().__init__(f"App '{name}' version '{version}' is already registered")

    def __str__(self) -> str:
        return f"[{self.error_code}] {super().__str__()}"


class AppRegistry:
    """Registry for App discovery and registration.

    The registry is a singleton that holds all registered Apps.
    Apps are registered via the App base class __init_subclass__.
    """

    _instance: "AppRegistry | None" = None
    _apps: dict[str, dict[str, AppMetadata]]  # name -> version -> metadata

    def __new__(cls) -> "AppRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._apps = {}
        return cls._instance

    @classmethod
    def get_instance(cls) -> "AppRegistry":
        """Get the singleton registry instance."""
        return cls()

    @classmethod
    def reset(cls) -> None:
        """Reset the registry (primarily for testing)."""
        if cls._instance is not None:
            cls._instance._apps = {}

    def register(
        self,
        name: str,
        version: str,
        app_cls: type,
        input_type: type,
        output_type: type,
        *,
        description: str = "",
        tags: dict[str, str] | None = None,
        passthrough_modules: set[str] | None = None,
        entry_points: "dict[str, EntryPointMetadata] | None" = None,
        allow_override: bool = False,
    ) -> AppMetadata:
        """Register an App.

        Args:
            name: Unique name for the App.
            version: Semantic version string.
            app_cls: The App class.
            input_type: The input dataclass type.
            output_type: The output dataclass type.
            description: Human-readable description.
            tags: Optional tags for categorization.
            passthrough_modules: Modules to pass through sandbox for this App.
            entry_points: Entry point metadata keyed by entry point name.
            allow_override: If True, allow re-registration (for testing).

        Returns:
            The registered AppMetadata.

        Raises:
            AppAlreadyRegisteredError: If App already registered and not overriding.
            ContractValidationError: If input/output types are not dataclasses.
        """
        validate_is_contract(input_type, f"Input type for App '{name}'")
        validate_is_contract(output_type, f"Output type for App '{name}'")

        if name not in self._apps:
            self._apps[name] = {}

        if version in self._apps[name] and not allow_override:
            raise AppAlreadyRegisteredError(name, version)

        metadata = AppMetadata(
            name=name,
            version=version,
            app_cls=app_cls,
            input_type=input_type,
            output_type=output_type,
            module_path=app_cls.__module__,
            class_name=app_cls.__name__,
            description=description,
            tags=tags or {},
            passthrough_modules=frozenset(passthrough_modules or set()),
            entry_points=entry_points or {},
        )

        self._apps[name][version] = metadata
        return metadata

    def get(self, name: str, version: str | None = None) -> AppMetadata:
        """Get an App by name and optionally version.

        Args:
            name: App name.
            version: Specific version, or None for latest.

        Returns:
            The AppMetadata.

        Raises:
            AppNotFoundError: If App not found.
        """
        if name not in self._apps:
            raise AppNotFoundError(name, version)

        versions = self._apps[name]
        if not versions:
            raise AppNotFoundError(name, version)

        if version is not None:
            if version not in versions:
                raise AppNotFoundError(name, version)
            return versions[version]

        # Return latest version (simple string sort - semver would be better)
        latest_version = sorted(versions.keys())[-1]
        return versions[latest_version]

    def get_all_versions(self, name: str) -> list[AppMetadata]:
        """Get all versions of an App.

        Args:
            name: App name.

        Returns:
            List of AppMetadata for all versions.

        Raises:
            AppNotFoundError: If App not found.
        """
        if name not in self._apps:
            raise AppNotFoundError(name)

        return list(self._apps[name].values())

    def list_apps(self) -> list[str]:
        """List all registered App names."""
        return list(self._apps.keys())

    def list_all(self) -> list[AppMetadata]:
        """List all registered Apps (all versions)."""
        result: list[AppMetadata] = []
        for versions in self._apps.values():
            result.extend(versions.values())
        return result

    def get_all_passthrough_modules(self) -> frozenset[str]:
        """Get all passthrough modules from all registered Apps.

        Returns:
            Combined set of all passthrough modules.
        """
        result: set[str] = set()
        for versions in self._apps.values():
            for metadata in versions.values():
                result.update(metadata.passthrough_modules)
        return frozenset(result)

    def unregister(self, name: str, version: str | None = None) -> None:
        """Unregister an App.

        Args:
            name: App name.
            version: Specific version, or None for all versions.
        """
        if name not in self._apps:
            return

        if version is not None:
            self._apps[name].pop(version, None)
            if not self._apps[name]:
                del self._apps[name]
        else:
            del self._apps[name]

    def deprecate(
        self,
        name: str,
        version: str,
        message: str | None = None,
    ) -> None:
        """Mark an App version as deprecated.

        Args:
            name: App name.
            version: Version to deprecate.
            message: Optional deprecation message.
        """
        metadata = self.get(name, version)
        # Create new metadata with deprecated flag
        # (AppMetadata is frozen, so we create a new instance)
        new_metadata = AppMetadata(
            name=metadata.name,
            version=metadata.version,
            app_cls=metadata.app_cls,
            input_type=metadata.input_type,
            output_type=metadata.output_type,
            description=metadata.description,
            tags=metadata.tags,
            entry_points=metadata.entry_points,
            deprecated=True,
            deprecation_message=message,
        )
        self._apps[name][version] = new_metadata


class TaskNotFoundError(Exception):
    """Raised when a Task is not found in the registry."""

    def __init__(
        self, app_name: str, task_name: str, *, error_code: ErrorCode | None = None
    ) -> None:
        self.app_name = app_name
        self.task_name = task_name
        self.error_code = error_code or TASK_NOT_FOUND
        super().__init__(f"Task '{task_name}' not found in app '{app_name}'")

    def __str__(self) -> str:
        return f"[{self.error_code}] {super().__str__()}"


class TaskRegistry:
    """Registry for Task discovery and registration.

    Tasks are private to their parent App and become Temporal activities.
    This registry tracks all registered tasks for worker setup.
    """

    _instance: "TaskRegistry | None" = None
    _tasks: dict[str, dict[str, "TaskMetadata"]]  # app_name -> task_name -> metadata

    def __new__(cls) -> "TaskRegistry":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._tasks = {}
        return cls._instance

    @classmethod
    def get_instance(cls) -> "TaskRegistry":
        """Get the singleton registry instance."""
        return cls()

    @classmethod
    def reset(cls) -> None:
        """Reset the registry (primarily for testing)."""
        if cls._instance is not None:
            cls._instance._tasks = {}

    def register(
        self,
        app_name: str,
        task_metadata: "TaskMetadata",
    ) -> None:
        """Register a task for an App.

        Args:
            app_name: The parent app name.
            task_metadata: The task metadata.
        """
        if app_name not in self._tasks:
            self._tasks[app_name] = {}

        # Update the app_name on the metadata
        task_metadata.app_name = app_name
        self._tasks[app_name][task_metadata.name] = task_metadata

    def get(self, app_name: str, task_name: str) -> "TaskMetadata":
        """Get a task by app name and task name.

        Args:
            app_name: The parent app name.
            task_name: The task name.

        Returns:
            The TaskMetadata.

        Raises:
            TaskNotFoundError: If task not found.
        """
        if app_name not in self._tasks:
            raise TaskNotFoundError(app_name, task_name)

        if task_name not in self._tasks[app_name]:
            raise TaskNotFoundError(app_name, task_name)

        return self._tasks[app_name][task_name]

    def get_tasks_for_app(self, app_name: str) -> list["TaskMetadata"]:
        """Get all tasks for an App.

        Args:
            app_name: The app name.

        Returns:
            List of TaskMetadata for the app.
        """
        if app_name not in self._tasks:
            return []
        return list(self._tasks[app_name].values())

    def get_all_tasks(self) -> dict[str, list["TaskMetadata"]]:
        """Get all registered tasks grouped by app.

        Returns:
            Dict mapping app_name to list of TaskMetadata.
        """
        return {
            app_name: list(tasks.values()) for app_name, tasks in self._tasks.items()
        }

    def list_apps_with_tasks(self) -> list[str]:
        """List all app names that have registered tasks."""
        return list(self._tasks.keys())

    def get_activity_name(self, app_name: str, task_name: str) -> str:
        """Get the Temporal activity name for a task.

        Args:
            app_name: The parent app name.
            task_name: The task name.

        Returns:
            Activity name in format ``{app_name}:{task_name}``.
        """
        return f"{app_name}:{task_name}"
