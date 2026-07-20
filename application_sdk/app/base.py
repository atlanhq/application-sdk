"""App base class and decorators."""

import asyncio
import importlib.metadata
import inspect
import os
import re
import shutil
import sys
import threading
import warnings
from abc import ABC
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Never,
    TypeVar,
    cast,
    get_type_hints,
)
from uuid import UUID

import obstore as obs
import orjson
from temporalio import activity, workflow
from temporalio.exceptions import FailureError

from application_sdk.app._ep_registration import (
    _apply_app_registration,
    _build_entry_points,
    _collect_implicit_ep,
    _register_tasks,
    _scan_entrypoints,
)
from application_sdk.app.base_errors import (
    AbstractRunNotImplementedError,
    ObjectStoreNotConfiguredError,
)
from application_sdk.app.context import (
    AppContext,
    TaskExecutionContext,
    _is_atlan_logger,
)
from application_sdk.app.entrypoint import EntryPointMetadata
from application_sdk.app.registry import AppMetadata, resolve_pool_queue
from application_sdk.app.task import get_task_metadata, is_task, task
from application_sdk.constants import (
    ASSET_VALIDATION_MAX_ITEMS_PER_AXIS,
    LOCAL_WORKFLOW_ID,
)
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
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.errors import (
    APP_CONTEXT_ERROR,
    APP_ERROR,
    APP_NON_RETRYABLE,
    ErrorCode,
)
from application_sdk.errors.base import AppError as _NewAppError
from application_sdk.errors.leaves import InternalError as _InternalError
from application_sdk.errors.leaves import InvalidInputError as _InvalidInputError
from application_sdk.observability.logger_adaptor import (
    ASSET_VALIDATION_MATRIX_KEY,
    get_logger,
)
from application_sdk.observability.observability import AtlanObservability

if TYPE_CHECKING:
    from application_sdk.validation import AssetValidationReport

_task_logger = get_logger(__name__)

try:
    _FRAMEWORK_VERSION = importlib.metadata.version("application-sdk")
except importlib.metadata.PackageNotFoundError:  # conformance: ignore[E009] package not installed (e.g. editable dev install); "unknown" sentinel is benign
    _FRAMEWORK_VERSION = "unknown"

# Fixed event name for the structured, queryable transformed-asset validation
# outcome. Emitted on every upload from inside the upload activity, so the
# Temporal activity context (workflow_id/run_id/app_name) is auto-stamped and the
# row joins to the workflow outcome in ClickHouse — mirrors the preflight gate's
# outcome event. Scalar counts land as top-level LogAttributes; the per-failure
# drill-down rides in the ``asset_validation_matrix`` JSON attribute.
# Peer of ``PREFLIGHT_OUTCOME_EVENT`` (execution/_temporal/preflight_gate.py):
# same outcome-event pattern. No shared registry for these names yet — worth
# adding only once a third such event exists.
ASSET_VALIDATION_EVENT = "Transformed-asset validation outcome"

# Cap on how many failure/orphan rows the matrix carries, aliased from the single
# source of truth in ``constants`` so the structured matrix and the human-readable
# ``format_report()`` listing (which defaults to the same constant) can never
# drift. The event's scalar counts always reflect the full batch; the matrix is a
# bounded drill-down sample so a pathological batch can't produce an unbounded
# LogAttributes value. Passed explicitly to ``format_report(max_items=...)`` below
# to make the shared cap obvious at the call site.
_VALIDATION_MATRIX_MAX_ROWS = ASSET_VALIDATION_MAX_ITEMS_PER_AXIS

# Per-row error message cap (chars). pyatlan_v9 ``.validate()`` messages can be
# long; truncate so a single row can't bloat the ClickHouse attribute.
_VALIDATION_MATRIX_ERROR_MAXLEN = 300


def _validation_matrix_json(report: "AssetValidationReport") -> str:
    """Compact per-failure matrix for the outcome event, as one JSON string.

    Lands as a single ``LogAttributes`` value in ClickHouse so connector-pulse can
    ``JSONExtract`` the per-failure detail against workflow outcomes with no schema
    change (mirrors the preflight gate's ``check_matrix``). Small fixed fields
    only, bounded to :data:`_VALIDATION_MATRIX_MAX_ROWS` rows per axis — the
    headline counts on the event carry the full totals, and the human-readable
    ``format_report()`` still rides in the WARNING body for flagged runs. Not
    internally guarded (``orjson.dumps`` can raise): the sole caller,
    :func:`_warn_on_invalid_transformed_assets`, wraps the whole emit in
    ``try/except``, so a raise here is caught there and never blocks the upload.
    """
    rows: list[dict[str, Any]] = []
    for f in report.failures[:_VALIDATION_MATRIX_MAX_ROWS]:
        rows.append(
            {
                "kind": "undeserializable" if f.deserialize_error else "invalid",
                "type_name": f.type_name,
                "qualified_name": f.qualified_name,
                "error": (f.errors[0] if f.errors else "")[
                    :_VALIDATION_MATRIX_ERROR_MAXLEN
                ],
                "file": f.file,
                "line": f.line,
            }
        )
    for o in report.orphans[:_VALIDATION_MATRIX_MAX_ROWS]:
        rows.append(
            {
                "kind": "orphan",
                "type_name": o.missing_type_name,
                "qualified_name": o.missing_qualified_name,
                "relationship": o.relationship,
                "reference_count": o.reference_count,
                "file": o.file,
                "line": o.line,
            }
        )
    return orjson.dumps(rows).decode()


