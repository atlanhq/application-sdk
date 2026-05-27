"""Task decorator for defining activities within Apps.

Tasks are operations with external side effects that need to be
executed as Temporal activities for durability and retry support.

Like Apps, Tasks follow the single-dataclass contract pattern:
- One Input dataclass parameter (extending Input base class)
- One Output dataclass return value (extending Output base class)

This ensures type safety, proper serialization, and backwards compatibility.

Tasks support heartbeating for long-running operations:
- Auto-heartbeating sends periodic signals to Temporal (default: every 10s)
- Manual heartbeating allows progress tracking for resume on retry
- If heartbeats stop, Temporal restarts the activity (default: after 60s)
"""

import inspect
import logging
import os
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, TypeVar, cast, get_type_hints, overload

from application_sdk.contracts.base import Input, Output
from application_sdk.errors import CONTRACT_VALIDATION, ErrorCode
from application_sdk.errors.leaves import InvalidInputError

if TYPE_CHECKING:
    from application_sdk.execution.retry import RetryPolicy

F = TypeVar("F", bound=Callable[..., Any])

logger = logging.getLogger(__name__)

# Sentinel for "use default" - allows None to mean "disable"
_USE_DEFAULT = object()


def _env_int(key: str, default: int) -> int:
    """Read an int env var, returning ``default`` when unset, empty, or unparsable."""
    val = os.environ.get(key)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        logger.warning(
            "Ignoring non-integer env var %s=%r; falling back to default %d",
            key,
            val,
            default,
        )
        return default


# Env-var-driven defaults for @task timeouts. Read once at import time so the
# value is stable for the process lifetime (same pattern as constants.py).
# Apps that need a different per-task value pass it explicitly to @task().
_DEFAULT_HEARTBEAT_TIMEOUT_SECONDS: int = _env_int(
    "ATLAN_HEARTBEAT_TIMEOUT_SECONDS", 60
)
_DEFAULT_TIMEOUT_SECONDS: int = _env_int("ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS", 600)

# Type alias for methods with single Input param returning Output
TaskMethod = Callable[..., Any]


@dataclass(kw_only=True)
class UnresolvableTaskAnnotationsError(InvalidInputError):
    """Task has string annotations that cannot be resolved at decoration time."""

    code: ClassVar[str] = "INVALID_INPUT_TASK_UNRESOLVABLE_ANNOTATIONS"
    field: str | None = "annotations"


