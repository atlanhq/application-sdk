"""App base class and decorators."""

import importlib.metadata
import inspect
import os
import re
import shutil
import sys
import threading
from abc import ABC
from collections.abc import Callable
from datetime import datetime
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Never,
    TypeVar,
    cast,
    get_type_hints,
    overload,
)
from uuid import UUID

from temporalio import workflow

from application_sdk.app.context import (
    AppContext,
    TaskExecutionContext,
    _is_atlan_logger,
)
from application_sdk.app.registry import AppMetadata, AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import HeartbeatDetails, Input, Output
from application_sdk.contracts.cleanup import (
    CleanupInput,
    CleanupOutput,
    StorageCleanupInput,
    StorageCleanupOutput,
)
from application_sdk.contracts.storage import (
    DownloadInput,
    DownloadOutput,
    UploadInput,
    UploadOutput,
)
from application_sdk.contracts.types import FileReference
from application_sdk.errors import (
    APP_CONTEXT_ERROR,
    APP_ERROR,
    APP_NON_RETRYABLE,
    ErrorCode,
)

try:
    _FRAMEWORK_VERSION = importlib.metadata.version("application-sdk")
except importlib.metadata.PackageNotFoundError:
    _FRAMEWORK_VERSION = "unknown"

if TYPE_CHECKING:
    from application_sdk.app.client import WorkflowAppClient
    from application_sdk.app.entrypoint import EntryPointMetadata

# Type variable for require() method
T = TypeVar("T")
HT = TypeVar("HT", bound=HeartbeatDetails)

# Type variables for child app calls
TChildInput = TypeVar("TChildInput", bound=Input)
TChildOutput = TypeVar("TChildOutput", bound=Output)


def _pascal_to_kebab(name: str) -> str:
    """Convert PascalCase to kebab-case.

    Examples:
        Greeter -> greeter
        CsvPipeline -> csv-pipeline
        MyAwesomeApp -> my-awesome-app
        HTTPHandler -> http-handler
        S3Loader -> s3-loader
    """
    # Handle consecutive uppercase (like HTTP -> http-)
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1-\2", name)
    # Handle lowercase followed by uppercase (like my -> my-)
    s = re.sub(r"([a-z\d])([A-Z])", r"\1-\2", s)
    return s.lower()


def _safe_now() -> datetime:
    """Get current time (deterministic for Temporal replay).

    Always runs in Temporal workflow context.
    """
    return workflow.now()


def _safe_uuid() -> UUID:
    """Generate UUID (deterministic for Temporal replay).

    Always runs in Temporal workflow context.
    """
    return UUID(str(workflow.uuid4()))


def _safe_log(level: str, message: str, **attrs: Any) -> None:
    """Log using Temporal's workflow logger.

    Always runs in Temporal workflow context.

    When workflow.logger is AtlanLoggerAdapter (the normal case after the events
    interceptor module is imported), attrs are passed directly as flat kwargs and
    surface as structured fields in loguru / OTEL.

    When workflow.logger is a stdlib Logger (edge case before the interceptor has
    loaded), attrs are packed into extra= to avoid TypeError from stdlib's
    reserved-kwarg restriction.
    """
    wf_logger = workflow.logger
    log_method = getattr(wf_logger, level)
    if _is_atlan_logger(wf_logger):
        log_method(message, **attrs)
    else:
        # stdlib logging reserves exc_info, stack_info, stacklevel as direct
        # kwargs — stuffing them into extra= causes makeRecord to raise KeyError.
        _STDLIB_RESERVED = {"exc_info", "stack_info", "stacklevel"}
        reserved = {k: v for k, v in attrs.items() if k in _STDLIB_RESERVED}
        extra = {k: v for k, v in attrs.items() if k not in _STDLIB_RESERVED}
        if extra:
            log_method(message, extra=extra, **reserved)
        elif reserved:
            log_method(message, **reserved)
        else:
            log_method(message)


# =============================================================================
# Error Classes
# =============================================================================