def _validate_transformed_assets_blocking(
    local_path: str,
) -> "AssetValidationReport | None":
    """Blocking transformed-asset validation — runs off the event loop in a thread.

    Returns the report, or ``None`` when there is nothing to validate (flag off, or
    ``local_path`` is not a ``transformed/`` asset subtree — e.g. a raw upload).
    All the work here (filesystem walk, msgspec decode, RocksDB spill) is
    synchronous and CPU/IO-bound, which is why the async wrapper offloads it.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — deferred-constant import mirrors upload()'s pattern
        VALIDATE_ASSETS_ON_UPLOAD,
    )

    if not VALIDATE_ASSETS_ON_UPLOAD or not local_path:
        return None

    from application_sdk.validation import (  # noqa: PLC0415 — deferred: only load the validator on the upload path
        validate_transformed_dir,
    )

    root = Path(local_path)
    if root.is_dir():
        transformed = root / "transformed"
        if transformed.is_dir():
            target = transformed
        elif "transformed" in root.parts:
            target = root
        else:
            return None
    elif "transformed" in root.parts:
        target = root
    else:
        return None

    # Referential (orphan) integrity runs by default on the upload hook: extracts
    # and transforms are full by design by default, so every referenced parent is
    # present in the same batch and the orphan pass is accurate. It is warn-only
    # (never blocks, never raises), so even on an atypical partial batch the worst
    # case is a spurious warning, not a failed handoff.
    return validate_transformed_dir(target)


async def _warn_on_invalid_transformed_assets(local_path: str, app_name: str) -> None:
    """Best-effort, warn-only validation of transformed asset NDJSON before upload.

    BLDX-1555 defense-in-depth at the SDR→Atlan boundary. When ``local_path``
    holds transformed asset output, every record is validated against the
    pyatlan_v9 ``.validate()`` backbone (plus the referential/orphan pass). On
    **every** upload a structured :data:`ASSET_VALIDATION_EVENT` is emitted with
    the per-axis counts and a compact per-failure ``asset_validation_matrix`` JSON
    attribute, all allowlisted for OTLP so they reach ClickHouse and join to the
    workflow outcome by Temporal run id (mirrors the preflight gate's outcome
    event). A ``clean`` outcome is emitted too, so there is a denominator to rank
    flag-rate against. A human-readable WARNING with the full ``format_report()``
    is additionally logged only when the batch is flagged. Extracts and transforms
    are full by design by default, so the batch is complete and the orphan pass is
    accurate. This **never** blocks the upload and **never** raises — a defect in
    the scaffold must not break a real handoff — and it scans every record (no
    sampling) so the summary is accurate.

    The scan is offloaded via :func:`application_sdk.execution.heartbeat.run_in_thread`
    (the SDK's blocking-work escape hatch, ADR-0010) so it never blocks the event
    loop or the activity's auto-heartbeat while a large batch is validated.
    ``run_in_thread`` dispatches onto a dedicated ``sdk-blocking-*`` pool rather
    than the shared default executor that Temporal's activity scheduling also
    uses; sharing that pool risks a catastrophic worker deadlock (enforced by
    conformance rule P031).
    """
    from application_sdk.constants import (  # noqa: PLC0415 — deferred-constant import mirrors upload()'s pattern
        VALIDATE_ASSETS_ON_UPLOAD,
    )

    # Read the flag before the thread hop so a disabled feature costs nothing —
    # no thread-pool dispatch on every upload. The blocking scan re-checks it too.
    if not VALIDATE_ASSETS_ON_UPLOAD:
        return

    from application_sdk.execution.heartbeat import (  # noqa: PLC0415 — deferred: app.base is imported by execution (circular)
        run_in_thread,
    )

    try:
        report = await run_in_thread(_validate_transformed_assets_blocking, local_path)
    except Exception:  # noqa: BLE001 — defense-in-depth must never break the upload
        _task_logger.warning(
            "Transformed-asset validation skipped due to an unexpected error",
            exc_info=True,
        )
        return

    if report is None:
        # Nothing to validate (flag off, or not a transformed/ subtree) — no
        # event, no denominator noise for uploads that never ran validation.
        return

    # Emit the structured outcome on every validated upload (clean + flagged) so
    # the row is queryable and gives a denominator. Wrapped: a defect in the
    # emit path (e.g. matrix encoding) must never break a real handoff.
    try:
        flagged = not report.ok
        _task_logger.info(
            ASSET_VALIDATION_EVENT,
            outcome="flagged" if flagged else "clean",
            app_name=app_name,
            assets_total=report.total,
            assets_passed=report.passed,
            # ``failed`` counts undeserializable records too; report "invalid"
            # (per-asset .validate() failures) disjointly, matching the headline
            # in format_report().
            assets_invalid=report.failed - report.undeserializable,
            assets_orphaned=len(report.orphans),
            assets_undeserializable=report.undeserializable,
            **{ASSET_VALIDATION_MATRIX_KEY: _validation_matrix_json(report)},
        )
        if flagged:
            _task_logger.warning(
                "Transformed-asset validation flagged issues before upload "
                "(handoff continues): %s",
                report.format_report(max_items=_VALIDATION_MATRIX_MAX_ROWS),
            )
    except Exception:  # noqa: BLE001 — defense-in-depth must never break the upload
        _task_logger.warning(
            "Transformed-asset validation outcome emission failed "
            "(handoff continues)",
            exc_info=True,
        )


# Type variable for require() method
T = TypeVar("T")
HT = TypeVar("HT", bound=HeartbeatDetails)

# BLDX-878: inter-app calls deactivated pending review.
# TChildInput = TypeVar("TChildInput", bound=Input)
# TChildOutput = TypeVar("TChildOutput", bound=Output)


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


class AppError(_NewAppError):
    """Deprecated: use ``application_sdk.errors.AppError`` directly — removed in v4.0.

    Back-compat shim. Accepts a positional ``message`` argument and the legacy
    ``error_code`` keyword to avoid breaking existing raise sites.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = APP_ERROR
    code: ClassVar[str] = "APP"

    def __init__(
        self,
        message: str,
        *,
        app_name: str | None = None,
        run_id: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        warnings.warn(
            "application_sdk.app.AppError is deprecated; "
            "use application_sdk.errors.AppError — will be removed in v4.0",
            DeprecationWarning,
            stacklevel=2,
        )
        _NewAppError.__init__(
            self, message=message, cause=cause, app_name=app_name, run_id=run_id
        )
        self._legacy_error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        return (
            self._legacy_error_code
            if self._legacy_error_code is not None
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


class AppContextError(_InternalError):
    """Raised when App or task context is accessed outside of valid execution scope.

    This is a programming error — it indicates that context-dependent methods
    (e.g. ``self.context``, ``self.heartbeat()``) were called outside of a
    workflow run or @task execution.
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = APP_CONTEXT_ERROR
    code: ClassVar[str] = "INTERNAL_APP_CONTEXT"

    def __init__(self, message: str, *, error_code: ErrorCode | None = None) -> None:
        _InternalError.__init__(self, message=message)
        self._legacy_error_code = error_code

    @property
    def error_code(self) -> ErrorCode:
        return (
            self._legacy_error_code
            if self._legacy_error_code is not None
            else self.DEFAULT_ERROR_CODE
        )

    def __str__(self) -> str:
        return f"[{self.error_code.code}] {self.message}"


class NonRetryableError(AppError):
    """Deprecated: use a typed ``AppError`` subclass with ``default_retryable = False`` — removed in v4.0.

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
    default_retryable: ClassVar[bool] = False


class RetryableError(AppError):
    """Deprecated: use a typed ``AppError`` subclass with ``default_retryable = True`` — removed in v4.0.

    Extend this when raising an error directly from an ``@entrypoint`` method
    that signals a *transient* failure — one where retrying the entire workflow
    execution might succeed (e.g. a downstream service that is temporarily
    unavailable).

    Any exception that does *not* extend ``RetryableError`` (including all
    native Python exceptions like ``ValueError`` or ``KeyError``) is treated as
    non-retryable when raised directly from an entry point, because those errors
    are deterministic: retrying will never change the outcome.

    Note: transient failures that occur *inside* a ``@task`` should use that
    task's own ``retry_max_attempts`` setting instead of this class, since
    activity-level retries are cheaper than full workflow retries.

    Example::

        class DownstreamUnavailable(RetryableError):
            pass

        @entrypoint
        async def extract(self, input: ExtractInput) -> ExtractOutput:
            if not await self.probe_downstream():
                raise DownstreamUnavailable("Downstream API is not ready")
            ...
    """

    DEFAULT_ERROR_CODE: ClassVar[ErrorCode] = APP_ERROR
    default_retryable: ClassVar[bool] = True


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
    try:
        wid = activity.info().workflow_id
    # conformance: ignore[E004] probe for Temporal activity context; broad catch is intentional, exception re-raised as typed AppContextError
    except Exception as e:
        raise AppContextError("Cannot access app state outside of task context") from e
    if not wid:
        raise AppContextError("activity workflow_id is empty")
    return wid


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
            StateStoreNotConfiguredError: If no state store is configured.
        """
        await self._app.context.save_state(key, value)

    async def load(self, key: str) -> dict[str, Any] | None:
        """Load state from the external state store.

        Args:
            key: State key (will be namespaced to this app/run).

        Returns:
            The saved state or None if not found.

        Raises:
            StateStoreNotConfiguredError: If no state store is configured.
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
    - Is durable and resumable

    The run() method must be deterministic - use @task methods for side effects.

    Example::

        class MyApp(App):

            @task
            async def fetch_data(self, input: FetchInput) -> FetchOutput:
                # Tasks can do I/O - no restrictions
                return FetchOutput(data=await http_client.get(input.url).json())

            async def run(self, input: MyInput) -> MyOutput:
                # run() is deterministic - only call tasks
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

    preflight_gate_mode: ClassVar[Literal["hard", "soft"]] = "soft"
    """Preflight gate posture. ``"soft"`` (default) never blocks — a
    ``NOT_READY`` verdict lets the run proceed and is emitted as
    ``outcome="would_block"`` on the gate outcome event so it is always
    reported. ``"hard"`` is the opt-in that blocks the run when the handler's
    verdict is ``NOT_READY`` (the worker logs at boot). Set ``"hard"`` once the
    app's checks are trusted to gate real runs. The ``ATLAN_PREFLIGHT_GATE_MODE``
    env var overrides this at deploy time; any set value other than ``"hard"``
    resolves to soft. An empty or unset value is not an override — resolution
    falls through to this declared attribute. See the adopt-preflight-gate skill."""

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
    # BLDX-878: inter-app calls deactivated pending review.
    # _client: "WorkflowAppClient | None" = None
    _task_context: "TaskExecutionContext | None" = None

    def __init_subclass__(cls, **kwargs: Any) -> None:
        """Automatically register App subclasses.

        This is called when a class inherits from App. It:
        1. Derives the app name from the class name if not specified
        2. Collects explicit @entrypoint methods and/or the implicit run() entry point
        3. Delegates building/validation to _build_entry_points
        4. Registers with the AppRegistry via _apply_app_registration

        Skip registration if:
        - The class was already registered
        - The class has other unimplemented abstract methods (besides run)
        - No valid entry points or run() with proper types found
        """
        super().__init_subclass__(**kwargs)

        # Skip if already registered (check own __dict__ only, not inherited)
        if cls.__dict__.get("_app_registered", False):
            return

        app_name = cls.name or _pascal_to_kebab(cls.__name__)

        # Skip classes with unimplemented abstract methods other than run() —
        # those are intermediate abstract bases, not concrete apps.
        abstract_methods = {
            m
            for m in dir(cls)
            if getattr(getattr(cls, m, None), "__isabstractmethod__", False)
        }
        if abstract_methods - {"run"}:
            return

        explicit_eps = _scan_entrypoints(cls)
        implicit_ep = _collect_implicit_ep(cls, App.run)

        entry_points = _build_entry_points(cls, implicit_ep, explicit_eps)
        if not entry_points:
            return

        # App-level _input_type/_output_type come from the default entry point
        # (or the first registered for single-entry-point apps where
        # _resolve_default_entrypoint uses the len==1 path).
        default_ep = next(
            (ep for ep in entry_points.values() if ep.default),
            next(iter(entry_points.values())),
        )
        _apply_app_registration(
            cls=cls,
            name=app_name,
            version=cls.version,
            description=cls.description,
            tags=cls.tags,
            passthrough_modules=cls.passthrough_modules,
            input_type=default_ep.input_type,
            output_type=default_ep.output_type,
            entry_points=entry_points,
        )

    @property
    def context(self) -> AppContext:
        """Get the current execution context.

        Raises:
            AppContextError: If accessed outside of run() execution.
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
            AppContextError: If accessed outside of @task method execution.
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
    # Task-only methods (raise AppContextError if called outside @task methods)
    # =========================================================================

    def heartbeat(self, *details: Any) -> None:
        """Send a heartbeat with optional progress details.

        Only available in @task methods.

        Args:
            *details: Serializable progress details.

        Raises:
            AppContextError: If called outside a @task method.
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
            AppContextError: If called outside a @task method.
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
            AppContextError: If called outside a @task method.
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
            AppContextError: If called outside a @task method.
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
            AppContextError: If called outside a @task method.
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
            AppContextError: If called outside a @task method.
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
        raise AbstractRunNotImplementedError(app_class=type(self).__name__)

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

        **Store routing (SDR vs non-SDR):** this method targets the upstream
        object store when one is configured (``UPSTREAM_OBJECT_STORE_NAME``
        points to a distinct Dapr component), and falls back to the deployment
        store otherwise.  In standard (non-SDR) deployments only the deployment
        binding is present, so ``upstream_storage`` is ``None`` and routing
        falls back to the deployment store.  In SDR deployments the upstream
        store is Atlan's bucket — the correct destination for extracted
        artifacts handed off to the publish app.

        This routing applies to ``App.upload()`` and ``App.download()``.  The
        automatic file-reference materialisation that transfers ``FileReference``
        objects between ``@task`` methods always uses the deployment store; use
        that mechanism (not ``App.upload()`` / ``App.download()``) for
        intermediate task-to-task data.

        For direct use inside an existing ``@task``, import and call
        :func:`application_sdk.storage.transfer.upload` directly.

        Args:
            input: ``UploadInput`` describing the local file or directory and
                optional destination override.

        Returns:
            ``UploadOutput`` with a durable ``FileReference`` (``is_durable=True``)
            containing both ``local_path`` and ``storage_path``, plus ``file_count``
            indicating the number of files uploaded.

        Example — SDR extract app handing off artifacts to the publish app::

            async def run(self, input: ExtractInput) -> ExtractOutput:
                result = await self.extract_data(ExtractInput(source=input.source))
                up = await self.upload(UploadInput(local_path=result.output_file))
                # Return the ref so the publish app can consume it as input
                return ExtractOutput(artifacts_ref=up.ref)

        Example — upload an entire output directory::

            up = await self.upload(UploadInput(local_path="/tmp/output/"))
            # up.ref.file_count == number of files in the directory
        """

        from application_sdk.constants import (  # noqa: PLC0415 — import here to avoid module-level circular import (same pattern as normalize_key)
            DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED,
            DEPLOYMENT_ARTIFACT_DUAL_WRITE_REQUIRED,
        )
        from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: app.base is imported by execution which imports storage
            normalize_key,
        )
        from application_sdk.storage.transfer import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
            upload as _upload,
        )

        # BLDX-1555 defense-in-depth: validate transformed assets against the
        # pyatlan_v9 backbone before the handoff. Warn-only, best-effort, and
        # offloaded to a worker thread so it never blocks the event loop.
        await _warn_on_invalid_transformed_assets(input.local_path, self._app_name)

        deployment = self.context.storage
        upstream = self.context.upstream_storage

        # Build the ordered list of (store, label, fatal) upload targets.
        # See ADR-0014 §"BLDX-1464 dual-write" for the full routing decision.
        if (
            DEPLOYMENT_ARTIFACT_DUAL_WRITE_ENABLED
            and upstream is not None
            and deployment is not None
            and upstream is not deployment
        ):
            targets = [
                (deployment, "deployment", DEPLOYMENT_ARTIFACT_DUAL_WRITE_REQUIRED),
                (upstream, "upstream", True),
            ]
        else:
            single = upstream or deployment
            if single is None:
                raise ObjectStoreNotConfiguredError()
            targets = [
                (single, "upstream" if single is upstream else "deployment", True)
            ]

        run_prefix = f"artifacts/apps/{self._app_name}/workflows/{self.context.workflow_id}/{self.context.run_id}"
        app_prefix = input.tier.upload_prefix(
            run_prefix=run_prefix, app_name=self._app_name
        )

        # Derive the FileReference for cross-store dedup / deployment-store fallback.
        # normalize_key strips TEMPORARY_PATH → canonical deployment-store key.
        # Guard: "" means local_path resolved to the store root — skip source_ref.
        _store_key = normalize_key(input.local_path) if input.local_path else ""
        source_ref = input.ref or (
            FileReference(local_path=input.local_path, storage_path=_store_key)
            if _store_key
            else None
        )

        # Fan-out: iterate targets in order. Fatal failures are deferred until all
        # remaining targets complete so the upstream write always runs and a copy
        # lands somewhere. If both writes fail the deployment error is chained as
        # __cause__ of the upstream error so neither traceback is lost.
        result: UploadOutput | None = None
        deferred_required_error: Exception | None = None
        for target_store, label, fatal in targets:
            # When writing to the deployment store, it is already the source for the
            # cross-store fallback — passing it as _source_store would add a redundant
            # SHA-256 sidecar lookup against itself. Skip it in that case.
            source_store = None if target_store is deployment else self.context.storage
            try:
                out = await _upload(
                    input.local_path,
                    input.storage_path,
                    storage_subdir=input.storage_subdir,
                    skip_if_exists=input.skip_if_exists,
                    raise_on_empty=input.raise_on_empty,
                    store=target_store,
                    _source_ref=source_ref,
                    _source_store=source_store,
                    _app_prefix=app_prefix,
                    _tier=input.tier,
                )
            except Exception as exc:
                if fatal:
                    _task_logger.error(
                        "Object-store upload to %s store failed for prefix %s; "
                        "will fail run after remaining targets complete",
                        label,
                        app_prefix,
                        exc_info=True,
                    )
                    if deferred_required_error is not None:
                        # Both writes failed: chain the earlier error as __cause__ so
                        # neither traceback is lost when the exception is surfaced.
                        exc.__cause__ = deferred_required_error
                    deferred_required_error = exc
                else:
                    _task_logger.warning(
                        "Object-store upload to %s store failed for prefix %s "
                        "(non-fatal); continuing to next target",
                        label,
                        app_prefix,
                        exc_info=True,
                    )
                continue
            result = out  # last successful write is authoritative (upstream in SDR)

        if deferred_required_error is not None:
            raise deferred_required_error
        if result is None:
            # Unreachable under current target-construction logic (the single-target
            # branch is always fatal=True), but guards against future changes.
            raise _InternalError(
                message="App.upload fan-out captured no result — this is a programming error",
                invariant="upload fan-out must produce at least one result",
            )
        return result

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

        **Store routing (SDR vs non-SDR):** mirrors ``App.upload()`` — reads
        from the upstream store when one is configured, falling back to the
        deployment store otherwise.  In standard (non-SDR) deployments only
        the deployment binding is present, so ``upstream_storage`` is ``None``
        and routing falls back to the deployment store.  In SDR deployments
        the publish app uses this to pull artifacts written by the extract app.

        For direct use inside an existing ``@task``, import and call
        :func:`application_sdk.storage.transfer.download` directly.

        Args:
            input: ``DownloadInput`` with the store key or prefix to download
                and optional local destination path.

        Returns:
            ``DownloadOutput`` with a fully materialised ``FileReference``
            containing both ``storage_path`` and ``local_path``.

        Example — SDR publish app consuming artifacts from the extract app::

            async def run(self, input: PublishInput) -> PublishOutput:
                dl = await self.download(DownloadInput(ref=input.artifacts_ref))
                return await self.publish_data(
                    PublishInput(local_path=dl.ref.local_path)
                )

        Example — re-materialise an existing FileReference::

            dl = await self.download(DownloadInput(ref=input.model_ref))
        """
        from application_sdk.storage.transfer import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
            download as _download,
        )

        store = self.context.upstream_storage or self.context.storage
        if store is None:
            raise ObjectStoreNotConfiguredError()
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

        from application_sdk.constants import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
            CLEANUP_BASE_PATHS,
            TEMPORARY_PATH,
            TRACKED_FILE_REFS_KEY,
        )
        from application_sdk.execution import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
            build_output_path,
        )

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
                            _task_logger.warning(
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
                _task_logger.warning(
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
        from application_sdk.constants import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
            PROTECTED_STORAGE_PREFIXES,
            TRACKED_FILE_REFS_KEY,
        )
        from application_sdk.execution import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
            build_output_path,
        )
        from application_sdk.storage.ops import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
            _resolve_store,
            delete,
        )

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
                    _task_logger.warning(
                        "Object store delete failed during cleanup",
                        exc_info=True,
                    )
                    return False

        # 1. Delete tracked transient objects.
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

            async def _local_cleanup() -> None:
                try:
                    await self.cleanup_files(CleanupInput())
                # conformance: ignore[E004] logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
                except Exception:
                    _safe_log(
                        "warning",
                        "cleanup_files task failed during on_complete",
                        exc_info=True,
                    )

            async def _storage_cleanup() -> None:
                try:
                    await self.cleanup_storage(StorageCleanupInput())
                # conformance: ignore[E004] logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
                except Exception:
                    _safe_log(
                        "warning",
                        "cleanup_storage task failed during on_complete",
                        exc_info=True,
                    )

            await asyncio.gather(_local_cleanup(), _storage_cleanup())

        try:
            await AtlanObservability.flush_all()
        # conformance: ignore[E004] logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
        except Exception:
            _safe_log("warning", "flush_all() failed during on_complete", exc_info=True)