class TaskContractError(InvalidInputError):
    """Deprecated: use ``application_sdk.errors.InvalidInputError`` — removed in v4.0."""

    code: ClassVar[str] = "TASK_CONTRACT"

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        warnings.warn(
            "TaskContractError is deprecated; use application_sdk.errors.InvalidInputError "
            "— will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        InvalidInputError.__init__(self, message=message)
        self._legacy_error_code = error_code or CONTRACT_VALIDATION

    @property
    def error_code(self) -> ErrorCode:
        return self._legacy_error_code

    def __str__(self) -> str:
        return f"[{self._legacy_error_code}] {self.message}"


@dataclass
class TaskMetadata:
    """Metadata about a registered task.

    Tasks are private to their parent App and become
    Temporal activities with simple names (just the task name).
    """

    name: str
    """Name of the task (method name by default)."""

    func: Callable[..., Any]
    """The original function/method."""

    input_type: type[Input]
    """The Input dataclass type for this task."""

    output_type: type[Output]
    """The Output dataclass type for this task."""

    app_name: str = ""
    """Parent app name (set by @app decorator)."""

    description: str = ""
    """Human-readable description."""

    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS
    """Default timeout for this task. Defaults to ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS
    env var, or 600s (10 minutes) if unset."""

    retry_policy: "RetryPolicy | None" = field(default=None, compare=False)
    """Full retry policy for this task. When provided, takes precedence over
    retry_max_attempts and retry_max_interval_seconds."""

    retry_max_attempts: int = 3
    """Maximum retry attempts for this task. Ignored when retry_policy is set."""

    retry_max_interval_seconds: int = 30
    """Maximum interval between retries in seconds. Caps exponential backoff
    to prevent very long waits between retries. Default: 30 seconds.
    Ignored when retry_policy is set."""

    heartbeat_timeout_seconds: int | None = _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
    """Heartbeat timeout in seconds. If no heartbeat is received within this
    window, Temporal will consider the activity dead and restart it.
    Set to None to disable heartbeating entirely (legacy behavior).
    Defaults to ATLAN_HEARTBEAT_TIMEOUT_SECONDS env var, or 60 seconds if unset."""

    auto_heartbeat_seconds: int | None = 10
    """Auto-heartbeat interval in seconds. The framework will automatically
    send heartbeats at this interval in a background task.
    Set to None to disable auto-heartbeating (use manual heartbeats only).
    Should be less than heartbeat_timeout_seconds (recommended: 1/6 of timeout).
    Default: 10 seconds."""


def _validate_task_signature(
    fn: Callable[..., Any],
) -> tuple[type[Input], type[Output]]:
    """Validate and extract Input/Output types from a task method.

    Tasks must follow the single-dataclass contract pattern:
    - Exactly one parameter (besides self) extending Input
    - Return type extending Output

    Args:
        fn: The task function to validate.

    Returns:
        Tuple of (input_type, output_type).

    Raises:
        TaskContractError: If the signature is invalid.
    """
    # Get function name safely (Callable doesn't guarantee __name__)
    fn_name = getattr(fn, "__name__", repr(fn))

    # Get function signature
    sig = inspect.signature(fn)
    params = list(sig.parameters.values())

    # Remove 'self' parameter if present (method)
    if params and params[0].name == "self":
        params = params[1:]

    # Must have exactly one parameter
    if len(params) != 1:
        raise TaskContractError(
            f"Task '{fn_name}' must have exactly one parameter (extending Input), "
            f"got {len(params)} parameters. "
            f"Wrap multiple values in a single Input dataclass."
        )

    # Get type hints.
    # get_type_hints() resolves string annotations (from 'from __future__ import
    # annotations' or explicit string literals) using the function's module globals.
    # Fall back to fn.__annotations__ directly when that resolution fails — this
    # handles the common case where Input/Output types are locally-scoped (e.g. inside
    # a test function) and were never string-ified because 'from __future__' was NOT
    # used; in that case __annotations__ already holds the real type objects.
    # If the annotations are strings that cannot be resolved, raise a clear error.
    try:
        hints = get_type_hints(fn)
    except NameError:
        raw: dict[str, Any] = getattr(fn, "__annotations__", {})
        unresolvable = [k for k, v in raw.items() if isinstance(v, str)]
        if unresolvable:
            raise UnresolvableTaskAnnotationsError(
                message=(
                    f"Task '{fn_name}' has unresolvable annotations for {unresolvable}. "
                    "This usually happens when 'from __future__ import annotations' is "
                    "used alongside Input/Output types that are not defined at module "
                    "level. Move the type definitions to module scope (before the App "
                    "class) or remove 'from __future__ import annotations'."
                ),
            ) from None
        hints = raw

    # Validate input type
    param = params[0]
    input_type = hints.get(param.name)
    if input_type is None:
        raise TaskContractError(
            f"Task '{fn_name}' parameter '{param.name}' must have a type annotation "
            f"extending Input."
        )

    # Check input extends Input base class
    if not (isinstance(input_type, type) and issubclass(input_type, Input)):
        raise TaskContractError(
            f"Task '{fn_name}' parameter '{param.name}' must extend Input base class, "
            f"got {input_type}. Define a dataclass that extends Input."
        )

    # Validate return type
    output_type = hints.get("return")
    if output_type is None:
        raise TaskContractError(
            f"Task '{fn_name}' must have a return type annotation extending Output."
        )

    # Check output extends Output base class
    if not (isinstance(output_type, type) and issubclass(output_type, Output)):
        raise TaskContractError(
            f"Task '{fn_name}' return type must extend Output base class, "
            f"got {output_type}. Define a dataclass that extends Output."
        )

    return input_type, output_type


@overload
def task(func: F) -> F: ...


@overload
def task(
    func: None = None,
    *,
    name: str | None = None,
    description: str = "",
    timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
    retry_policy: "RetryPolicy | None" = None,
    retry_max_attempts: int = 3,
    retry_max_interval_seconds: int = 30,
    heartbeat_timeout_seconds: int | None | object = _USE_DEFAULT,
    auto_heartbeat_seconds: int | None | object = _USE_DEFAULT,
) -> Callable[[F], F]: ...


def task(
    func: F | None = None,
    *,
    name: str | None = None,
    description: str = "",
    timeout_seconds: int | object = _USE_DEFAULT,
    retry_policy: "RetryPolicy | None" = None,
    retry_max_attempts: int = 3,
    retry_max_interval_seconds: int = 30,
    heartbeat_timeout_seconds: int | None | object = _USE_DEFAULT,
    auto_heartbeat_seconds: int | None | object = _USE_DEFAULT,
) -> F | Callable[[F], F]:
    """Decorator to mark a method as a task (Temporal activity).

    Tasks follow the single-dataclass contract pattern (like Apps):
    - Exactly one Input parameter (dataclass extending Input)
    - Exactly one Output return type (dataclass extending Output)

    This ensures type safety, proper serialization, and backwards compatibility.

    Tasks are PRIVATE to the app and cannot be called from other apps.
    They are only callable via `self.task_name()` within the app's methods.

    Each task becomes a distinct named Temporal activity for observability.

    Heartbeating:
        Tasks support heartbeating for long-running operations. By default,
        the framework sends heartbeats every 10 seconds, and Temporal will
        restart the activity if no heartbeat is received for 60 seconds.

        IMPORTANT: Auto-heartbeats only work when the event loop yields.
        For blocking operations (requests.get, file I/O, pandas operations),
        use self.task_context.run_in_thread() to keep heartbeats alive.

    Example::

        @dataclass
        class FetchInput(Input):
            endpoint: str
            timeout: int = 30

        @dataclass
        class FetchOutput(Output):
            data: dict[str, Any]
            status_code: int

        class MyPipeline(App):

            @task
            async def read_from_api(self, input: FetchInput) -> FetchOutput:
                '''Fetch data from external API.'''
                response = await http_client.get(input.endpoint)
                return FetchOutput(data=response.json(), status_code=response.status)

            @task(timeout_seconds=1800)  # 30 min timeout, uses default heartbeat
            async def write_to_database(self, input: WriteInput) -> WriteOutput:
                '''Write records to database.'''
                count = await db.bulk_insert(input.records)
                return WriteOutput(count=count)

            async def run(self, input: PipelineInput) -> PipelineOutput:
                fetch_result = await self.read_from_api(
                    FetchInput(endpoint=input.endpoint)
                )
                return PipelineOutput(count=fetch_result.status_code)

    Args:
        func: The function to decorate (when used without parentheses).
        name: Override the task name (defaults to function name).
        description: Human-readable description.
        timeout_seconds: Activity timeout in seconds. Defaults to
            ``ATLAN_START_TO_CLOSE_TIMEOUT_SECONDS`` env var, or 600 s (10 min)
            if unset. Explicit values take precedence over the env var.
        retry_policy: Full retry policy. When provided, takes precedence over
            retry_max_attempts and retry_max_interval_seconds.
        retry_max_attempts: Maximum retry attempts (default 3). Ignored when
            retry_policy is provided.
        retry_max_interval_seconds: Maximum interval between retries in seconds.
            Caps exponential backoff to prevent very long waits. Default: 30 seconds.
            Ignored when retry_policy is provided.
        heartbeat_timeout_seconds: Heartbeat timeout in seconds — if no heartbeat
            is received within this window, Temporal restarts the activity. Set to
            None to disable heartbeating entirely. Defaults to
            ``ATLAN_HEARTBEAT_TIMEOUT_SECONDS`` env var, or 60 s if unset.
            Explicit values take precedence over the env var.
        auto_heartbeat_seconds: Auto-heartbeat interval - framework sends
            heartbeats at this rate in a background task. Set to None to disable
            auto-heartbeating (manual only). Default: 10 seconds (~1/6 of timeout).

    Returns:
        The decorated function with task metadata attached.

    Raises:
        TaskContractError: If the method doesn't follow the contract pattern.
    """
    # Resolve sentinel values to defaults — evaluated at decoration time so that
    # process-level env-var overrides (e.g. ATLAN_HEARTBEAT_TIMEOUT_SECONDS) are
    # picked up even when the module-level constants were patched after import.
    resolved_timeout: int = (
        _DEFAULT_TIMEOUT_SECONDS
        if timeout_seconds is _USE_DEFAULT
        else cast("int", timeout_seconds)
    )
    resolved_heartbeat_timeout: int | None = (
        _DEFAULT_HEARTBEAT_TIMEOUT_SECONDS
        if heartbeat_timeout_seconds is _USE_DEFAULT
        else cast("int | None", heartbeat_timeout_seconds)
    )
    resolved_auto_heartbeat: int | None = (
        10
        if auto_heartbeat_seconds is _USE_DEFAULT
        else cast("int | None", auto_heartbeat_seconds)
    )

    def decorator(fn: F) -> F:
        task_name = name or getattr(fn, "__name__", repr(fn))

        # Validate signature and extract types
        input_type, output_type = _validate_task_signature(fn)

        # Store metadata on the function
        fn._task_metadata = TaskMetadata(  # type: ignore[attr-defined]
            name=task_name,
            func=fn,
            input_type=input_type,
            output_type=output_type,
            app_name="",  # Will be set by App registration
            description=description or fn.__doc__ or "",
            timeout_seconds=resolved_timeout,
            retry_policy=retry_policy,
            retry_max_attempts=retry_max_attempts,
            retry_max_interval_seconds=retry_max_interval_seconds,
            heartbeat_timeout_seconds=resolved_heartbeat_timeout,
            auto_heartbeat_seconds=resolved_auto_heartbeat,
        )

        return fn

    # Support both @task and @task() syntax
    if func is not None:
        return decorator(func)
    return decorator


def is_task(obj: Any) -> bool:
    """Check if an object is decorated with @task."""
    return hasattr(obj, "_task_metadata")


def get_task_metadata(obj: Any) -> TaskMetadata | None:
    """Get task metadata from a decorated function."""
    return getattr(obj, "_task_metadata", None)