class AppError(Exception):
    """Base exception for App-related errors."""

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = APP_ERROR

    def __init__(
        self,
        message: str,
        *,
        app_name: str | None = None,
        run_id: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.app_name = app_name
        self.run_id = run_id
        self.cause = cause
        self._error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        """Structured error code for monitoring and alerting."""
        return (
            self._error_code
            if self._error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        parts = [f"[{self.error_code.code}] {self.message}"]
        if self.app_name:
            parts.append(f"app={self.app_name}")
        if self.run_id:
            parts.append(f"run_id={self.run_id}")
        if self.cause:
            parts.append(f"caused_by={self.cause}")
        return " | ".join(parts)


class AppContextError(RuntimeError):
    """Raised when App or task context is accessed outside of valid execution scope.

    This is a programming error — it indicates that context-dependent methods
    (e.g. ``self.context``, ``self.heartbeat()``) were called outside of a
    workflow run or @task execution.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = APP_CONTEXT_ERROR

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        super().__init__(message)
        self._error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        """Structured error code for monitoring and alerting."""
        return (
            self._error_code
            if self._error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        return f"[{self.error_code.code}] {super().__str__()}"


class NonRetryableError(AppError):
    """Error that should not be retried.

    Use this for failures that are deterministic and will never succeed on retry:
    - Authentication failures (invalid credentials)
    - Authorization failures (insufficient permissions)
    - Validation failures (invalid input data)
    - Configuration errors (missing required settings)

    When raised in a workflow or activity, Temporal will NOT retry the operation.

    Example::

        if not auth_result.success:
            raise NonRetryableError(
                "Authentication failed: invalid API key",
                app_name="my-pipeline",
            )
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = APP_NON_RETRYABLE


# =============================================================================
# State Accessors
# =============================================================================


# Class-level app state storage shared by all App subclasses.
# Keyed by workflow_id to isolate state between different app executions.
_app_state: dict[str, dict[str, Any]] = {}
_app_state_lock = threading.Lock()


def _get_execution_id_from_task() -> str:
    """Get the execution ID from current task context.

    Returns:
        The execution ID string.

    Raises:
        RuntimeError: If called outside a @task method.
    """
    from temporalio import activity

    try:
        return activity.info().workflow_id or ""
    except Exception as e:
        raise AppContextError("Cannot access app state outside of task context") from e


class TaskStateAccessor:
    """Accessor for app state from within a task (not a @task method on an App).

    This provides the same interface as AppStateAccessor but can be used
    in standalone tasks that don't have an App instance.
    The state is shared with App instances via the module-level _app_state dict.

    Usage:
        state = TaskStateAccessor()
        state.set("client", my_client)
        cached = state.get("client")
    """

    def get(self, key: str) -> Any | None:
        """Get in-memory state for the current app execution."""
        workflow_id = _get_execution_id_from_task()
        with _app_state_lock:
            return _app_state.get(workflow_id, {}).get(key)

    def set(self, key: str, value: Any) -> None:
        """Set in-memory state for the current app execution."""
        workflow_id = _get_execution_id_from_task()
        with _app_state_lock:
            if workflow_id not in _app_state:
                _app_state[workflow_id] = {}
            _app_state[workflow_id][key] = value


class AppStateAccessor:
    """Accessor for in-memory state scoped to app execution.

    Provides a clean namespace for app-scoped state operations:
        self.app_state.get(key)
        self.app_state.set(key, value)

    State persists across task calls within the same app execution but
    is NOT persisted externally - if the worker restarts, state is lost.
    """

    def __init__(self, app: "App") -> None:
        self._app = app

    def get(self, key: str) -> Any | None:
        """Get in-memory state for the current app execution.

        Args:
            key: State key to retrieve.

        Returns:
            The stored value, or None if not set.

        Raises:
            RuntimeError: If called outside a @task method.
        """
        return self._app.get_app_state(key)

    def set(self, key: str, value: Any) -> None:
        """Set in-memory state for the current app execution.

        Args:
            key: State key to store.
            value: Value to store (any Python object).

        Raises:
            RuntimeError: If called outside a @task method.
        """
        self._app.set_app_state(key, value)


class PersistentStateAccessor:
    """Accessor for durable state stored externally.

    Provides a clean namespace for persistent state operations:
        await self.persistent_state.load(key)
        await self.persistent_state.save(key, value)

    State is persisted to an external state store and survives
    worker restarts.
    """

    def __init__(self, app: "App") -> None:
        self._app = app

    async def save(self, key: str, value: dict[str, Any]) -> None:
        """Save state to the external state store.

        Args:
            key: State key (will be namespaced to this app/run).
            value: State data to save.

        Raises:
            RuntimeError: If no state store is configured.
        """
        await self._app.context.save_state(key, value)

    async def load(self, key: str) -> dict[str, Any] | None:
        """Load state from the external state store.

        Args:
            key: State key (will be namespaced to this app/run).

        Returns:
            The saved state or None if not found.

        Raises:
            RuntimeError: If no state store is configured.
        """
        return await self._app.context.load_state(key)


# =============================================================================
# App Base Class
# =============================================================================


class App(ABC):
    """Base class for all Apps.

    Apps are the fundamental unit of execution in Application SDK.
    Each App:
    - Has a single typed input (dataclass)
    - Has a single typed output (dataclass)
    - Can call other Apps
    - Is durable and resumable

    The run() method must be deterministic - use @task methods for side effects.

    Example::

        class MyApp(App):

            @task
            async def fetch_data(self, input: FetchInput) -> FetchOutput:
                # Tasks can do I/O - no restrictions
                return FetchOutput(data=await http_client.get(input.url).json())

            async def run(self, input: MyInput) -> MyOutput:
                # run() is deterministic - only call tasks and other apps
                result = await self.fetch_data(FetchInput(url=input.url))
                return MyOutput(data=result.data)

    Override class attributes when needed::

        class CsvPipeline(App):
            name = "csv-ingest-v2"  # Override derived name
            version = "2.0.0"       # Override default version

            async def run(self, input: PipelineInput) -> PipelineOutput:
                ...

    Name derivation from class name:

    - ``Greeter`` → ``greeter``
    - ``CsvPipeline`` → ``csv-pipeline``
    - ``MyAwesomeApp`` → ``my-awesome-app``
    - ``HTTPHandler`` → ``http-handler``
    """

    # Class-level configuration (override in subclasses)
    name: ClassVar[str] = ""  # Empty = derive from class name
    version: ClassVar[str] = "1.0.0"
    description: ClassVar[str] = ""
    tags: ClassVar[dict[str, str] | None] = None
    passthrough_modules: ClassVar[set[str] | None] = None

    # Marker to track if class has been registered
    _app_registered: ClassVar[bool] = False

    # Set by registration
    _app_name: str
    _app_version: str
    _app_metadata: AppMetadata
    _original_run: Callable[..., Any]
    _input_type: type[Input]
    _output_type: type[Output]

    # Set by the execution layer before run() is called
    _context: AppContext | None = None
    _client: "WorkflowAppClient | None" = None
    _task_context: "TaskExecutionContext | None" = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Automatically register App subclasses.

        This is called when a class inherits from App. It:
        1. Derives the app name from the class name if not specified
        2. Scans for @entrypoint methods (Path 1) or infers from run() (Path 2)
        3. Registers the app with the AppRegistry

        Skip registration if:
        - The class was already registered
        - The class has other abstract methods (besides run)
        - No valid entry points or run() with proper types found
        """
        super().__init_subclass__(**kwargs)

        # Skip if already registered (check own __dict__ only, not inherited)
        if cls.__dict__.get("_app_registered", False):
            return

        app_name = cls.name or _pascal_to_kebab(cls.__name__)

        # Path 1: explicit @entrypoint methods
        entry_points = _scan_entrypoints(cls)
        if entry_points:
            # Only register concrete classes (no unimplemented abstract methods besides run)
            abstract_methods = {
                m
                for m in dir(cls)
                if getattr(getattr(cls, m, None), "__isabstractmethod__", False)
            }
            if abstract_methods - {"run"}:
                return  # Genuine abstract class with other abstract methods

            # Every entry point must follow the single-dataclass contract:
            # one Input subclass parameter, one Output subclass return type.
            # Unlike the implicit run() path (which silently skips template base
            # classes), explicit @entrypoint decoration is always intentional —
            # raise loudly so the developer sees the problem immediately.
            # deferred import: circular dependency (entrypoint imports App)
            from application_sdk.app.entrypoint import EntryPointContractError

            for ep in entry_points.values():
                if not (
                    isinstance(ep.input_type, type) and issubclass(ep.input_type, Input)
                ):
                    raise EntryPointContractError(
                        f"Entry point '{ep.name}' on {cls.__name__}: "
                        f"input type {ep.input_type!r} must be a subclass of Input."
                    )
                if not (
                    isinstance(ep.output_type, type)
                    and issubclass(ep.output_type, Output)
                ):
                    raise EntryPointContractError(
                        f"Entry point '{ep.name}' on {cls.__name__}: "
                        f"output type {ep.output_type!r} must be a subclass of Output."
                    )

            first_ep = next(iter(entry_points.values()))
            _apply_app_registration(
                cls=cls,
                name=app_name,
                version=cls.version,
                description=cls.description,
                tags=cls.tags,
                passthrough_modules=cls.passthrough_modules,
                input_type=first_ep.input_type,
                output_type=first_ep.output_type,
                entry_points=entry_points,
            )
            return

        # Path 2: implicit run() — backward compat
        if inspect.isabstract(cls):
            return

        # Only consider an explicitly overridden run(). Classes that do not
        # override run() inherit the default raise-NotImplementedError stub —
        # silently skip them (they may define only @entrypoint methods, or they
        # may be intermediate base classes that are not yet concrete).
        if "run" not in cls.__dict__:
            return

        run_method = cls.__dict__["run"]

        if getattr(run_method, "__isabstractmethod__", False):
            return

        # deferred import: circular dependency (entrypoint imports App)
        from application_sdk.app.entrypoint import EntryPointContractError

        try:
            hints = get_type_hints(cls.run)
        except Exception:
            return  # Unresolvable annotations (e.g. forward refs) — skip silently

        input_type = hints.get("input")
        output_type = hints.get("return")

        if input_type is None or output_type is None:
            raise EntryPointContractError(
                f"run() on {cls.__name__} must have type annotations: "
                f"async def run(self, input: <Input subclass>) -> <Output subclass>"
            )

        if not (isinstance(input_type, type) and issubclass(input_type, Input)):
            raise EntryPointContractError(
                f"run() on {cls.__name__}: input type {input_type!r} must be "
                f"a subclass of Input."
            )
        if not (isinstance(output_type, type) and issubclass(output_type, Output)):
            raise EntryPointContractError(
                f"run() on {cls.__name__}: output type {output_type!r} must be "
                f"a subclass of Output."
            )

        # Using the base Input/Output directly is an error — concrete apps must
        # define their own narrowed dataclass types.
        if input_type is Input:
            raise EntryPointContractError(
                f"run() on {cls.__name__}: input type must be a concrete subclass "
                f"of Input, not Input itself. Define a dedicated dataclass."
            )
        if output_type is Output:
            raise EntryPointContractError(
                f"run() on {cls.__name__}: output type must be a concrete subclass "
                f"of Output, not Output itself. Define a dedicated dataclass."
            )

        # deferred import: circular dependency (entrypoint imports App)
        from application_sdk.app.entrypoint import EntryPointMetadata

        implicit_ep = EntryPointMetadata(
            name="run",
            input_type=input_type,
            output_type=output_type,
            method_name="run",
            implicit=True,
        )

        _apply_app_registration(
            cls=cls,
            name=app_name,
            version=cls.version,
            description=cls.description,
            tags=cls.tags,
            passthrough_modules=cls.passthrough_modules,
            input_type=input_type,
            output_type=output_type,
            entry_points={"run": implicit_ep},
        )

    @property
    def context(self) -> AppContext:
        """Get the current execution context.

        Raises:
            RuntimeError: If accessed outside of run() execution.
        """
        if self._context is None:
            raise AppContextError(
                "App context is only available during run() execution. "
                "Do not access context in __init__ or outside of run()."
            )
        return self._context

    @property
    def task_context(self) -> TaskExecutionContext:
        """Get the current task execution context.

        Only available inside @task methods.

        Raises:
            RuntimeError: If accessed outside of @task method execution.
        """
        if self._task_context is None:
            raise AppContextError(
                "task_context is only available during @task method execution. "
                "Do not access task_context in run() or outside of task methods."
            )
        return self._task_context

    # =========================================================================
    # Convenience accessors for common context properties
    # =========================================================================

    @property
    def logger(self) -> Any:
        """Get a logger bound to this app context."""
        return self.context.logger

    @property
    def run_id(self) -> str:
        """Get the current run ID."""
        return self.context.run_id

    @property
    def correlation_id(self) -> str:
        """Get the correlation ID."""
        return self.context.correlation_id

    def is_cancelled(self) -> bool:
        """Check if execution has been cancelled."""
        return self.context.is_cancelled()

    # =========================================================================
    # Task-only methods (raise RuntimeError if called outside @task methods)
    # =========================================================================

    def heartbeat(self, *details: Any) -> None:
        """Send a heartbeat with optional progress details.

        Only available in @task methods.

        Args:
            *details: Serializable progress details.

        Raises:
            RuntimeError: If called outside a @task method.
        """
        if self._task_context is None:
            raise AppContextError(
                "heartbeat() can only be called inside @task methods. "
                "Do not call this in run() or outside of task methods."
            )
        self._task_context.heartbeat(*details)

    def get_last_heartbeat_details(self) -> tuple[Any, ...]:
        """Get details from last heartbeat (for resume on retry).

        Only available in @task methods.

        Returns:
            Tuple of details from last heartbeat, or empty tuple if none.

        Raises:
            RuntimeError: If called outside a @task method.
        """
        if self._task_context is None:
            raise AppContextError(
                "get_last_heartbeat_details() can only be called inside @task methods."
            )
        return self._task_context.get_last_heartbeat_details()

    def get_heartbeat_details(self, cls: type[HT]) -> HT | None:
        """Get last heartbeat details deserialized as a typed dataclass.

        Args:
            cls: HeartbeatDetails subclass to deserialize into.

        Returns:
            An instance of cls with values from the last heartbeat,
            or None if no heartbeat was recorded.

        Raises:
            RuntimeError: If called outside a @task method.
        """
        if self._task_context is None:
            raise AppContextError(
                "get_heartbeat_details() can only be called inside @task methods."
            )
        return self._task_context.get_heartbeat_details(cls)

    async def run_in_thread(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """Run a blocking function in a thread pool.

        Only available in @task methods.

        Args:
            func: Blocking function to run.
            *args: Positional arguments for func.
            **kwargs: Keyword arguments for func.

        Returns:
            Result of ``func(*args, **kwargs)``.

        Raises:
            RuntimeError: If called outside a @task method.
        """
        if self._task_context is None:
            raise AppContextError(
                "run_in_thread() can only be called inside @task methods."
            )
        return await self._task_context.run_in_thread(func, *args, **kwargs)

    # =========================================================================
    # State accessors
    # =========================================================================

    @property
    def app_state(self) -> "AppStateAccessor":
        """Access in-memory state scoped to this app execution."""
        return AppStateAccessor(self)

    @property
    def persistent_state(self) -> "PersistentStateAccessor":
        """Access durable state stored externally."""
        return PersistentStateAccessor(self)

    def get_name(self) -> str:
        """Get the app name."""
        return self._app_name

    def get_version(self) -> str:
        """Get the app version."""
        return self._app_version

    def now(self) -> datetime:
        """Get current time (safe for workflow replay).

        Use this instead of datetime.now() or datetime.utcnow() in run().
        """
        return _safe_now()

    def uuid(self) -> UUID:
        """Generate a UUID (safe for workflow replay).

        Use this instead of uuid.uuid4() in run().
        """
        return _safe_uuid()

    def require(self, value: "T | None", name: str, context: str = "") -> "T":
        """Require a value to be non-None, raising NonRetryableError if it is.

        Args:
            value: The value to check.
            name: Name of the parameter for the error message.
            context: Optional context explaining when it's required.

        Returns:
            The value if not None.

        Raises:
            NonRetryableError: If value is None.
        """
        if value is None:
            msg = f"{name} is required"
            if context:
                msg = f"{msg} {context}"
            raise NonRetryableError(msg, app_name=self._app_name)
        return value

    def get_app_state(self, key: str) -> Any | None:
        """Get in-memory state for the current app execution.

        Only available inside @task methods.

        Args:
            key: State key to retrieve.

        Returns:
            The stored value, or None if not set.

        Raises:
            RuntimeError: If called outside a @task method.
        """
        execution_id = self._get_current_execution_id()
        with _app_state_lock:
            return _app_state.get(execution_id, {}).get(key)

    def set_app_state(self, key: str, value: Any) -> None:
        """Set in-memory state for the current app execution.

        Only available inside @task methods.

        Args:
            key: State key to store.
            value: Value to store (any Python object).

        Raises:
            RuntimeError: If called outside a @task method.
        """
        execution_id = self._get_current_execution_id()
        with _app_state_lock:
            if execution_id not in _app_state:
                _app_state[execution_id] = {}
            _app_state[execution_id][key] = value

    def _get_current_execution_id(self) -> str:
        """Get the execution ID from current task context."""
        return _get_execution_id_from_task()

    async def run(self, input: Input) -> Output:
        """Execute the App with the given input.

        This is the main entry point for App logic. Implement this method
        with your business logic, or define @entrypoint methods instead.

        IMPORTANT: This method must be deterministic. Do not use:
        - datetime.now() or datetime.utcnow() - use self.now() instead
        - uuid.uuid4() - use self.uuid() instead
        - File I/O, network calls - use @task methods instead

        Args:
            input: The typed input dataclass.

        Returns:
            The typed output dataclass.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement run() or define @entrypoint methods."
        )

    async def call(
        self,
        app_cls: "type[App]",
        input: Input,
        *,
        version: str | None = None,
        task_queue: str | None = None,
    ) -> Output:
        """Call another App from within this App.

        Args:
            app_cls: The App class to call.
            input: The input dataclass for the target App (must extend Input).
            version: Specific version to call, or None for latest.
            task_queue: Task queue where the child app's worker is listening.

        Returns:
            The output from the called App (extends Output).

        Raises:
            AppError: If the call fails.
            RuntimeError: If called outside of run() execution.
        """
        if self._client is None:
            raise AppContextError(
                "App client is only available during run() execution. "
                "Do not call other Apps in __init__ or outside of run()."
            )

        return await self._client.call(
            app_cls,
            input,
            version=version,
            parent_context=self.context,
            task_queue=task_queue,
        )

    @overload
    async def call_by_name(
        self,
        app_name: str,
        input: Input,
        *,
        version: str | None = None,
        output_type: type[TChildOutput],
        task_queue: str | None = None,
    ) -> TChildOutput: ...

    @overload
    async def call_by_name(
        self,
        app_name: str,
        input: Input,
        *,
        version: str | None = None,
        output_type: None = None,
        task_queue: str | None = None,
    ) -> dict[str, Any]: ...

    async def call_by_name(
        self,
        app_name: str,
        input: Input,
        *,
        version: str | None = None,
        output_type: type[Output] | None = None,
        task_queue: str | None = None,
    ) -> "Output | dict[str, Any]":
        """Call another App by name.

        Args:
            app_name: The registered name of the App.
            input: The input dataclass for the target App (must extend Input).
            version: Specific version to call, or None for latest.
            output_type: Optional Output subclass for deserializing the result.
            task_queue: Task queue where the child app's worker is listening.

        Returns:
            The output from the called App.
        """
        if self._client is None:
            raise AppContextError(
                "App client is only available during run() execution."
            )

        return await self._client.call_by_name(
            app_name,
            input,
            version=version,
            parent_context=self.context,
            output_type=output_type,
            task_queue=task_queue,
        )

    def continue_with(self, input: Input) -> Never:
        """Restart this App with new input, preserving correlation context.

        Truncates the current workflow history and restarts execution from the
        beginning with the provided input. Useful for long-running Apps that
        process data incrementally to avoid Temporal history size limits.

        This method does not return — it signals the framework to restart
        execution as a new workflow run with a clean history.

        Args:
            input: The new input to restart with (must extend Input).

        Raises:
            AppContextError: If called outside of run() execution.
        """
        if self._context is None:
            raise AppContextError(
                "continue_with() is only available during run() execution."
            )

        _safe_log(
            "info",
            f"App continuing with new input | app={self._app_name} run_id={self.run_id} correlation_id={self.correlation_id}",
            app_name=self._app_name,
            run_id=self.run_id,
            correlation_id=self.correlation_id,
        )

        workflow.continue_as_new(
            args=[input],
            memo={"correlation_id": self.correlation_id},
        )

    # =========================================================================
    # Framework-provided storage tasks
    # =========================================================================

    @task(timeout_seconds=600, retry_max_attempts=3)
    async def upload(
        self,
        input: UploadInput,
    ) -> UploadOutput:
        """Framework task: upload a local file or directory to the object store.

        Call this from ``run()`` — **not** from inside another ``@task``.
        Wrapping the upload in its own Temporal activity gives it a dedicated
        retry policy and timeout, and Temporal records the result so the upload
        is never re-executed on workflow replay even if the worker is replaced
        mid-run (e.g. a KEDA scale-down event).

        For direct use inside an existing ``@task``, import and call
        :func:`application_sdk.storage.transfer.upload` directly.

        Args:
            input: ``UploadInput`` describing the local file or directory and
                optional destination override.

        Returns:
            ``UploadOutput`` with a durable ``FileReference`` (``is_durable=True``)
            containing both ``local_path`` and ``storage_path``, plus ``file_count``
            indicating the number of files uploaded.

        Example — upload a single file produced by an extraction task::

            async def run(self, input: PipelineInput) -> PipelineOutput:
                extract = await self.extract_data(ExtractInput(source=input.source))
                up = await self.upload(UploadInput(local_path=extract.output_file))
                # up.ref is durable — safe to pass to a task on a different worker
                await self.load_data(LoadInput(ref=up.ref))

        Example — upload an entire output directory::

            up = await self.upload(UploadInput(local_path="/tmp/output/"))
            # up.ref.file_count == number of files in the directory
        """
        from application_sdk.storage.transfer import upload as _upload

        store = self.context.storage
        if store is None:
            raise RuntimeError(
                "No object store configured. "
                "Ensure the deployment has a storage binding or APP_STORAGE_ROOT set."
            )
        run_prefix = f"artifacts/apps/{self._app_name}/workflows/{self.context.run_id}"
        app_prefix = input.tier.upload_prefix(
            run_prefix=run_prefix, app_name=self._app_name
        )
        return await _upload(
            input.local_path,
            input.storage_path,
            storage_subdir=input.storage_subdir,
            skip_if_exists=input.skip_if_exists,
            store=store,
            _app_prefix=app_prefix,
            _tier=input.tier,
        )

    @task(timeout_seconds=600, retry_max_attempts=3)
    async def download(
        self,
        input: DownloadInput,
    ) -> DownloadOutput:
        """Framework task: download a key or prefix from the object store.

        Call this from ``run()`` — **not** from inside another ``@task``.
        Handles both single-file and directory/prefix downloads automatically.
        If ``input.ref`` is provided and ``input.storage_path`` is empty, the
        ref's ``storage_path`` is used as the source.

        For direct use inside an existing ``@task``, import and call
        :func:`application_sdk.storage.transfer.download` directly.

        Args:
            input: ``DownloadInput`` with the store key or prefix to download
                and optional local destination path.

        Returns:
            ``DownloadOutput`` with a fully materialised ``FileReference``
            containing both ``storage_path`` and ``local_path``.

        Example — download a file reference from a previous upload::

            async def run(self, input: InferInput) -> InferOutput:
                dl = await self.download(
                    DownloadInput(storage_path=input.model_ref.storage_path)
                )
                result = await self.run_inference(
                    InferenceInput(model_path=dl.ref.local_path, data=input.data)
                )

        Example — re-materialise an existing FileReference::

            dl = await self.download(DownloadInput(ref=input.model_ref))
        """
        from application_sdk.storage.transfer import download as _download

        store = self.context.storage
        if store is None:
            raise RuntimeError(
                "No object store configured. "
                "Ensure the deployment has a storage binding or APP_STORAGE_ROOT set."
            )

        # Resolve storage_path: explicit field takes precedence over ref.storage_path
        storage_path = input.storage_path
        if not storage_path and input.ref is not None:
            storage_path = input.ref.storage_path or ""

        return await _download(
            storage_path,
            input.local_path,
            skip_if_exists=input.skip_if_exists,
            store=store,
        )

    @task(timeout_seconds=300, retry_max_attempts=3)
    async def cleanup_files(self, input: CleanupInput) -> CleanupOutput:
        """Framework task: clean up local files after a workflow run.

        Removes two categories of local files:

        1. ``FileReference`` local paths tracked during the run (auto-materialised
           or persisted files, including their ``.sha256`` sidecars).
        2. Convention-based temp directories: ``input.extra_paths`` if provided,
           otherwise ``CLEANUP_BASE_PATHS`` / ``TEMPORARY_PATH + build_output_path()``.

        All errors are swallowed per-path so a cleanup failure never fails the
        workflow.

        Call this from ``on_complete()`` (the default implementation does so
        automatically).  Do not call it directly from ``run()``.
        """

        from application_sdk.constants import (
            CLEANUP_BASE_PATHS,
            TEMPORARY_PATH,
            TRACKED_FILE_REFS_KEY,
        )
        from application_sdk.execution._temporal.activity_utils import build_output_path

        path_results: dict[str, bool] = {}

        # 1. Delete tracked FileReference local paths (+ .sha256 sidecars).
        tracked_refs = TaskStateAccessor().get(TRACKED_FILE_REFS_KEY)
        if tracked_refs:
            for ref in tracked_refs:
                if ref.local_path:
                    for p in (ref.local_path, ref.local_path + ".sha256"):
                        try:
                            if os.path.exists(p):
                                if os.path.isdir(p):
                                    shutil.rmtree(p)
                                else:
                                    os.remove(p)
                            path_results[p] = True
                        except Exception:
                            _safe_log(
                                "warning",
                                "Failed to delete local path during cleanup",
                                exc_info=True,
                            )
                            path_results[p] = False

        # 2. Delete convention-based temp directories.
        if input.extra_paths:
            dir_paths: list[str] = input.extra_paths
        elif CLEANUP_BASE_PATHS:
            dir_paths = CLEANUP_BASE_PATHS
        else:
            dir_paths = [os.path.join(TEMPORARY_PATH, build_output_path())]

        for base_path in dir_paths:
            try:
                if os.path.exists(base_path):
                    if os.path.isdir(base_path):
                        shutil.rmtree(base_path)
                    else:
                        os.remove(base_path)
                path_results[base_path] = True
            except Exception:
                _safe_log(
                    "warning",
                    "Failed to delete temp directory during cleanup",
                    exc_info=True,
                )
                path_results[base_path] = False

        return CleanupOutput(path_results=path_results)

    @task(
        timeout_seconds=300,
        retry_max_attempts=1,
        heartbeat_timeout_seconds=None,
        auto_heartbeat_seconds=None,
    )
    async def cleanup_storage(self, input: StorageCleanupInput) -> StorageCleanupOutput:
        """Framework task: delete transient object-store files after a workflow run.

        Deletes two categories of object-store objects:

        1. **Tracked ``TRANSIENT``-tier refs** (always): auto-persisted
           intermediary files (``StorageTier.TRANSIENT``, the default).
           Each key and its ``.sha256`` sidecar are deleted.
           ``RETAINED`` and ``PERSISTENT`` tier refs are skipped.
        2. **Run-scoped prefix** (opt-in via ``input.include_prefix_cleanup``):
           all objects under ``artifacts/apps/{app}/workflows/{wf_id}/{run_id}/``,
           which includes any ``RETAINED``-tier refs from this run.

        Objects under ``persistent-artifacts/`` are never deleted.

        If no object store is configured (local dev), returns immediately with
        zero counts.  Individual delete errors increment ``error_count`` but
        never abort the task.
        """
        import asyncio

        import obstore as obs

        from application_sdk.constants import (
            PROTECTED_STORAGE_PREFIXES,
            TRACKED_FILE_REFS_KEY,
        )
        from application_sdk.execution._temporal.activity_utils import build_output_path
        from application_sdk.storage.ops import _resolve_store, delete

        store = self.context.storage if self._context is not None else None
        if store is None:
            return StorageCleanupOutput()

        resolved = _resolve_store(store)

        deleted = 0
        skipped = 0
        errors = 0

        MAX_CONCURRENT_DELETES = 20
        sem = asyncio.Semaphore(MAX_CONCURRENT_DELETES)

        async def _delete_one(key: str) -> bool:
            async with sem:
                try:
                    await delete(key, store, normalize=False)
                    return True
                except Exception:
                    _safe_log(
                        "warning",
                        "Object store delete failed during cleanup",
                        exc_info=True,
                    )
                    return False

        # 1. Delete tracked transient objects.
        from application_sdk.contracts.types import StorageTier

        tracked_refs = TaskStateAccessor().get(TRACKED_FILE_REFS_KEY)
        if tracked_refs:
            for ref in tracked_refs:
                storage_path: str | None = getattr(ref, "storage_path", None)
                if not storage_path:
                    continue
                if any(storage_path.startswith(p) for p in PROTECTED_STORAGE_PREFIXES):
                    skipped += 1
                    continue
                tier = getattr(ref, "tier", StorageTier.TRANSIENT)
                if tier != StorageTier.TRANSIENT:
                    skipped += 1
                    continue
                if storage_path.endswith("/"):
                    # Directory ref — stream-and-delete sub-keys.
                    for batch in obs.list(resolved, prefix=storage_path):
                        tasks = []
                        for item in batch:
                            key = str(item["path"])
                            if any(
                                key.startswith(p) for p in PROTECTED_STORAGE_PREFIXES
                            ):
                                skipped += 1
                                continue
                            tasks.append(_delete_one(key))
                        results = await asyncio.gather(*tasks)
                        for ok in results:
                            if ok:
                                deleted += 1
                            else:
                                errors += 1
                else:
                    # Single file — delete key and .sha256 sidecar.
                    for key in (storage_path, storage_path + ".sha256"):
                        if await _delete_one(key):
                            deleted += 1
                        else:
                            errors += 1

        # 2. Delete run-scoped prefix (opt-in).
        if input.include_prefix_cleanup:
            prefix = build_output_path() + "/"
            for batch in obs.list(resolved, prefix=prefix):
                tasks = []
                for item in batch:
                    key = str(item["path"])
                    if any(key.startswith(p) for p in PROTECTED_STORAGE_PREFIXES):
                        skipped += 1
                        continue
                    tasks.append(_delete_one(key))
                results = await asyncio.gather(*tasks)
                for ok in results:
                    if ok:
                        deleted += 1
                    else:
                        errors += 1

        return StorageCleanupOutput(
            deleted_count=deleted,
            skipped_count=skipped,
            error_count=errors,
        )

    async def on_complete(self) -> None:
        """Lifecycle hook called after ``run()`` finishes (success or failure).

        The default implementation deletes local files produced during the run
        when cleanup is enabled (``APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR``
        not set to a falsy value).

        Override this method to add custom post-run logic.  Call
        ``await super().on_complete()`` to preserve the default file cleanup::

            async def on_complete(self) -> None:
                await self.send_notification()
                await super().on_complete()
        """
        cleanup_enabled = os.environ.get(
            "APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", "true"
        ).lower() not in ("0", "false", "no")
        if cleanup_enabled:
            import asyncio

            async def _local_cleanup() -> None:
                try:
                    await self.cleanup_files(CleanupInput())
                except Exception:
                    _safe_log("warning", "cleanup_files task failed during on_complete")

            async def _storage_cleanup() -> None:
                try:
                    await self.cleanup_storage(StorageCleanupInput())
                except Exception:
                    _safe_log(
                        "warning", "cleanup_storage task failed during on_complete"
                    )

            await asyncio.gather(_local_cleanup(), _storage_cleanup())

        try:
            from application_sdk.observability.observability import AtlanObservability

            await AtlanObservability.flush_all()
        except Exception:
            _safe_log("warning", "flush_all() failed during on_complete")


# =============================================================================
# Registration helpers
# =============================================================================


def _register_tasks(cls: type, app_name: str) -> None:
    """Register all @task decorated methods for an App class.

    Args:
        cls: The App class.
        app_name: The app's registered name.
    """
    from dataclasses import replace

    from application_sdk.app.task import get_task_metadata, is_task

    task_registry = TaskRegistry.get_instance()

    # Scan the class for @task decorated methods
    for attr_name in dir(cls):
        if attr_name.startswith("_"):
            continue

        attr = getattr(cls, attr_name, None)
        if attr is None:
            continue

        if is_task(attr):
            task_meta = get_task_metadata(attr)
            if task_meta:
                # Create a copy with the app name set
                task_meta_copy = replace(task_meta, app_name=app_name)
                # Register the task
                task_registry.register(app_name, task_meta_copy)


def _scan_entrypoints(cls: type) -> "dict[str, EntryPointMetadata]":
    """Scan a class for @entrypoint-decorated methods.

    Args:
        cls: The App class to scan.

    Returns:
        Dict mapping entry point name to EntryPointMetadata.
    """
    # deferred import: circular dependency (entrypoint imports App)
    from application_sdk.app.entrypoint import (
        EntryPointMetadata,
        get_entrypoint_metadata,
        is_entrypoint,
    )

    entry_points: dict[str, EntryPointMetadata] = {}
    for attr_name in dir(cls):
        if attr_name.startswith("_"):
            continue
        attr = getattr(cls, attr_name, None)
        if attr is None:
            continue
        if is_entrypoint(attr):
            ep_meta = get_entrypoint_metadata(attr)
            if ep_meta is not None:
                entry_points[ep_meta.name] = ep_meta
    return entry_points


def _apply_app_registration(
    cls: "type[App]",
    name: str,
    version: str,
    description: str,
    tags: dict[str, str] | None,
    passthrough_modules: set[str] | None,
    input_type: type[Input],
    output_type: type[Output],
    entry_points: "dict[str, EntryPointMetadata] | None" = None,
) -> None:
    """Register an App class with the AppRegistry.

    Args:
        cls: The App class to register.
        name: App name.
        version: Semantic version string.
        description: Human-readable description.
        tags: Optional tags for categorization.
        passthrough_modules: Modules to pass through sandbox.
        input_type: Input dataclass type.
        output_type: Output dataclass type.
        entry_points: Entry point metadata keyed by entry point name.
    """
    # Mark as registered to prevent duplicate registration
    cls._app_registered = True

    # Set class attributes
    cls._app_name = name
    cls._app_version = version
    cls._input_type = input_type
    cls._output_type = output_type

    # Register with the global app registry
    registry = AppRegistry.get_instance()
    metadata = registry.register(
        name=name,
        version=version,
        app_cls=cls,
        input_type=input_type,
        output_type=output_type,
        description=description,
        tags=tags,
        passthrough_modules=passthrough_modules,
        entry_points=entry_points or {},
        allow_override=True,
    )
    cls._app_metadata = metadata

    # Register all @task decorated methods
    _register_tasks(cls, name)


# Cache generated workflow classes keyed by (app_cls, entry_point_name) so
# generate_workflow_class() is idempotent across repeated calls (e.g. tests
# or worker re-creation) and never registers the same Temporal workflow twice.
_workflow_class_cache: dict[tuple[type, str], type] = {}


def generate_workflow_class(app_cls: "type[App]", ep: "EntryPointMetadata") -> type:
    """Generate a Temporal workflow class for one entry point.

    Creates a @workflow.defn-decorated class whose run() sets up App context,
    then calls the entry point method on a fresh App instance.

    Args:
        app_cls: The App subclass.
        ep: The entry point to generate a workflow class for.

    Returns:
        A Temporal workflow class decorated with @workflow.defn.
    """
    cache_key = (app_cls, ep.name)
    if cache_key in _workflow_class_cache:
        return _workflow_class_cache[cache_key]

    workflow_name = (
        app_cls._app_name if ep.implicit else f"{app_cls._app_name}:{ep.name}"
    )
    entry_method_name = ep.method_name
    input_type = ep.input_type
    output_type = ep.output_type
    app_name = app_cls._app_name
    app_version = app_cls._app_version

    async def _run(self, input_data: Input) -> Output:
        # deferred imports: inside Temporal sandbox (workflow.unsafe.imports_passed_through context)
        from application_sdk.app.client import WorkflowAppClient
        from application_sdk.app.context import AppContext

        start_time = _safe_now()
        run_id = workflow.info().run_id

        try:
            with workflow.unsafe.imports_passed_through():
                from application_sdk.observability.correlation import (
                    get_correlation_context,
                )

            _corr_ctx = get_correlation_context()
            correlation_id = _corr_ctx.correlation_id if _corr_ctx else run_id
        except Exception:
            _safe_log(
                "warning",
                "Failed to read correlation context, falling back to run_id",
                exc_info=True,
            )
            correlation_id = run_id

        context = AppContext(
            app_name=app_name,
            app_version=app_version,
            run_id=run_id,
            correlation_id=correlation_id,
            started_at=start_time,
        )
        app_instance = app_cls()
        app_instance._context = context

        context_data = {"run_id": run_id, "correlation_id": context.correlation_id}
        app_instance._client = WorkflowAppClient(context_data)
        _wrap_instance_tasks(app_instance, context_data)

        _safe_log(
            "info",
            "App started",
            app_name=app_name,
            run_id=str(run_id),
            correlation_id=context.correlation_id,
        )

        try:
            if hasattr(input_data, "_log_summary"):
                input_summary = input_data._log_summary()
                if input_summary:
                    _safe_log(
                        "info",
                        "App input",
                        app_name=app_name,
                        run_id=str(run_id),
                        correlation_id=context.correlation_id,
                        input=input_summary,
                    )
        except Exception:
            _safe_log("warning", "Failed to log input summary")

        try:
            entry_method = getattr(app_instance, entry_method_name)
            result = await entry_method(input_data)
            return cast("Output", result)

        except Exception as e:
            _safe_log(
                "error",
                "App failed",
                app_name=app_name,
                run_id=str(run_id),
                correlation_id=context.correlation_id,
                error_type=type(e).__name__,
            )
            # deferred import: circular dependency
            # Raw Python exceptions (e.g. ValueError raised directly in an
            # entrypoint) must be wrapped in ApplicationError so Temporal
            # treats them as a clean workflow execution failure.  Without this,
            # Temporal sees a non-FailureError and marks the workflow *task*
            # as failed, causing the server to retry the task indefinitely
            # instead of failing the workflow execution.
            # FailureError subclasses (ActivityError, CancelledError, …) are
            # already handled natively by Temporal and must not be rewrapped.
            from temporalio.exceptions import FailureError

            from application_sdk.execution.errors import ApplicationError

            if isinstance(e, FailureError):
                raise
            raise ApplicationError(
                str(e),
                type=type(e).__name__,
                non_retryable=isinstance(e, NonRetryableError),
            ) from e

        finally:
            try:
                await app_instance.on_complete()
            except Exception:
                _safe_log(
                    "warning", "on_complete() hook raised an unexpected exception"
                )

            end_time = _safe_now()
            duration_ms = round((end_time - start_time).total_seconds() * 1000, 2)
            _safe_log(
                "info",
                "App completed",
                app_name=app_name,
                run_id=str(run_id),
                correlation_id=context.correlation_id,
                duration_ms=duration_ms,
            )

            workflow_id = workflow.info().workflow_id
            with _app_state_lock:
                _app_state.pop(workflow_id, None)

            app_instance._context = None
            app_instance._client = None

    safe_name = workflow_name.replace("-", "_").replace(":", "_")
    cls_name = f"_Workflow_{safe_name}"

    # Temporal's _is_unbound_method_on_cls checks:
    #   fn.__qualname__.rsplit(".", 1)[0] == cls.__name__
    # so we must set __qualname__ BEFORE applying @workflow.run.
    _run.__name__ = "run"
    _run.__qualname__ = f"{cls_name}.run"
    _run.__module__ = app_cls.__module__
    _run.__annotations__ = {"input_data": input_type, "return": output_type}

    decorated_run = workflow.run(_run)

    wf_cls = type(cls_name, (), {"run": decorated_run})
    wf_cls.__module__ = app_cls.__module__
    wf_cls.__qualname__ = cls_name

    workflow.defn(name=workflow_name)(wf_cls)

    # Temporal's sandbox runner imports the workflow class by name from its
    # __module__.  Since this class is generated dynamically it's never added
    # to the module's namespace automatically, so we do it here.

    _src_module = sys.modules.get(app_cls.__module__)
    if _src_module is not None:
        setattr(_src_module, cls_name, wf_cls)

    _workflow_class_cache[cache_key] = wf_cls
    return wf_cls


def _wrap_instance_tasks(app_instance: Any, context_data: dict[str, Any]) -> None:
    """Wrap @task methods on an instance to execute as Temporal activities.

    Args:
        app_instance: The app instance.
        context_data: Context dict with run_id and correlation_id.
    """
    from application_sdk.app.task import get_task_metadata, is_task

    for attr_name in dir(app_instance):
        if attr_name.startswith("_"):
            continue

        attr = getattr(type(app_instance), attr_name, None)
        if attr is None:
            continue

        if is_task(attr):
            task_meta = get_task_metadata(attr)
            if task_meta:
                wrapper = _create_task_activity_wrapper(
                    app_instance._app_name,
                    task_meta.name,
                    task_meta.timeout_seconds,
                    task_meta.retry_max_attempts,
                    task_meta.retry_max_interval_seconds,
                    task_meta.output_type,
                    context_data,
                    task_meta.heartbeat_timeout_seconds,
                    task_meta.auto_heartbeat_seconds,
                    task_meta.retry_policy,
                )
                setattr(app_instance, attr_name, wrapper)


def _create_task_activity_wrapper(
    app_name: str,
    task_name: str,
    timeout_seconds: int,
    retry_max_attempts: int,
    retry_max_interval_seconds: int,
    output_type: type,
    context_data: dict[str, Any],
    heartbeat_timeout_seconds: int | None = 60,
    auto_heartbeat_seconds: int | None = 10,
    retry_policy: Any = None,
) -> Any:
    """Create a wrapper that executes a task as a Temporal activity.

    Args:
        app_name: Name of the app.
        task_name: Name of the task (simple name, no prefix).
        timeout_seconds: Activity timeout.
        retry_max_attempts: Maximum retry attempts.
        retry_max_interval_seconds: Maximum interval between retries.
        output_type: The typed output class for deserialization.
        context_data: Context dict with run_id and correlation_id.
        heartbeat_timeout_seconds: Heartbeat timeout. None disables.
        auto_heartbeat_seconds: Auto-heartbeat interval. None disables.
        retry_policy: Full retry policy (overrides max_attempts/interval if set).

    Returns:
        Async function that executes the task as an activity.
    """
    from datetime import timedelta

    from application_sdk.execution.retry import RetryPolicy as _RP
    from application_sdk.execution.retry import _to_temporal_retry_policy

    with workflow.unsafe.imports_passed_through():
        from application_sdk.execution._temporal.activities import TaskContext

    # Build the Temporal RetryPolicy once (not per invocation)
    if retry_policy is not None:
        temporal_retry_policy = _to_temporal_retry_policy(retry_policy)
    else:
        temporal_retry_policy = _to_temporal_retry_policy(
            _RP(
                max_attempts=retry_max_attempts,
                max_interval=timedelta(seconds=retry_max_interval_seconds),
            )
        )

    async def wrapper(input_data: Input) -> Output:
        # Create the task context (metadata for the activity)
        task_context = TaskContext(
            app_name=app_name,
            task_name=task_name,
            run_id=context_data.get("run_id", ""),
            heartbeat_timeout_seconds=heartbeat_timeout_seconds,
            auto_heartbeat_seconds=auto_heartbeat_seconds,
        )

        # Build heartbeat timeout if enabled
        heartbeat_timeout = (
            timedelta(seconds=heartbeat_timeout_seconds)
            if heartbeat_timeout_seconds is not None
            else None
        )

        # Extract summary from input for Temporal UI display
        summary = input_data.summary() if hasattr(input_data, "summary") else None

        # Execute as activity - pass result_type for proper deserialization
        result: Output = await workflow.execute_activity(
            f"{app_name}:{task_name}",
            args=[task_context, input_data],
            start_to_close_timeout=timedelta(seconds=timeout_seconds),
            heartbeat_timeout=heartbeat_timeout,
            retry_policy=temporal_retry_policy,
            result_type=output_type,
            summary=summary,
        )

        return result

    return wrapper


# Keep FileReference accessible via base module for convenience
__all__ = [
    "App",
    "AppError",
    "AppStateAccessor",
    "NonRetryableError",
    "PersistentStateAccessor",
    "TaskStateAccessor",
    "_app_state",
    "_app_state_lock",
    "_pascal_to_kebab",
    "_safe_log",
    "_safe_now",
    "_safe_uuid",
    "_apply_app_registration",
    "_create_task_activity_wrapper",
    "_register_tasks",
    "_scan_entrypoints",
    "_wrap_instance_tasks",
    "generate_workflow_class",
    "task",
    "FileReference",
]