# =============================================================================
# Registration helpers — see application_sdk/app/_ep_registration.py
# =============================================================================
# _register_tasks, _collect_implicit_ep, _scan_entrypoints,
# _build_entry_points, _apply_app_registration are imported at the top of this
# module from _ep_registration and re-exported via __all__ for backward compat.


# Cache generated workflow classes keyed by (app_cls, entry_point_name) so
# generate_workflow_class() is idempotent across repeated calls (e.g. tests
# or worker re-creation) and never registers the same Temporal workflow twice.
_workflow_class_cache: dict[tuple[type, str], type] = {}


def _validate_interaction_signature(
    fn: Callable[..., Any],
    kind: Literal["signal", "query", "update"],
    fn_name: str,
) -> None:
    """Validate that a @signal / @query / @update method satisfies the interaction contract.

    Rules enforced at class-definition time:
    - ``@signal``: no params besides ``self`` (pure trigger, no payload).
    - ``@query``: no params besides ``self``; return type must be a subclass of Output.
    - ``@update``: exactly one param besides ``self`` that is a subclass of Input;
      return type must be a subclass of Output.

    Dynamic interactions (``name is None``) are skipped — callers must check before
    calling this function.

    Args:
        fn: The original (undecorated) interaction function.
        kind: One of ``"signal"``, ``"query"``, or ``"update"``.
        fn_name: Human-readable name used in error messages.

    Raises:
        _InvalidInputError: If the signature does not satisfy the contract.
    """
    sig = inspect.signature(fn)
    params = [p for p in sig.parameters.values() if p.name != "self"]

    try:
        hints: dict[str, Any] = get_type_hints(fn)
    # conformance: ignore[E004,E009] get_type_hints probe; broad catch intentional — falls back to __annotations__ when forward refs are unresolvable
    except Exception:
        hints = getattr(fn, "__annotations__", {})

    if kind == "signal":
        if params:
            raise _InvalidInputError(
                message=(
                    f"@signal '{fn_name}' must have no parameters besides self "
                    f"(signals are pure triggers — they carry no payload). "
                    f"Got {len(params)} extra parameter(s): "
                    f"{[p.name for p in params]}. "
                    f"To carry data into a running workflow, use @update instead."
                )
            )

    elif kind == "query":
        if params:
            raise _InvalidInputError(
                message=(
                    f"@query '{fn_name}' must have no parameters besides self. "
                    f"Got {len(params)} extra parameter(s): "
                    f"{[p.name for p in params]}. "
                    f"Queries are read-only probes; pass context via instance fields set "
                    f"by an earlier @update if needed."
                )
            )
        return_type = hints.get("return")
        if not (
            return_type is not None
            and isinstance(return_type, type)
            and issubclass(return_type, Output)
        ):
            raise _InvalidInputError(
                message=(
                    f"@query '{fn_name}' return type must be a subclass of Output, "
                    f"got {return_type!r}. "
                    f"Define a dataclass that extends Output and annotate the return type."
                )
            )

    else:  # kind == "update"
        if len(params) != 1:
            raise _InvalidInputError(
                message=(
                    f"@update '{fn_name}' must have exactly one parameter besides self "
                    f"(a subclass of Input), got {len(params)}. "
                    f"Wrap multiple values in a single Input dataclass."
                )
            )
        param = params[0]
        input_type = hints.get(param.name)
        if not (
            input_type is not None
            and isinstance(input_type, type)
            and issubclass(input_type, Input)
        ):
            raise _InvalidInputError(
                message=(
                    f"@update '{fn_name}' parameter '{param.name}' must be a subclass "
                    f"of Input, got {input_type!r}. "
                    f"Define a dataclass that extends Input and use it as the parameter type."
                )
            )
        return_type = hints.get("return")
        if not (
            return_type is not None
            and isinstance(return_type, type)
            and issubclass(return_type, Output)
        ):
            raise _InvalidInputError(
                message=(
                    f"@update '{fn_name}' return type must be a subclass of Output, "
                    f"got {return_type!r}. "
                    f"Define a dataclass that extends Output and annotate the return type."
                )
            )


