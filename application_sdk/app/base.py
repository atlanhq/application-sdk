"""App base class and decorators."""

import importlib.metadata
import inspect
import re
import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast, get_type_hints, overload
from uuid import UUID

from temporalio import workflow

from application_sdk.app.context import AppContext, TaskExecutionContext
from application_sdk.app.registry import AppMetadata, AppRegistry, TaskRegistry
from application_sdk.app.task import task
from application_sdk.contracts.base import HeartbeatDetails, Input, Output
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

    Passes structured attrs via extra={"_structlog_attrs": attrs} so the
    ProcessorFormatter pipeline can merge them into the JSON output and
    forward them to OTel — no direct OTel import needed here.
    """
    log_method = getattr(workflow.logger, level)
    if attrs:
        log_method(message, extra={"_structlog_attrs": attrs})
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

    Example:
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

    Example:
        class MyApp(App):

            @task
            async def fetch_data(self, input: FetchInput) -> FetchOutput:
                # Tasks can do I/O - no restrictions
                return FetchOutput(data=await http_client.get(input.url).json())

            async def run(self, input: MyInput) -> MyOutput:
                # run() is deterministic - only call tasks and other apps
                result = await self.fetch_data(FetchInput(url=input.url))
                return MyOutput(data=result.data)

    Override class attributes when needed:
        class CsvPipeline(App):
            name = "csv-ingest-v2"  # Override derived name
            version = "2.0.0"       # Override default version

            async def run(self, input: PipelineInput) -> PipelineOutput:
                ...

    Name derivation from class name:
        - Greeter -> greeter
        - CsvPipeline -> csv-pipeline
        - MyAwesomeApp -> my-awesome-app
        - HTTPHandler -> http-handler
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
        2. Infers input/output types from run() signature
        3. Applies Temporal decorators
        4. Registers the app with the AppRegistry

        Skip registration if:
        - The class is abstract (has abstract methods)
        - The class doesn't have a run() method
        - The class was already registered
        """
        super().__init_subclass__(**kwargs)

        # Skip if already registered
        if getattr(cls, "_app_registered", False):
            return

        # Skip abstract classes - check if run() is still abstract
        if inspect.isabstract(cls):
            return

        # Skip if run() is not implemented (still abstract)
        run_method = getattr(cls, "run", None)
        if run_method is None:
            return

        # Check if run() is the abstract one from App base class
        if getattr(run_method, "__isabstractmethod__", False):
            return

        # Try to infer types from run() signature
        try:
            hints = get_type_hints(cls.run)
        except Exception:
            # If we can't get type hints, skip auto-registration
            return

        input_type = hints.get("input")
        output_type = hints.get("return")

        # Skip if types aren't available
        if input_type is None or output_type is None:
            return

        # Check if types extend Input/Output base classes
        if not (isinstance(input_type, type) and issubclass(input_type, Input)):
            return
        if not (isinstance(output_type, type) and issubclass(output_type, Output)):
            return

        # Derive name from class name if not specified
        app_name = cls.name or _pascal_to_kebab(cls.__name__)

        # Apply the registration logic
        _apply_app_registration(
            cls=cls,
            name=app_name,
            version=cls.version,
            description=cls.description,
            tags=cls.tags,
            passthrough_modules=cls.passthrough_modules,
            input_type=input_type,
            output_type=output_type,
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
            Result of func(*args, **kwargs).

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

    @abstractmethod
    async def run(self, input: Input) -> Output:
        """Execute the App with the given input.

        This is the main entry point for App logic. Implement this method
        with your business logic.

        IMPORTANT: This method must be deterministic. Do not use:
        - datetime.now() or datetime.utcnow() - use self.now() instead
        - uuid.uuid4() - use self.uuid() instead
        - File I/O, network calls - use @task methods instead

        Args:
            input: The typed input dataclass.

        Returns:
            The typed output dataclass.
        """
        ...

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
        app_prefix = f"artifacts/apps/{self._app_name}/workflows/{self.context.run_id}"
        return await _upload(
            input.local_path,
            input.storage_path,
            skip_if_exists=input.skip_if_exists,
            store=store,
            _app_prefix=app_prefix,
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


def _apply_app_registration(
    cls: "type[App]",
    name: str,
    version: str,
    description: str,
    tags: dict[str, str] | None,
    passthrough_modules: set[str] | None,
    input_type: type[Input],
    output_type: type[Output],
) -> None:
    """Apply Temporal decorators and register an App class.

    Args:
        cls: The App class to register.
        name: App name (for Temporal workflow).
        version: Semantic version string.
        description: Human-readable description.
        tags: Optional tags for categorization.
        passthrough_modules: Modules to pass through sandbox.
        input_type: Input dataclass type.
        output_type: Output dataclass type.
    """
    # Mark as registered to prevent duplicate registration
    cls._app_registered = True

    # Set class attributes
    cls._app_name = name
    cls._app_version = version

    # Store the original run method and input/output types for wrapper
    original_run = cls.run
    cls._original_run = original_run
    cls._input_type = input_type
    cls._output_type = output_type

    # Create wrapper run method for Temporal execution
    async def workflow_run_wrapper(self: Any, input_data: Input) -> Output:
        """Temporal workflow entry point."""
        from application_sdk.app.client import WorkflowAppClient
        from application_sdk.app.context import AppContext

        # Get deterministic time for Temporal replay
        start_time = _safe_now()

        # Use Temporal's workflow run ID as the single source of truth.
        run_id = workflow.info().run_id

        # Try to get correlation ID from context, fallback to run_id
        try:
            with workflow.unsafe.imports_passed_through():
                from application_sdk.observability.context import (
                    get_correlation_context,
                )
            _corr_ctx = get_correlation_context()
            correlation_id = _corr_ctx.correlation_id if _corr_ctx else run_id
        except Exception:
            correlation_id = run_id

        context = AppContext(
            app_name=self._app_name,
            app_version=self._app_version,
            run_id=run_id,
            correlation_id=correlation_id,
            started_at=start_time,
        )
        self._context = context

        # Set up client for child app calls and wrap tasks
        context_data = {
            "run_id": run_id,
            "correlation_id": context.correlation_id,
        }
        self._client = WorkflowAppClient(context_data)
        _wrap_instance_tasks(self, context_data)

        # Log app start
        _safe_log(
            "info",
            f"App started | app={self._app_name} run_id={run_id} correlation_id={context.correlation_id}",
            app_name=self._app_name,
            run_id=str(run_id),
            correlation_id=context.correlation_id,
        )

        # Log input summary for debugging
        try:
            if hasattr(input_data, "_log_summary"):
                input_summary = input_data._log_summary()
                if input_summary:
                    _safe_log(
                        "info",
                        f"App input | app={self._app_name} run_id={run_id}",
                        app_name=self._app_name,
                        run_id=str(run_id),
                        correlation_id=context.correlation_id,
                        input=input_summary,
                    )
        except Exception:
            pass  # Never fail on logging

        try:
            result = await self._original_run(input_data)
            return cast("Output", result)

        except Exception as e:
            err_parts = f"App failed | app={self._app_name} run_id={run_id} error={e} error_type={type(e).__name__}"
            _safe_log(
                "error",
                err_parts,
                app_name=self._app_name,
                run_id=str(run_id),
                correlation_id=context.correlation_id,
                error_type=type(e).__name__,
            )

            if isinstance(e, NonRetryableError):
                from temporalio.exceptions import ApplicationError

                raise ApplicationError(
                    str(e),
                    type=type(e).__name__,
                    non_retryable=True,
                ) from e

            raise

        finally:
            end_time = _safe_now()
            duration_ms = round((end_time - start_time).total_seconds() * 1000, 2)

            _safe_log(
                "info",
                f"App completed | app={self._app_name} run_id={run_id} duration_ms={duration_ms}",
                app_name=self._app_name,
                run_id=str(run_id),
                correlation_id=context.correlation_id,
                duration_ms=duration_ms,
            )

            # Clean up app-scoped state to prevent memory leaks
            workflow_id = workflow.info().workflow_id
            with _app_state_lock:
                _app_state.pop(workflow_id, None)

            self._context = None
            self._client = None

    # Set proper name, qualname, and module for Temporal's validation
    workflow_run_wrapper.__name__ = "run"
    workflow_run_wrapper.__qualname__ = f"{cls.__name__}.run"
    workflow_run_wrapper.__module__ = cls.__module__

    # Set type annotations for Temporal deserialization
    workflow_run_wrapper.__annotations__ = {
        "input_data": input_type,
        "return": output_type,
    }

    # Apply @workflow.run to the wrapper and assign to class
    decorated_run = workflow.run(workflow_run_wrapper)
    cls.run = decorated_run

    # Apply @workflow.defn with the app name
    workflow.defn(name=name)(cls)

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
        allow_override=True,
    )
    cls._app_metadata = metadata

    # Register all @task decorated methods
    _register_tasks(cls, name)


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

    from temporalio.common import RetryPolicy

    with workflow.unsafe.imports_passed_through():
        from application_sdk.execution._temporal.activities import TaskContext

    # Build the Temporal RetryPolicy once (not per invocation)
    if retry_policy is not None:
        temporal_retry_policy = RetryPolicy(
            maximum_attempts=retry_policy.max_attempts,
            initial_interval=retry_policy.initial_interval,
            maximum_interval=retry_policy.max_interval,
            backoff_coefficient=retry_policy.backoff_coefficient,
            non_retryable_error_types=list(retry_policy.non_retryable_errors),
        )
    else:
        temporal_retry_policy = RetryPolicy(
            maximum_attempts=retry_max_attempts,
            maximum_interval=timedelta(seconds=retry_max_interval_seconds),
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
            task_name,
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
    "_wrap_instance_tasks",
    "task",
    "FileReference",
]
