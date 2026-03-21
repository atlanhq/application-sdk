"""Execution context for Apps."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import uuid4

from application_sdk.contracts.base import HeartbeatDetails

if TYPE_CHECKING:
    from obstore.store import ObjectStore

    from application_sdk.credentials.ref import CredentialRef
    from application_sdk.credentials.types import Credential
    from application_sdk.execution.heartbeat import HeartbeatController
    from application_sdk.infrastructure.secrets import SecretStore
    from application_sdk.infrastructure.state import StateStore

T = TypeVar("T")
HT = TypeVar("HT", bound=HeartbeatDetails)


def _utc_now() -> datetime:
    """Return current UTC time (timezone-aware)."""
    return datetime.now(UTC)


def _is_in_workflow() -> bool:
    """Check if we're in a Temporal workflow context.

    Returns True only inside workflow code, False in activities or elsewhere.
    Reads from the ExecutionContext ContextVar — no Temporal import needed.
    """
    from application_sdk.observability.context import get_execution_context

    return get_execution_context().execution_type == "workflow"


def _is_atlan_logger(obj: Any) -> bool:
    """Check if a logger object is our AtlanLoggerAdapter (supports arbitrary kwargs).

    Uses duck-typing to avoid importing AtlanLoggerAdapter here — which could
    trigger Temporal sandbox restrictions if called from workflow context.

    Stdlib logging.Logger does NOT have these attributes; arbitrary kwargs passed
    to it will raise TypeError ('unexpected keyword argument').
    """
    return hasattr(obj, "process") and hasattr(obj, "logger_name")


class _WorkflowSafeLogger:
    """A logger wrapper that works in both workflow and activity contexts.

    In workflow context: Uses workflow.logger (deterministic).
    In activity context: Uses loguru (activities run outside workflow event loop).
    """

    def __init__(
        self,
        name: str,
        app_name: str,
        run_id: str,
        correlation_id: str,
    ) -> None:
        self._name = name
        self._context = {
            "app_name": app_name,
            "run_id": run_id,
            "correlation_id": correlation_id,
        }
        self._structlog_logger: Any = None

    def _get_structlog_logger(self) -> Any:
        """Get or create the fallback logger (for activity/non-workflow use)."""
        if self._structlog_logger is None:
            from temporalio import workflow as _workflow

            with _workflow.unsafe.imports_passed_through():
                from loguru import logger as _loguru_logger
            self._structlog_logger = _loguru_logger.bind(**self._context)
        return self._structlog_logger

    def _log(self, level: str, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a message at the specified level.

        Supports both printf-style positional args and keyword context:
            self.logger.info("Processed %d records", count)
            self.logger.info("Done", records=count)

        Printf-style args are pre-formatted before being passed to loguru, which
        uses {} formatting natively. This prevents silent data loss when %s-style
        args are used (common in AI-generated code and stdlib-style logging).
        """
        # Pre-format printf-style args. Try %-substitution; fall through so {}
        # style args are still handled by loguru.
        if args:
            try:
                message = message % args
                args = ()
            except (TypeError, ValueError):
                pass  # {} style or mismatch — loguru will handle it

        if _is_in_workflow():
            from temporalio import workflow as _workflow

            wf_logger = _workflow.logger
            log_method = getattr(wf_logger, level)

            if _is_atlan_logger(wf_logger):
                # AtlanLoggerAdapter: supports arbitrary kwargs natively via
                # process() → bind(). Pass context + user kwargs as flat fields.
                merged = {**self._context, **kwargs}
                log_method(message, *args, **merged)
            else:
                # Stdlib logger fallback (e.g., Temporal's default workflow.logger
                # before the events interceptor module has been imported).
                # Stdlib Logger._log() only accepts exc_info/extra/stack_info/
                # stacklevel — arbitrary kwargs cause TypeError. Pack them safely.
                exc_info = kwargs.pop("exc_info", False)
                extra = {**self._context, **kwargs}
                if exc_info:
                    log_method(message, *args, exc_info=exc_info, extra=extra)
                else:
                    log_method(message, *args, extra=extra)
        else:
            # In activity or elsewhere: use loguru
            logger = self._get_structlog_logger()
            log_method = getattr(logger, level)
            log_method(message, *args, **kwargs)

    def debug(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a debug message."""
        self._log("debug", message, *args, **kwargs)

    def info(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an info message."""
        self._log("info", message, *args, **kwargs)

    def warning(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log a warning message."""
        self._log("warning", message, *args, **kwargs)

    def error(self, message: str, *args: Any, **kwargs: Any) -> None:
        """Log an error message."""
        self._log("error", message, *args, **kwargs)

    def bind(self, **kwargs: Any) -> "_WorkflowSafeLogger":
        """Create a new logger with additional context bound."""
        new_logger = _WorkflowSafeLogger(
            self._name,
            self._context["app_name"],
            self._context["run_id"],
            self._context["correlation_id"],
        )
        new_logger._context = {**self._context, **kwargs}
        return new_logger


@dataclass
class AppMetadata:
    """Typed metadata for App execution context.

    Provides structured metadata storage instead of arbitrary dict[str, Any].
    """

    tags: list[str] = field(default_factory=list)
    """Tags for categorizing or filtering app executions."""

    properties: dict[str, str] = field(default_factory=dict)
    """String key-value properties for custom metadata."""


@dataclass
class AppContext:
    """Execution context passed to Apps during execution.

    Provides:
    - Run identification (run_id, app_name, correlation_id)
    - Access to infrastructure (state, secrets) via abstractions
    - Observability hooks (logging, tracing)
    - Cancellation checking

    This context is created by the execution layer and passed to Apps.
    Apps should not create contexts directly.
    """

    app_name: str
    app_version: str
    run_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default="")
    parent_run_id: str | None = None
    started_at: datetime = field(default_factory=_utc_now)
    metadata: AppMetadata = field(default_factory=AppMetadata)
    execution_id_prefix: str = field(default="")

    # These are set by the execution layer, not by users
    _state_store: "StateStore | None" = field(default=None, repr=False)
    _secret_store: "SecretStore | None" = field(default=None, repr=False)
    _storage: "ObjectStore | None" = field(default=None, repr=False)
    _logger: Any = field(default=None, repr=False)
    _cancelled: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if not self.correlation_id:
            self.correlation_id = str(self.run_id)

    @property
    def run_id_str(self) -> str:
        """Run ID as string (alias for run_id, kept for compatibility)."""
        return self.run_id

    @property
    def logger(self) -> Any:
        """Get a logger bound to this app context.

        The logger automatically includes app_name, run_id, and correlation_id
        in all log messages.

        This logger is safe to use in both Temporal workflows and local execution:
        - In Temporal: Uses workflow.logger (deterministic, no OTel imports)
        - Outside Temporal: Uses loguru with full OTel integration

        Returns:
            Bound logger instance that supports debug/info/warning/error methods.
        """
        if self._logger is None:
            self._logger = _WorkflowSafeLogger(
                name=f"app.{self.app_name}",
                app_name=self.app_name,
                run_id=self.run_id_str,
                correlation_id=self.correlation_id,
            )
        return self._logger

    def is_cancelled(self) -> bool:
        """Check if execution has been cancelled.

        Apps should check this periodically during long-running operations
        and exit gracefully if True.
        """
        return self._cancelled

    async def save_state(self, key: str, value: dict[str, Any]) -> None:
        """Save state to the state store.

        Args:
            key: State key (will be namespaced to this app/run).
            value: State data to save.

        Raises:
            RuntimeError: If no state store is configured.
        """
        if self._state_store is None:
            raise RuntimeError("No state store configured")
        namespaced_key = f"{self.app_name}:{self.run_id}:{key}"
        await self._state_store.save(namespaced_key, value)

    async def load_state(self, key: str) -> dict[str, Any] | None:
        """Load state from the state store.

        Args:
            key: State key (will be namespaced to this app/run).

        Returns:
            The saved state or None if not found.

        Raises:
            RuntimeError: If no state store is configured.
        """
        if self._state_store is None:
            raise RuntimeError("No state store configured")
        namespaced_key = f"{self.app_name}:{self.run_id}:{key}"
        return await self._state_store.load(namespaced_key)

    async def get_secret(self, name: str) -> str:
        """Get a secret by name from the secret store.

        Args:
            name: Secret name.

        Returns:
            The secret value.

        Raises:
            RuntimeError: If no secret store is configured.
        """
        if self._secret_store is None:
            raise RuntimeError("No secret store configured")
        return await self._secret_store.get(name)

    async def get_secret_optional(self, name: str) -> str | None:
        """Get a secret by name, returning None if not found or not configured.

        Args:
            name: Secret name.

        Returns:
            The secret value, or None if not found or not configured.
        """
        if self._secret_store is None:
            return None
        return await self._secret_store.get_optional(name)

    @property
    def storage(self) -> "ObjectStore | None":
        """Object store for this context, or ``None`` if not configured.

        Use with the streaming storage API::

            from application_sdk.storage import upload_file, download_file

            sha256 = await upload_file("output/result.json", local_path, self.context.storage)
            await download_file("input/data.parquet", local_path, self.context.storage)
        """
        return self._storage

    def log_debug(self, message: str, **kwargs: Any) -> None:
        """Log a debug message."""
        self.logger.debug(message, **kwargs)

    def log_info(self, message: str, **kwargs: Any) -> None:
        """Log an info message."""
        self.logger.info(message, **kwargs)

    def log_warning(self, message: str, **kwargs: Any) -> None:
        """Log a warning message."""
        self.logger.warning(message, **kwargs)

    async def resolve_credential(self, ref: "CredentialRef") -> "Credential":
        """Resolve a CredentialRef to a typed Credential.

        Args:
            ref: The CredentialRef to resolve.

        Returns:
            A typed Credential instance.

        Raises:
            RuntimeError: If no secret store is configured.
            CredentialNotFoundError: If the credential cannot be found.
            CredentialParseError: If parsing fails.
        """
        if self._secret_store is None:
            raise RuntimeError("No secret store configured")
        from application_sdk.credentials.resolver import CredentialResolver

        resolver = CredentialResolver(self._secret_store)
        return await resolver.resolve(ref)

    async def resolve_credential_raw(self, ref: "CredentialRef") -> dict[str, Any]:
        """Resolve a CredentialRef to a raw dict (for legacy client compat).

        Args:
            ref: The CredentialRef to resolve.

        Returns:
            The raw credential data as a dict.

        Raises:
            RuntimeError: If no secret store is configured.
        """
        if self._secret_store is None:
            raise RuntimeError("No secret store configured")
        from application_sdk.credentials.resolver import CredentialResolver

        resolver = CredentialResolver(self._secret_store)
        return await resolver.resolve_raw(ref)

    def log_error(self, message: str, **kwargs: Any) -> None:
        """Log an error message."""
        self.logger.error(message, **kwargs)


@dataclass
class ChildAppContext:
    """Context for calling child Apps from within a parent App.

    Created via AppContext.child() to maintain correlation.
    """

    parent: AppContext
    child_app_name: str

    @property
    def correlation_id(self) -> str:
        """Inherit correlation ID from parent."""
        return self.parent.correlation_id

    @property
    def parent_run_id(self) -> str:
        """Parent's run ID for tracing."""
        return self.parent.run_id


@dataclass
class TaskExecutionContext:
    """Execution context available during @task method execution.

    Provides heartbeat support for long-running tasks:
    - heartbeat(): Send manual heartbeats with progress details
    - get_last_heartbeat_details(): Get last heartbeat for resume on retry
    - run_in_thread(): Run blocking operations without breaking heartbeats

    This context is only available inside @task methods, not in run().
    Access via self.task_context in your task methods.

    Example:
        @task(timeout_seconds=3600)
        async def process_large_dataset(self, input: ProcessInput) -> ProcessOutput:
            # Resume from last heartbeat on retry
            last = self.task_context.get_last_heartbeat_details()
            start_index = last[0] if last else 0

            for i, record in enumerate(records[start_index:], start=start_index):
                await process_record(record)
                if i % 100 == 0:
                    self.task_context.heartbeat(i, len(records))

            return ProcessOutput(processed_count=len(records))
    """

    app_context: AppContext
    """The parent app's execution context."""

    task_name: str
    """Name of the task being executed."""

    heartbeat_controller: "HeartbeatController"
    """Controller for heartbeat operations."""

    def heartbeat(self, *details: Any) -> None:
        """Send a heartbeat with optional progress details.

        Call this periodically during long-running operations to signal
        the activity is still alive. Include progress information to
        enable resuming from the last checkpoint on retry.

        IMPORTANT: If heartbeating is disabled (heartbeat_timeout_seconds=None),
        this is a no-op.

        Args:
            *details: Serializable progress details (e.g., index, count).
                These are stored by Temporal and available via
                get_last_heartbeat_details() if the activity is retried.

        Example:
            for i, record in enumerate(records):
                process(record)
                if i % 100 == 0:
                    self.task_context.heartbeat(i, len(records))
        """
        self.heartbeat_controller.heartbeat(*details)

    def get_last_heartbeat_details(self) -> tuple[Any, ...]:
        """Get details from last heartbeat (for resume on retry).

        When an activity is retried after failure, this returns the
        details from the last successful heartbeat. Use this to resume
        processing from where you left off.

        Returns:
            Tuple of details from last heartbeat, or empty tuple if none.

        Example:
            last = self.task_context.get_last_heartbeat_details()
            start_index = last[0] if last else 0
            for i, record in enumerate(records[start_index:], start=start_index):
                ...
        """
        return self.heartbeat_controller.get_last_heartbeat_details()

    def get_heartbeat_details(self, cls: type[HT]) -> HT | None:
        """Get last heartbeat details deserialized as a typed dataclass.

        Prefer this over get_last_heartbeat_details() when using
        HeartbeatDetails subclasses. It handles the dict->dataclass
        reconstruction needed because Temporal deserializes heartbeat
        payloads to plain dicts on retry.

        Only known fields are passed to cls() — extra keys from a newer
        heartbeat version are silently ignored, and missing keys fall back
        to their dataclass defaults. This ensures forward and backward
        compatibility as HeartbeatDetails subclasses evolve.

        Args:
            cls: HeartbeatDetails subclass to deserialize into.

        Returns:
            An instance of cls with values from the last heartbeat,
            or None if no heartbeat was recorded.

        Example:
            last = self.task_context.get_heartbeat_details(LoadTypeHeartbeat)
            start_chunk = last.chunk_idx if last else 0
        """
        from dataclasses import fields as dc_fields

        raw = self.heartbeat_controller.get_last_heartbeat_details()
        if not raw:
            return None
        detail = raw[0]
        if isinstance(detail, cls):
            # Local/test execution: NoopHeartbeatController returns the actual instance
            return detail
        if isinstance(detail, dict):
            # Temporal deserializes heartbeat payloads to plain dicts on retry.
            # Filter to known fields for forward/backward compatibility.
            known = {f.name for f in dc_fields(cls)}
            return cls(**{k: v for k, v in detail.items() if k in known})
        return None

    async def run_in_thread(
        self, func: Callable[..., T], *args: Any, **kwargs: Any
    ) -> T:
        """Run a blocking function in a thread pool.

        Use this for blocking I/O or CPU-bound operations to keep the
        event loop responsive for auto-heartbeating.

        CRITICAL: Auto-heartbeats only work when the event loop yields.
        Without this wrapper, blocking operations will prevent heartbeats
        from being sent, potentially causing Temporal to restart your activity.

        Args:
            func: Blocking function to run.
            *args: Positional arguments for func.
            **kwargs: Keyword arguments for func.

        Returns:
            Result of func(*args, **kwargs).

        Example:
            # Instead of: response = requests.get(url)  # BLOCKS!
            response = await self.task_context.run_in_thread(
                requests.get, url, timeout=30
            )

            # For pandas operations:
            df = await self.task_context.run_in_thread(
                pd.read_csv, path, sep=",", header=0
            )
        """
        from application_sdk.execution.heartbeat import run_in_thread

        return await run_in_thread(func, *args, **kwargs)