def _collect_interaction_relays(
    app_cls: "type[App]", cls_name: str
) -> dict[str, Callable[..., Any]]:
    """Scan the App class for @signal / @query / @update runtime interactions and
    synthesize per-interaction relay methods bound to the generated wf_cls.

    Each relay extracts the per-run App instance from ``wf_self._app_instance`` and
    delegates the call. The synthesized relay carries Temporal's discovery metadata
    (rebound to point at the relay), so @workflow.defn(wf_cls) registers the
    interaction against the generated class — which is what Temporal requires.

    Returns a mapping of method name -> relay callable, ready to be placed on wf_cls.
    """
    relays: dict[str, Callable[..., Any]] = {}

    def _build_relay(method_name: str, is_coroutine: bool) -> Callable[..., Any]:
        """Construct a wf_cls-level method that delegates to self._app_instance."""
        if is_coroutine:

            async def _async_relay(wf_self: Any, *args: Any, **kwargs: Any) -> Any:
                bound = getattr(wf_self._app_instance, method_name)
                return await bound(*args, **kwargs)

            _async_relay.__name__ = method_name
            _async_relay.__qualname__ = f"{cls_name}.{method_name}"
            _async_relay.__module__ = app_cls.__module__
            return _async_relay

        def _sync_relay(wf_self: Any, *args: Any, **kwargs: Any) -> Any:
            bound = getattr(wf_self._app_instance, method_name)
            return bound(*args, **kwargs)

        _sync_relay.__name__ = method_name
        _sync_relay.__qualname__ = f"{cls_name}.{method_name}"
        _sync_relay.__module__ = app_cls.__module__
        return _sync_relay

    def _build_validator_relay(
        orig_validator: Callable[..., Any],
    ) -> Callable[..., Any]:
        def vrelay(wf_self: Any, *args: Any, **kwargs: Any) -> Any:
            return orig_validator(wf_self._app_instance, *args, **kwargs)

        return vrelay

    for member_name, member in inspect.getmembers(app_cls):
        if member_name == "run":
            # The entry method is handled separately; never relay it as an interaction.
            continue

        signal_defn = getattr(member, "__temporal_signal_definition", None)
        query_defn = getattr(member, "__temporal_query_definition", None)
        update_defn = getattr(member, "_defn", None)

        # @workflow.update returns a callable with both `_defn` and `validator`
        # (Temporal's runtime_checkable UpdateMethodMultiParam Protocol). The
        # `validator` attribute distinguishes it from arbitrary objects that
        # might happen to carry a `_defn` field.
        is_update = update_defn is not None and hasattr(member, "validator")

        if not (signal_defn or query_defn or is_update):
            continue

        # Contract enforcement — skip dynamic interactions (name is None).
        if signal_defn is not None and signal_defn.name is not None:
            _validate_interaction_signature(signal_defn.fn, "signal", member_name)
        elif query_defn is not None and query_defn.name is not None:
            _validate_interaction_signature(query_defn.fn, "query", member_name)
        elif is_update and update_defn is not None and update_defn.name is not None:
            _validate_interaction_signature(update_defn.fn, "update", member_name)

        relay = _build_relay(member_name, inspect.iscoroutinefunction(member))

        if signal_defn is not None:
            # Rebind the definition's fn to the relay so Temporal's _bind_method
            # passes wf_self (not an App instance) as the first arg.
            relay.__temporal_signal_definition = replace(  # type: ignore[attr-defined]
                signal_defn, fn=relay
            )
        elif query_defn is not None:
            relay.__temporal_query_definition = replace(  # type: ignore[attr-defined]
                query_defn, fn=relay
            )
        else:
            # update_defn is _UpdateDefinition (asserted above by `is_update`).
            assert update_defn is not None
            new_validator: Callable[..., Any] | None = None
            if update_defn.validator is not None:
                new_validator = _build_validator_relay(update_defn.validator)

            relay._defn = replace(  # type: ignore[attr-defined]
                update_defn, fn=relay, validator=new_validator
            )
            # Temporal's @workflow.update decorator also sets a `.validator`
            # attribute on the decorated fn (partial(_update_validator, defn)).
            # We don't need it for runtime dispatch — Temporal reads the
            # validator off the definition — but provide one so the relay
            # structurally matches UpdateMethodMultiParam.
            relay.validator = lambda fn: fn  # type: ignore[attr-defined]

        relays[member_name] = relay

    return relays


async def _run_preflight_gate(
    input_data: Input, app_name: str, entrypoint: str
) -> None:
    """Run the SDK-owned pre-extraction preflight gate (HYP-1883).

    Dispatches the app's preflight handler as a mandatory first activity. The
    activity raises ``PreflightFailed`` when the verdict is ``NOT_READY``; this
    re-raises only that deliberate block and aborts the run. Every other activity
    failure (timeout, secret-store/transport error, handler crash) fails open —
    logged loudly, run proceeds — because the gate is an injected guardrail the
    app never opted into, so its own plumbing must not kill a run.

    Guards: ``workflow.patched("preflight-gate")`` keeps pre-gate runs replaying
    deterministically; ``isinstance(input_data, CredentialResolvable)`` skips
    source-less apps. Credential resolution happens inside the activity — the
    deterministic workflow forwards only secret-free references. Dispatch is to
    the one app-level handler, with ``entrypoint`` threaded through for internal
    branching.

    The ``no_verdict`` outcome event emitted here (fail-open path) omits
    ``gate_mode``, unlike the ``blocked``/``would_block``/``proceeded`` events
    the activity emits: the workflow layer never sees ``enforce`` (it is baked
    into the activity closure at worker build), so the mode is not available to
    stamp on this row.
    """
    if not workflow.patched("preflight-gate"):
        return

    with workflow.unsafe.imports_passed_through():
        from application_sdk.credentials.ref import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            CredentialResolvable,
        )
        from application_sdk.execution._temporal.preflight_gate import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            GATE_RETRY,
            GATE_SCHEDULE_TO_CLOSE,
            GATE_START_TO_CLOSE,
            PREFLIGHT_OUTCOME_EVENT,
            PreflightGateInput,
            is_preflight_block,
            preflight_gate_activity_name,
        )

    if not isinstance(input_data, CredentialResolvable):
        return

    entry = entrypoint or "<implicit>"
    try:
        gate_input = PreflightGateInput.from_extraction_input(input_data, entrypoint)
        await workflow.execute_activity(
            preflight_gate_activity_name(app_name),
            gate_input,
            schedule_to_close_timeout=GATE_SCHEDULE_TO_CLOSE,
            start_to_close_timeout=GATE_START_TO_CLOSE,
            retry_policy=GATE_RETRY,
        )
    except Exception as e:
        # The activity emits the blocked outcome event before it raises; the
        # workflow only re-raises the deliberate block here.
        if is_preflight_block(e):
            raise
        # Fail-open: only the workflow knows a no-verdict failure happened, so it
        # owns the no_verdict row (plus the loud ERROR line).
        _safe_log(
            "error",
            "Preflight gate could not produce a verdict; proceeding without source "
            "verification (fail-open)",
            app_name=app_name,
            entrypoint=entry,
            exc_info=True,
        )
        _safe_log(
            "info",
            PREFLIGHT_OUTCOME_EVENT,
            app_name=app_name,
            entrypoint=entry,
            outcome="no_verdict",
            reason=type(e).__name__,
        )
        return

    # Success: the activity already emitted the proceeded outcome event.


def _validate_workflow_input(raw_input: Any, input_type: type[Input]) -> Input:
    """Validate a decoded workflow payload against the entry point's typed contract.

    The generated workflow declares its argument to Temporal with a permissive
    ``Any`` annotation (see ``generate_workflow_class``). That is deliberate: if the
    argument were annotated as ``input_type``, Temporal's pydantic data converter
    would validate the payload while *decoding the workflow task*, and a bad payload
    would surface as a Workflow *Task* failure — which Temporal retries with backoff
    **indefinitely** (there is no max-attempts for workflow tasks). A permanently
    invalid input (e.g. a required object field sent as ``""``) would then retry
    forever instead of failing.

    Validating here, inside the workflow body, converts that into a clean,
    non-retryable workflow *execution* failure that fails fast.

    This does **not** loosen or bypass the typed contract. The payload is validated
    against exactly ``input_type`` — the same model the entry point declares — before
    any app code runs, and the fully typed, validated model is what every downstream
    caller (preflight gate, ``_log_summary``, the entry point method) receives. The
    permissive annotation is confined to this generated wire boundary; there is no
    path by which a raw payload reaches app code, and app authors cannot opt into it.

    Note that this validates in pydantic's python/lax mode (``model_validate`` on a
    decoded dict) rather than the json mode the removed typed annotation gave Temporal's
    data converter. The two are equivalent for every field type a permitted ``Input``
    tree allows — ``bytes``/``bytearray``, the one genuinely mode-divergent type, is
    forbidden by ``validate_payload_safety`` (contracts/base.py). A maintainer adding a
    custom, mode-sensitive pydantic type to a contract should re-check that equivalence.

    Args:
        raw_input: The decoded payload — a dict from the wire, or (unit tests / the
            in-process ``/start`` path) an already-constructed ``input_type``.
        input_type: The entry point's typed input contract.

    Returns:
        A validated ``input_type`` instance.

    Raises:
        ApplicationError: non-retryable, if the payload does not satisfy ``input_type``.
    """
    if isinstance(raw_input, input_type):
        # Already the typed model (constructed by the SDK /start path or a test);
        # it was validated at construction, so re-validation would be redundant.
        return raw_input

    # deferred import: circular — execution/__init__.py loads _temporal which imports app.base
    from application_sdk.execution.errors import ApplicationError  # noqa: PLC0415

    try:
        return input_type.model_validate(raw_input)
    # conformance: ignore[E004] deterministic input-validation failure; logged via _safe_log with exc_info=True and re-raised as a non-retryable ApplicationError
    except Exception as e:
        _safe_log(
            "error",
            "Workflow input failed validation against its typed contract",
            input_type=input_type.__name__,
            exc_info=True,
        )
        # Non-retryable: a payload that fails schema validation is deterministic —
        # retrying the same input can never succeed. Failing the execution now
        # avoids the indefinite Workflow Task retry loop that a decode-time failure
        # would cause.
        raise ApplicationError(
            f"Invalid workflow input for {input_type.__name__}: {e}",
            type="InputValidationError",
            non_retryable=True,
        ) from e


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
    entrypoint_name = ep.name
    input_type = ep.input_type
    output_type = ep.output_type
    app_name = app_cls._app_name
    app_version = app_cls._app_version

    from application_sdk.execution._temporal.preflight_gate import (  # noqa: PLC0415 — boot-time (not in workflow sandbox); avoids a module-load cycle
        input_type_supports_gate,
    )

    if not input_type_supports_gate(input_type):
        _task_logger.warning(
            "Preflight gate will not run for entrypoint '%s' (%s): input type %s does "
            "not declare the credential-routing fields (extraction_method, "
            "credential_guid, agent_json) top-level. Expected for source-less "
            "entrypoints; if this entrypoint verifies a data source, declare those "
            "fields (e.g. via the contract toolkit's ExtractionInput) so source "
            "verification runs before extraction.",
            entrypoint_name,
            workflow_name,
            input_type.__name__,
        )

    # input_data is annotated ``Any`` (not ``input_type``) so Temporal's data
    # converter decodes the payload without validating it — validation happens
    # below via _validate_workflow_input so a bad payload fails the workflow
    # execution (fast, non-retryable) instead of failing the workflow *task*
    # (retried by the server indefinitely). See _validate_workflow_input.
    async def _run(self, input_data: Any) -> Output:
        # deferred imports: inside Temporal sandbox (workflow.unsafe.imports_passed_through context)
        # BLDX-878: inter-app calls deactivated pending review.
        # from application_sdk.app.client import WorkflowAppClient

        # Fail fast on a malformed / wrong-typed payload, before any setup runs.
        # Enforces the entry point's typed contract at the workflow boundary.
        #
        # Placement is deliberate — this MUST stay above the setup and the outer
        # try/except below, for two reasons:
        #   1. Downstream code (the preflight gate, _log_summary, the entry method)
        #      requires a validated typed model; it must never see the raw dict the
        #      Any-annotated argument decodes to.
        #   2. It preserves pre-PR behavior: previously a bad payload failed during
        #      Temporal's decode, so neither the _run body nor the on_complete()
        #      finally ran. Moving this into the try/except would run on_complete()
        #      for an input that never entered the workflow proper.
        # (Retryability is not the reason: the raised ApplicationError is a
        # FailureError subclass, so the outer handler's `isinstance(e, FailureError)`
        # branch would bare re-raise it, preserving non_retryable=True either way.)
        input_data = _validate_workflow_input(input_data, input_type)

        start_time = _safe_now()
        run_id = workflow.info().run_id
        workflow_id = workflow.info().workflow_id

        try:
            with workflow.unsafe.imports_passed_through():
                from application_sdk.observability.correlation import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
                    get_correlation_context,
                )

            _corr_ctx = get_correlation_context()
            correlation_id = _corr_ctx.correlation_id if _corr_ctx else run_id
        # conformance: ignore[E004] logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
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
            workflow_id=workflow_id,
            correlation_id=correlation_id,
            started_at=start_time,
        )
        # The wf_cls.__init__ constructs the App instance up-front so that any
        # @signal / @query / @update runtime interactions
        # (which may fire as early as immediately after workflow start, before
        # _run's first await) can delegate to the same instance _run uses.
        # Fall back to constructing one here when ``self`` is a stand-in (e.g.
        # MagicMock from unit tests) where ``__init__`` didn't run.
        existing = getattr(self, "_app_instance", None)
        if isinstance(existing, app_cls):
            app_instance = existing
        else:
            app_instance = app_cls()
            self._app_instance = app_instance
        app_instance._context = context

        context_data = {
            "run_id": run_id,
            "workflow_id": workflow_id,
            "correlation_id": context.correlation_id,
        }
        # BLDX-878: inter-app calls deactivated pending review.
        # app_instance._client = WorkflowAppClient(context_data)
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
        # conformance: ignore[E004] logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
        except Exception:
            _safe_log("warning", "Failed to log input summary", exc_info=True)

        try:
            await _run_preflight_gate(input_data, app_name, entrypoint_name)
            entry_method = getattr(app_instance, entry_method_name)
            result = await entry_method(input_data)
            return cast("Output", result)

        # conformance: ignore[E004] top-level entrypoint handler; logged via _safe_log with exc_info=True and re-raised as typed ApplicationError
        except Exception as e:
            with workflow.unsafe.imports_passed_through():
                from application_sdk.execution._temporal.preflight_gate import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
                    is_preflight_block,
                )
            # A deliberate preflight-gate block logs terse (classification already
            # on the error's FailureDetails); the marker may sit on a cause.
            if is_preflight_block(e):
                _safe_log(
                    "warning",
                    "App blocked by preflight gate",
                    app_name=app_name,
                    run_id=str(run_id),
                    correlation_id=context.correlation_id,
                    reason=str(e),
                )
            else:
                _safe_log(
                    "error",
                    "App failed",
                    app_name=app_name,
                    run_id=str(run_id),
                    correlation_id=context.correlation_id,
                    error_type=type(e).__name__,
                    exc_info=True,
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
            #
            # All raw exceptions are non-retryable: any Python exception raised
            # directly from an entrypoint (not through a @task activity) is
            # deterministic — retrying will never fix a KeyError or TypeError.
            # Transient failures (network, timeout) should be modelled as @task
            # activities with their own retry policy, not raised directly here.
            from application_sdk.execution.errors import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
                ApplicationError,
            )

            if isinstance(e, FailureError):
                raise
            if isinstance(e, _NewAppError):
                raise ApplicationError(
                    str(e),
                    e.to_failure_details(),
                    type=type(e).__name__,
                    non_retryable=not e.effective_retryable,
                ) from e
            raise ApplicationError(
                str(e),
                type=type(e).__name__,
                non_retryable=not isinstance(e, RetryableError),
            ) from e

        finally:
            try:
                await app_instance.on_complete()
            # conformance: ignore[E004] finally-block cleanup handler; logged via _safe_log with exc_info=True; must not re-raise from finally
            except Exception:
                _safe_log(
                    "warning",
                    "on_complete() hook raised an unexpected exception",
                    exc_info=True,
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
            # BLDX-878: inter-app calls deactivated pending review.
            # app_instance._client = None

    safe_name = workflow_name.replace("-", "_").replace(":", "_")
    cls_name = f"_Workflow_{safe_name}"

    # Temporal's _is_unbound_method_on_cls checks:
    #   fn.__qualname__.rsplit(".", 1)[0] == cls.__name__
    # so we must set __qualname__ BEFORE applying @workflow.run.
    _run.__name__ = "run"
    _run.__qualname__ = f"{cls_name}.run"
    _run.__module__ = app_cls.__module__
    # input_data is decoded as ``Any`` on purpose: this is the annotation Temporal's
    # pydantic data converter reads to decode the workflow task. Decoding as ``Any``
    # keeps validation out of the converter (where a failure = an indefinitely
    # retried Workflow Task failure) and defers it to _validate_workflow_input inside
    # the run body, which enforces ``input_type`` and fails fast on a bad payload.
    # The return type stays typed so results are still validated/serialized normally.
    _run.__annotations__ = {"input_data": Any, "return": output_type}

    decorated_run = workflow.run(_run)

    # Collect any @signal / @query / @update runtime interactions declared on
    # the App subclass. Each is rewritten into a relay whose Temporal-discovery
    # metadata points at the relay (so the wf_cls is what's registered, not the
    # App class) and whose body delegates to ``self._app_instance.<method>``
    # — sharing state with _run.
    interaction_relays = _collect_interaction_relays(app_cls, cls_name)

    def _wf_init(self: Any) -> None:
        # Construct the per-run App instance eagerly so interactions that fire
        # before _run's first await still hit a live instance. _run later
        # finishes context setup (correlation id, _wrap_instance_tasks, etc.)
        # on this same instance.
        self._app_instance = app_cls()

    _wf_init.__name__ = "__init__"
    _wf_init.__qualname__ = f"{cls_name}.__init__"
    _wf_init.__module__ = app_cls.__module__

    wf_methods: dict[str, Any] = {"run": decorated_run, "__init__": _wf_init}
    wf_methods.update(interaction_relays)

    wf_cls = type(cls_name, (), wf_methods)
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
        context_data: Context dict with run_id, workflow_id, and correlation_id.
    """
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
                    pool=task_meta.pool,
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
    *,
    pool: str | None = None,
) -> Any:
    """Create a wrapper that executes a task as a Temporal activity.

    Args:
        app_name: Name of the app.
        task_name: Name of the task (simple name, no prefix).
        timeout_seconds: Activity timeout.
        retry_max_attempts: Maximum retry attempts.
        retry_max_interval_seconds: Maximum interval between retries.
        output_type: The typed output class for deserialization.
        context_data: Context dict with run_id, workflow_id, and correlation_id.
        heartbeat_timeout_seconds: Heartbeat timeout. None disables.
        auto_heartbeat_seconds: Auto-heartbeat interval. None disables.
        retry_policy: Full retry policy (overrides max_attempts/interval if set).
        pool: Logical worker-pool name. When set, the activity is routed
            to a dedicated task queue. Queue name resolution order:
            1. ``ATLAN_POOL_<POOL>_QUEUE`` env var (explicit override).
            2. ``{ATLAN_TASK_QUEUE}-{pool}`` derived from the app's base queue
               (default — ensures different apps with the same pool name get
               different Temporal queues automatically).

    Returns:
        Async function that executes the task as an activity.
    """
    # Resolve pool → task queue at construction time. Env vars are fixed for
    # the process lifetime, so capturing the result in the closure is safe.
    # Resolution order: explicit ATLAN_POOL_<POOL>_QUEUE override first, then
    # derive from ATLAN_TASK_QUEUE so two apps sharing a pool name (e.g.
    # "heavy") never collide on the same Temporal queue.
    pool_queue: str | None = resolve_pool_queue(pool) if pool else None
    from application_sdk.execution.retry import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
        RetryPolicy as _RP,
    )
    from application_sdk.execution.retry import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
        _to_temporal_retry_policy,
    )

    with workflow.unsafe.imports_passed_through():
        from application_sdk.execution._temporal.activities import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
            TaskContext,
        )
        from application_sdk.execution._temporal.eviction_retry import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
            execute_activity_with_eviction_retry,
        )

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
            workflow_id=context_data.get("workflow_id", LOCAL_WORKFLOW_ID),
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

        # Execute as activity, routed through the SDK eviction-retry loop so
        # worker pod evictions (SIGTERM mid-activity) re-dispatch as fresh
        # attempts without burning the application-error retry budget.
        # When a pool_queue is set the activity is dispatched to the task
        # queue for that pool; otherwise it runs on the workflow's own queue.
        result: Output = await execute_activity_with_eviction_retry(
            f"{app_name}:{task_name}",
            args=[task_context, input_data],
            start_to_close_timeout=timedelta(seconds=timeout_seconds),
            heartbeat_timeout=heartbeat_timeout,
            retry_policy=temporal_retry_policy,
            result_type=output_type,
            summary=summary,
            **({"task_queue": pool_queue} if pool_queue else {}),
        )

        return result

    return wrapper


# Keep FileReference accessible via base module for convenience
__all__ = [
    "App",
    "AppError",
    "AppStateAccessor",
    "FileReference",
    "NonRetryableError",
    "PersistentStateAccessor",
    "RetryableError",
    "TaskStateAccessor",
    "_app_state",
    "_app_state_lock",
    "_apply_app_registration",
    "_create_task_activity_wrapper",
    "_pascal_to_kebab",
    "_register_tasks",
    "_safe_log",
    "_safe_now",
    "_safe_uuid",
    "_scan_entrypoints",
    "_wrap_instance_tasks",
    "generate_workflow_class",
    "task",
]
