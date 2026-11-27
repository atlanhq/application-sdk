"""App base class and decorators."""

import asyncio
import importlib.metadata
import inspect
import os
import re
import shutil
import threading
import types
import warnings
from abc import ABC
from collections.abc import Callable, Iterable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import (
    TYPE_CHECKING,
    Any,
    ClassVar,
    Literal,
    Never,
    ParamSpec,
    TypeVar,
)
from uuid import UUID

from temporalio import activity, workflow

from application_sdk import _deprecation
from application_sdk._context_errors import (  # re-exported; defined in the api distribution
    AppContextError,
)
from application_sdk._runtime.offload import run_in_thread
from application_sdk.app._app_config_validation import validate_app_config
from application_sdk.app._ep_registration import (
    _apply_app_registration,
    _build_entry_points,
    _collect_implicit_ep,
    _register_tasks,
    _scan_entrypoints,
)
from application_sdk.app._run_scratch import run_scratch_dir as _run_scratch_dir
from application_sdk.app.base_errors import (
    AbstractRunNotImplementedError,
)
from application_sdk.app.context import (
    AppContext,
    TaskExecutionContext,
    _is_atlan_logger,
)
from application_sdk.app.entrypoint import (
    EntryPointDeclarationError,
)
from application_sdk.app.registry import AppMetadata
from application_sdk.app.task import task
from application_sdk.common._env import env_bool
from application_sdk.constants import (
    APPLICATION_VERSION,
    COMMIT_SHA,
)
from application_sdk.contracts.base import HeartbeatDetails, Input, Output
from application_sdk.contracts.cleanup import (
    CleanupInput,
    CleanupOutput,
    StorageCleanupInput,
    StorageCleanupOutput,
)
from application_sdk.contracts.fetch_failures import (
    MergeFetchFailuresInput,
    MergeFetchFailuresOutput,
)
from application_sdk.contracts.skipped_records import (
    MergeSkippedRecordsInput,
    MergeSkippedRecordsOutput,
)
from application_sdk.contracts.storage import (
    DownloadInput,
    DownloadOutput,
    UploadInput,
    UploadOutput,
    UploadRefsInput,
    UploadRefsOutput,
    VerifyRefsInput,
    VerifyRefsOutput,
)
from application_sdk.contracts.types import FileReference, StorageTier
from application_sdk.errors._legacy_codes import ErrorCode
from application_sdk.errors.base import AppError as _NewAppError
from application_sdk.errors.base import _LegacyErrorCodeMixin
from application_sdk.execution._temporal.run_scope import (
    _current_run_key,
)
from application_sdk.handler.contracts import PreflightGateMode

# Kept importable from here: the event's emitter lived in this module, and v3
# has shipped that import site (tests/unit/observability/test_event_registry.py).
from application_sdk.observability.events import (  # noqa: F401
    ASSET_VALIDATION_EVENT,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.observability import AtlanObservability
from application_sdk.outputs import Metric, get_outputs
from application_sdk.outputs.fetch_failures import (
    FETCH_FAILURES_LAYOUT,
    FETCH_FAILURES_TRUNCATED_METRIC,
)
from application_sdk.storage import _app_tasks
from application_sdk.storage._app_tasks import _cleanup_ref_key
from application_sdk.version import __version__ as _SDK_VERSION

if TYPE_CHECKING:
    from application_sdk.validation.asset_rules import AssetCreationRules

_task_logger = get_logger(__name__)

try:
    _FRAMEWORK_VERSION = importlib.metadata.version("application-sdk")
except importlib.metadata.PackageNotFoundError:  # conformance: ignore[E009] package not installed (e.g. editable dev install); "unknown" sentinel is benign
    _FRAMEWORK_VERSION = "unknown"

#: Retry shape for the four framework tasks that move bytes across a store
#: boundary — ``upload``, ``download``, ``verify_refs``, ``upload_refs``.
#:
#: What these tasks were short of is not attempt *count* but attempt *spread*.
#: Three attempts on the default 1-second initial interval retry after 1s and
#: 2s, so the whole budget is spent inside a ~3-second window: any dependency
#: outage outliving three seconds costs the entire run. The outages they
#: actually meet are tens of seconds. In FND-2076 a tenant's blobstorage
#: gateway rejected every signed request for at least 55 seconds while its own
#: Keycloak lookup was timing out, and ``upload_refs`` burned all three
#: attempts within 25 seconds of the first failure — the run died at the
#: handoff with a complete, already-paid-for extraction behind it.
#:
#: Four attempts at 10s / 20s / 40s spread 70 seconds of backoff across the
#: same work, which covers that shape with margin. The cost falls only on a
#: store that is genuinely broken rather than briefly unavailable: it now fails
#: after ~70s of waiting instead of ~3s, which is the right way round for work
#: this far downstream.
#:
#: These tasks declare no ``schedule_to_close_seconds``, so the wider backoff
#: has no ceiling to overrun. A deployment that sets a fleet-wide one via
#: ``ATLAN_SCHEDULE_TO_CLOSE_TIMEOUT_SECONDS`` should note that
#: ``retry_product_seconds``'s fixed 10s backoff allowance no longer covers
#: this policy's waits — see its docstring on passing a larger
#: ``backoff_headroom_seconds``.
#:
#: Three constants passed by name rather than one dict splatted into ``@task``:
#: the decorator is overloaded, and a ``**dict[str, int]`` defeats overload
#: resolution.
_STORE_TRANSFER_MAX_ATTEMPTS = 4
_STORE_TRANSFER_INITIAL_INTERVAL_SECONDS = 10
_STORE_TRANSFER_MAX_INTERVAL_SECONDS = 60


# Type variable for require() method
T = TypeVar("T")
_P = ParamSpec("_P")
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


def _format_build_identity() -> str:
    """``sdk=<v> app=<v> commit=<sha>`` for the App lifecycle messages (FND-1936).

    This rides in the *message* rather than in structured attributes, because
    the message is the only field that survives every hop of the run-logs path.
    The Iceberg table behind it (``observability.app_logs``) has a fixed schema
    and the ingest pipe's Jolt shift maps a known field list onto those columns,
    so a new attribute is never carried; heracles then re-projects each record
    through two closed structs that declare no attributes bag. A marker in the
    message needs none of that to change, and reaches the run-log panel, the
    ``.log`` and ``.ndjson`` exports and any future consumer at once.

    The remaining structured carriers are the OTel Resource ``sdk.version``
    (on every OTLP log record) and the ``app_version`` / ``commit_sha`` fields
    on ``worker_start`` and ``token_refresh`` events. Those still answer fleet
    questions this string cannot. The four interceptor lifecycle lines
    (``workflow.*`` / ``activity.*``) no longer carry ``sdk.version`` /
    ``app.version`` attributes.

    Keys are ``k=v`` and ASCII so that the grep that motivated this — an
    engineer searching an exported run log for a version — actually hits.
    An empty carrier drops its key rather than emitting a bare ``app=``, which
    would read as a value of its own. ``sdk`` is always known.
    """
    parts = [f"sdk={_SDK_VERSION}"]
    if APPLICATION_VERSION:
        parts.append(f"app={APPLICATION_VERSION}")
    if COMMIT_SHA:
        parts.append(f"commit={COMMIT_SHA}")
    return " ".join(parts)


#: Computed once at import: every carrier is an import-time constant and none
#: can change for the life of the container, so the workflow-side call sites
#: below do no work and stay sandbox-safe.
_BUILD_IDENTITY = _format_build_identity()


def _safe_log(level: str, message: str, **attrs: object) -> None:
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


@dataclass(kw_only=True)
class AppError(_LegacyErrorCodeMixin, _NewAppError):
    """Deprecated: use ``application_sdk.errors.AppError`` directly — removed in v4.0.

    Back-compat shim. Accepts a positional ``message`` argument and the legacy
    ``error_code`` keyword to avoid breaking existing raise sites.
    """

    code: ClassVar[str] = "APP"
    _STR_FIELDS: ClassVar[tuple[tuple[str, str], ...]] = (
        ("app_name", "app"),
        ("run_id", "run_id"),
    )

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
        self._set_legacy_error_code(error_code)


@dataclass(kw_only=True)
class NonRetryableError(AppError):
    """Deprecated: use a typed ``AppError`` subclass with ``default_retryable = False`` — removed in v4.0.0.

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

    default_retryable: ClassVar[bool] = False

    def __init__(
        self,
        message: str,
        *,
        app_name: str | None = None,
        run_id: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        # Warns when an error is raised, not when a subclass is defined, so
        # importing an app that subclasses this never warns. Replaces the
        # generic AppError notice with one naming this class.
        _deprecation.warn_deprecated(
            "NonRetryableError",
            "a typed application_sdk.errors.AppError subclass with "
            "default_retryable = False",
            "4.0.0",
        )
        _NewAppError.__init__(
            self, message=message, cause=cause, app_name=app_name, run_id=run_id
        )
        self._set_legacy_error_code(error_code)


@dataclass(kw_only=True)
class RetryableError(AppError):
    """Deprecated: use a typed ``AppError`` subclass with ``default_retryable = True`` — removed in v4.0.0.

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

    default_retryable: ClassVar[bool] = True

    def __init__(
        self,
        message: str,
        *,
        app_name: str | None = None,
        run_id: str | None = None,
        cause: Exception | None = None,
        error_code: ErrorCode | None = None,
    ) -> None:
        # Warns when an error is raised, not when a subclass is defined, so
        # importing an app that subclasses this never warns. Replaces the
        # generic AppError notice with one naming this class.
        _deprecation.warn_deprecated(
            "RetryableError",
            "a typed application_sdk.errors.AppError subclass with "
            "default_retryable = True",
            "4.0.0",
        )
        _NewAppError.__init__(
            self, message=message, cause=cause, app_name=app_name, run_id=run_id
        )
        self._set_legacy_error_code(error_code)


# =============================================================================
# State Accessors
# =============================================================================


#: In-memory app state shared by all App subclasses, one slot per run:
#: ``(workflow_id, run_id) -> {key: value}``. Process-local: each worker holds
#: the slots of the runs whose tasks it executed, so a run's state on one
#: worker is invisible to another. A slot is released, and every value in it
#: closed, by the worker's run-state sweeper once Temporal reports the run
#: closed (``execution._temporal.credential_sweep``), and the rest at worker
#: shutdown — on every worker, not only the one that ran the workflow.
_app_state: dict[tuple[str, str], dict[str, Any]] = {}
_app_state_lock = threading.Lock()

#: Bound on closing one app-state value when its run's slot is released, so a
#: client whose close hangs cannot stall the sweeper.
_STATE_VALUE_CLOSE_TIMEOUT_SECONDS = 10.0


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


def _runs_with_app_state() -> list[tuple[str, str]]:
    """The runs that still hold an app-state slot on this worker."""
    with _app_state_lock:
        return list(_app_state)


def _run_close_in_own_thread(closer: Callable[[], object]) -> "asyncio.Future[object]":
    """Run a synchronous ``close()`` on a thread of its own; resolve when done.

    Not on the shared blocking pool (``run_in_thread``): a close that hangs
    past ``_STATE_VALUE_CLOSE_TIMEOUT_SECONDS`` keeps its thread after the
    await gives up, and on the pool every such hang would take a worker away
    from the tasks the pool serves. A daemon thread of its own costs nothing
    to leave behind, and does not hold up interpreter exit.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[object] = loop.create_future()

    def _settle(outcome: object, error: BaseException | None) -> None:
        if future.done():
            return  # the await already gave up on it
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(outcome)

    def _run() -> None:
        outcome: object = None
        error: BaseException | None = None
        try:
            outcome = closer()
        # conformance: ignore[E004] the outcome is handed back to the awaiting coroutine, which logs it
        except BaseException as exc:  # noqa: BLE001 — re-raised on the event loop through the future
            error = exc
        try:
            loop.call_soon_threadsafe(_settle, outcome, error)
        except RuntimeError:  # conformance: ignore[E002] the event loop has closed (worker exit), so nothing awaits this close any more
            pass

    threading.Thread(target=_run, name="app-state-close", daemon=True).start()
    return future


async def _close_state_value(value: object) -> None:
    """Close one app-state value: ``aclose()`` if it has one, else ``close()``.

    A value with neither is left alone. A synchronous ``close()`` runs on a
    thread of its own, since a client's close may block on a socket. Each
    close is awaited for at most ``_STATE_VALUE_CLOSE_TIMEOUT_SECONDS``.
    """
    if isinstance(value, type):
        return  # a class object's close is an unbound method, not this value's
    closer = getattr(value, "aclose", None)
    if not callable(closer):
        closer = getattr(value, "close", None)
    if not callable(closer):
        return

    async def _close() -> None:
        if inspect.iscoroutinefunction(closer):
            result: object = await closer()
        else:
            result = await _run_close_in_own_thread(closer)
        if inspect.isawaitable(result):
            await result

    await asyncio.wait_for(_close(), timeout=_STATE_VALUE_CLOSE_TIMEOUT_SECONDS)


#: Releases started and not yet finished. A release removes its slot from
#: ``_app_state`` before it does the work, so a shutdown that cancels the
#: sweeper mid-release would otherwise lose that slot: ``release_all_runs``
#: waits for these before it releases what is left.
_releases_in_flight: set["asyncio.Task[None]"] = set()


async def _release_run_state(run: tuple[str, str]) -> None:
    """Release *run*'s app-state slot on this worker.

    Removes the slot, deletes the local files of the ``FileReference`` objects
    it tracked (when cleanup is enabled, as for ``cleanup_files``), and closes
    every value that has ``aclose()`` or ``close()`` — a cached client, a
    connection pool. Called by the worker's run-state sweeper once Temporal
    reports the run closed, and for every remaining run at worker shutdown.
    Each failure is logged and skipped; the slot is gone either way.

    The work runs as its own task, shielded: cancelling the caller (the
    sweeper, at shutdown) leaves it running, and ``_drain_state_releases``
    waits for it.
    """
    with _app_state_lock:
        slot = _app_state.pop(run, None)
    if not slot:
        return
    release = asyncio.create_task(_release_slot(slot), name="app-state-release")
    _releases_in_flight.add(release)
    release.add_done_callback(_releases_in_flight.discard)
    await asyncio.shield(release)


async def _drain_state_releases() -> None:
    """Wait for every release already under way to finish."""
    while _releases_in_flight:
        await asyncio.gather(*list(_releases_in_flight), return_exceptions=True)


async def _release_slot(slot: dict[str, Any]) -> None:
    """Delete a removed slot's tracked local files and close its values."""
    from application_sdk.constants import (  # noqa: PLC0415 — patched at module path in tests; lifting would break mock.patch sites
        TRACKED_FILE_REFS_KEY,
    )

    tracked_refs = slot.get(TRACKED_FILE_REFS_KEY) or ()
    if tracked_refs and env_bool("APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR", True):
        for ref in tracked_refs:
            local_path: str | None = getattr(ref, "local_path", None)
            if not local_path:
                continue
            for path in (local_path, local_path + ".sha256"):
                try:
                    if os.path.isdir(path):
                        await run_in_thread(shutil.rmtree, path)
                    elif os.path.lexists(path):
                        await run_in_thread(os.remove, path)
                except OSError:
                    _task_logger.warning(
                        "Failed to delete a tracked local path of a closed run",
                        exc_info=True,
                    )

    closed: set[int] = set()
    for key, value in slot.items():
        if key == TRACKED_FILE_REFS_KEY or value is None or id(value) in closed:
            continue
        closed.add(id(value))
        try:
            await _close_state_value(value)
        # conformance: ignore[E004] releasing a closed run's state must outlive one bad value; logged, the rest are still closed
        except Exception:
            _task_logger.warning(
                "Failed to close app state value %r of a closed run",
                key,
                exc_info=True,
            )


class _RunStoredRefs:
    """Stored refs a run's workflow read off its tasks' inputs and results.

    Lives on the workflow side, one per run. Built only from task inputs and
    results, which come from workflow history, so a replay on any worker
    rebuilds the same set. ``cleanup_storage`` gets them through
    ``StorageCleanupInput.file_refs``: the refs a task on another worker
    tracked are otherwise invisible to the worker that runs the cleanup.
    """

    def __init__(self) -> None:
        self._refs: dict[tuple[object, ...], FileReference] = {}

    def record(self, refs: Iterable[FileReference]) -> None:
        """Keep each ref that names a stored object, once per object.

        The copy kept drops ``local_path`` (a path on the worker that made
        it) and turns ``auto_materialize`` off, so ``cleanup_storage`` is
        never handed a download.
        """
        for ref in refs:
            if not ref.storage_path:
                continue
            key = _cleanup_ref_key(ref)
            if key not in self._refs:
                self._refs[key] = ref.model_copy(
                    update={"local_path": None, "auto_materialize": False}
                )

    def for_cleanup(self) -> list[FileReference]:
        """The refs to pass to ``cleanup_storage``, bounded for the payload.

        Durable refs come first, so a cut never drops the ref that keeps a
        ``TRANSIENT`` alias of its key from being deleted. A cut is logged.
        """
        from application_sdk.contracts.cleanup import (  # noqa: PLC0415 — cold path: once per run, at cleanup
            STORAGE_CLEANUP_FILE_REFS_MAX,
        )

        refs = sorted(
            self._refs.values(), key=lambda r: r.tier == StorageTier.TRANSIENT
        )
        if len(refs) > STORAGE_CLEANUP_FILE_REFS_MAX:
            # _safe_log takes no format args; the counts go in the message.
            _safe_log(
                "warning",
                f"Passing {STORAGE_CLEANUP_FILE_REFS_MAX} of the run's "
                f"{len(refs)} stored refs to cleanup_storage; TRANSIENT objects "
                "among those left out are deleted only where the worker that "
                "tracked them runs the cleanup",
            )
            refs = refs[:STORAGE_CLEANUP_FILE_REFS_MAX]
        return refs


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
        """Get in-memory state for the current run on this worker."""
        run = _current_run_key()
        with _app_state_lock:
            return _app_state.get(run, {}).get(key)

    def set(self, key: str, value: object) -> None:
        """Set in-memory state for the current run on this worker.

        A value with ``aclose()`` or ``close()`` is closed when the run's slot
        is released, after the run has closed.
        """
        run = _current_run_key()
        with _app_state_lock:
            _app_state.setdefault(run, {})[key] = value


class AppStateAccessor:
    """Accessor for in-memory state scoped to app execution.

    Provides a clean namespace for app-scoped state operations:
        self.app_state.get(key)
        self.app_state.set(key, value)

    State persists across task calls of the same run on the same worker. It
    is NOT shared between workers and NOT persisted externally: a task that
    runs on another worker, or after a restart, sees an empty slot. Once the
    run has closed, each worker releases its slot and closes every value with
    ``aclose()`` or ``close()`` (a cached client, say), so do not store an
    object another run still uses.
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

    def set(self, key: str, value: object) -> None:
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


def _require_aliases_not_expired(cls: type, app_name: str) -> None:
    """Enforce the opt-in ``legacy_workflow_types_removal_version`` expiry.

    Own-class reads, like the alias map itself: the expiry names one app's
    migration deadline and must not propagate through the MRO.
    """
    removal = cls.__dict__.get("legacy_workflow_types_removal_version") or ""
    aliases = cls.__dict__.get("legacy_workflow_types") or {}
    if not removal or not aliases:
        return
    from application_sdk.version import (  # noqa: PLC0415 — cheap, avoids import-time cycle
        __version__,
    )

    if not all(part.isdigit() for part in removal.split(".")):
        raise EntryPointDeclarationError(
            message=f"App '{app_name}': legacy_workflow_types_removal_version must "
            f"be a dotted numeric version, got {removal!r}."
        )
    from packaging.version import (  # noqa: PLC0415 — cheap, lazy like __version__
        Version,
    )

    if Version(__version__) >= Version(removal):
        raise EntryPointDeclarationError(
            message=f"App '{app_name}': legacy_workflow_types declared with removal "
            f"version {removal}, and the installed SDK is {__version__}. The "
            f"aliases have expired — delete the drained "
            f"legacy_workflow_types entries, or push "
            f"legacy_workflow_types_removal_version out if callers remain."
        )


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

    asset_validation_rules: ClassVar["frozenset[AssetCreationRules]"] = frozenset()
    """Extra create-time checks for the pre-upload transformed-asset validation.

    Empty (default) validates exactly what pyatlan_v9's ``.validate()`` checks.
    pyatlan 11.5.0 stopped reporting SQL-family assets that lack their schema or
    database parents; an app whose transform emits those assets opts back in::

        from application_sdk.validation import AssetCreationRules

        class MyApp(App):
            asset_validation_rules = frozenset({AssetCreationRules.SQL_PARENTS})

    Applies to :meth:`upload` and :meth:`upload_refs`. The validation stays
    warn-only: a failure is reported on ``ASSET_VALIDATION_EVENT`` and never
    blocks the handoff. Integration tests declare the same rules on
    ``BaseIntegrationTest.asset_validation_rules``."""

    legacy_workflow_types: ClassVar[Mapping[str, str]] = types.MappingProxyType({})
    """Inbound-only Temporal workflow type aliases: ``{alias: entry-point name}``.

    Declare a legacy type that external callers still dispatch (e.g. a bare
    pre-migration name like ``"KeifuWorkflow"``) so the worker keeps accepting
    it. The alias is accepted, never produced: every SDK-initiated dispatch
    emits the canonical ``{app-name}:{entry-point-name}`` type, so the
    ``temporal.workflow.type`` telemetry dimension counts exactly the callers
    that have not migrated. Delete an alias only when that count reaches zero.

    Not inherited: registration reads each subclass's own declaration, so an
    App subclassing an aliased App starts with no aliases — the alias names a
    wire contract of one specific app, and silently re-registering it under a
    second app's name would fail worker startup with a cross-app collision.

    This is a temporary compatibility surface for migrations, not a naming
    lever — the canonical type cannot be changed.

    Declare it twice, on purpose. For an app with a contract tree the contracted
    declaration site is the generated manifest's ``legacy_workflow_types`` block
    (the toolkit's ``legacyWorkflowTypes``), which conformance K015 holds in
    agreement with this attribute. Only this attribute registers the alias with
    the worker; only the manifest block is read by P016 when it decides whether
    a bare DAG node routes an entry point. An app with no contract tree declares
    it here alone.
    """

    legacy_workflow_types_removal_version: ClassVar[str] = ""
    """Opt-in expiry for the aliases: an SDK version string (``"4.2.0"``).

    Once the installed SDK reaches this version, registration fails while any
    ``legacy_workflow_types`` remain declared — keeping the aliases becomes a
    loud decision (push the version out, or delete the drained aliases) rather
    than drift. Empty (the default) declares no expiry; removal then gates on
    the ``temporal.workflow.type`` legacy-caller count reaching zero, which is
    the right posture for aliases with a wide external caller set.

    The expiry is app-level, not per-alias. A contract-carrying app declares the
    same value as the manifest block's ``removal_version``; K015 compares them."""

    preflight_gate_mode: ClassVar["PreflightGateMode | Literal['hard', 'soft']"] = (
        "soft"
    )
    """Preflight gate posture, a :class:`~application_sdk.handler.contracts.PreflightGateMode`.

    Soft (default) never blocks — a ``NOT_READY`` verdict lets the run proceed
    and is emitted as ``outcome="would_block"`` on the gate outcome event so it
    is always reported. Hard is the opt-in that blocks the run on an outcome
    the gate attributes to the source — a ``NOT_READY`` verdict, anything the
    handler raises, a frame Temporal ended — when the attributed failure is one
    the customer can act on (``GATE_BLOCKING_CATEGORIES``: auth, permission,
    invalid input, precondition, not found, and a source still unavailable or
    rate-limited on the gate's final attempt). Anything else, including a probe overrunning the
    budget, is reported as ``would_block`` and the run proceeds. A source that
    is cold rather than down says so through :meth:`Handler.warmup
    <application_sdk.handler.base.Handler.warmup>`, never by raising
    ``SOURCE_UNAVAILABLE``. Set it once the app's checks are
    trusted to gate real runs. The bare strings
    ``"hard"`` and ``"soft"`` are accepted and coerced once; any other value
    resolves to soft. This attribute is the only source of the posture: the
    worker and the workflow both read it, so they cannot disagree. See the
    adopt-preflight-gate skill.

    Note this posture applies to *every* outcome the gate can attribute to the
    source, not only a ``NOT_READY`` verdict, but never blocks on ``TIMEOUT``
    or the deprecated fail-open categories: a probe that
    overruns :attr:`preflight_gate_timeout_seconds` and an untyped handler crash
    (``INTERNAL``) are reported, a missing credential typed ``NOT_FOUND`` blocks.
    Failures of the gate's own plumbing (secret
    store outage, rate limit, worker unavailable) always fail open, in both
    postures — a platform blip must not fail a healthy run."""

    preflight_gate_timeout_seconds: ClassVar[int] = 150
    """Seconds the preflight handler gets to run all its checks.

    Clamped to 5-300s. Note this bounds the *whole* handler call, not each check —
    one slow probe can consume the budget and leave the rest unrun. The SDK
    *enforces* this — the gate cancels ``preflight_check`` when it elapses — and
    passes what remains after credential resolution as
    ``PreflightInput.timeout_seconds``, so a handler that sizes its probes to that
    value is sizing to the real deadline.

    A deadline, not a reservation: a handler returning in 3s holds its slot for 3s
    whatever this says. The 150s default is deliberately generous while the fleet's
    real check durations are being measured; expect it to come down once the
    distribution is known. Raise it only for a source demonstrably slower than
    that, and size checks to finish inside it with headroom. An overrun is a ``TIMEOUT``,
    which hard mode reports rather than blocks, so a budget smaller than the handler's
    real cost silently turns the gate into a report. Probes must
    also stay awaitable: cancellation lands at an ``await``, so blocking
    synchronous I/O on the event loop cannot be interrupted."""

    preflight_gate_max_attempts: ClassVar[int] = 2
    """Attempts the gate activity may take. Clamped to 1-3.

    In hard mode a ``SOURCE_UNAVAILABLE`` or ``RATE_LIMITED`` failure is
    retried: it blocks only on the final attempt, so a source unreachable or
    rate-limited for a moment gets the next attempt to answer. Every other source fault, handler crash and budget
    overrun is a verdict on the attempt it happens. A retry also buys the
    gate's own retryable plumbing — a store probe that failed, a credential
    lookup the secret store refused — and this sizes the
    ``schedule_to_close`` window. ``1`` means an unreachable source blocks on
    its first answer. So an app declaring a large
    :attr:`preflight_gate_timeout_seconds` usually wants ``1`` here: at the 300s
    ceiling, two attempts reserve a ~10 minute ``schedule_to_close``."""

    preflight_warmup_ceiling_seconds: ClassVar[int] = 600
    """How long the gate waits, from gate start, for the source's warmup.

    Read only when a run's first :meth:`Handler.warmup
    <application_sdk.handler.base.Handler.warmup>` probe is not ``READY`` —
    never for an app that does not override ``warmup``, whose default answers
    ``READY``. The gate then runs the ``PREFLIGHT`` tier, polls ``warmup`` on
    durable timers (5s doubling to 30s, or the source's ``next_poll_seconds``
    with a 5s floor), and runs the ``WARMUP`` tier once it reports ``READY``.
    The wait holds no worker slot, so this can be far longer than
    :attr:`preflight_gate_timeout_seconds`; the loop exits on the first
    terminal observation and uses the full ceiling only while the source keeps
    reporting it is not ready. Reaching it is
    ``SOURCE_UNAVAILABLE_WARMUP_EXHAUSTED``, and :attr:`preflight_warmup_mode`
    decides whether that blocks. Floor 30s, no cap (Snowflake declares 1800)."""

    preflight_warmup_probe_timeout_seconds: ClassVar[int] = 10
    """How long one ``warmup`` probe should take on warm compute.

    Passed to the handler as ``WarmupInput.probe_timeout_seconds`` and enforced
    around each probe; it also sizes each poll activity. Floor 1s, at most the
    ceiling."""

    preflight_warmup_mode = PreflightGateMode.SOFT
    """Whether a warmup that never got ready blocks the run, independent of
    :attr:`preflight_gate_mode`.

    Applies to the two outcomes the gate attributes to the source's compute:
    ``UNAVAILABLE`` and the ceiling. ``SOFT`` (default) reports them and lets
    the run proceed; ``HARD`` stops it. A typed AUTH / PERMISSION / NOT_FOUND
    raise from ``warmup`` is a verdict like any check's, so
    :attr:`preflight_gate_mode` decides that one. Assign it without an
    annotation (``preflight_warmup_mode = PreflightGateMode.HARD``)."""

    preflight_verify_storage: ClassVar[bool] = False
    """Also verify the run's artifact object store(s) in the preflight gate.

    ``False`` (default) keeps the gate source-only. ``True`` opts in to probing
    every store the run will upload artifacts to — a write, a HEAD, and a
    **multipart-forced** write. The multipart probe matters because uploads
    above the writer's part size (8 MiB default,
    ``ATLAN_STORAGE_UPLOAD_PART_SIZE_BYTES``) go out as multipart, and a store
    can accept plain PUTs while rejecting multipart initiation (GCS does
    exactly this for the whole window of a bucket relocation; a production RCA
    traced multi-hour extractions dying at the final upload to that
    condition). A failed probe — after the gate's retry attempts are exhausted
    — appends a typed platform-attributed check and downgrades a ``READY`` or
    ``PARTIAL`` verdict to ``NOT_READY``. In hard mode it blocks only when its
    category is in ``GATE_BLOCKING_CATEGORIES``: in-cluster stores are
    Atlan-side and map to ``DEPENDENCY_UNAVAILABLE``, which is reported as
    ``would_block``; in SDR mode a missing binding, denied grant or rejected
    credential on the customer's bucket blocks. Soft mode only reports it.

    Deliberately opt-in while the fleet's probe precision is measured: the probe
    costs under a second per run against healthy storage, but a hard-mode SDR
    app turning this on is trusting a storage canary to abort real runs — and the
    probe is stricter than a small-artifact-only workload's real writes, which
    never touch multipart at all.

    **Deployment prerequisite.** The probed buckets must carry an
    abort-incomplete-multipart-upload lifecycle rule. A probe cancelled between
    multipart initiate and complete — the gate's own timeout does exactly this —
    leaves hidden partial parts that the provider bills until they are aborted,
    and now once per run rather than once per worker boot. The SDK cannot clean
    them up itself: ``obstore`` owns the upload id inside ``put_async`` and
    exposes no abort call, so there is nothing to cancel against. Confirm the
    lifecycle rule on every bucket in the deployment before setting this
    ``True``; the SDK neither checks nor reports it."""

    artifact_validation_mode: ClassVar[Literal["hard", "soft"]] = "soft"
    """Artifact-validation posture (ADR-0020). ``"soft"`` (default) never blocks.

    Every ``FileReference`` crossing a ``@task`` boundary is checked against the
    artifact schema the app declared for that contract field, on both sides — at
    ingest (after materialise) and at hand-off (before persist). This attribute
    decides only what happens to a *negative* verdict:

    * ``"soft"`` — the outcome event carries ``artifact_enforcement="would_block"``
      and the hand-off proceeds. This is where every app starts.
    * ``"hard"`` — the same outcome fails the activity with a typed
      :class:`~application_sdk.validation.interceptor.ArtifactValidationBlockedError`.
      Blast radius is one workflow, and the blame lands on the task that was
      handed, or that produced, the artifact.

    ``ATLAN_ARTIFACT_VALIDATION_MODE`` overrides this at deploy time; any set value
    other than ``"hard"`` resolves to soft, and an empty or unset value is not an
    override. Only the literal ``"hard"`` enforces, so blocking is always a
    deliberate opt-in and a typo falls back to reporting.

    Two things are never subject to this posture. **A validator that breaks always
    fails open** — a defect in the SDK's own check may not fail a healthy run, in
    either posture — and it says so on the event as
    ``artifact_classification="validator_broken"``. And an *undeclared* artifact on
    an app-internal ``@task`` contract never blocks, because ADR-0020 makes
    declaration optional there on purpose; on an entrypoint's public boundary it is
    a finding like any other.

    Do not set ``"hard"`` before the app's false-positive rate has been measured
    from the outcome events (FND-694). ``ATLAN_VALIDATE_ARTIFACTS=false`` is the
    separate deployment kill switch that stops the check running at all — this
    attribute never turns reporting off."""

    # Marker to track if class has been registered
    _app_registered: ClassVar[bool] = False

    # Set by registration
    _app_name: str
    _app_version: str
    #: The image's build identity (FND-1684), or "" when the image carries no
    #: stamp. A ClassVar with a default, unlike its two neighbours: those are
    #: always written by registration, while this must read as "" on any App
    #: subclass constructed without going through it (test doubles, and every
    #: app registered by an SDK older than this attribute).
    _app_build_id: ClassVar[str] = ""
    _app_metadata: AppMetadata
    _input_type: type[Input]
    _output_type: type[Output]

    # Set by the execution layer before run() is called
    _context: AppContext | None = None
    # Set by the generated workflow for each run: the stored refs read off
    # every task's input and result, for cleanup_storage. None outside one.
    _run_stored_refs: "_RunStoredRefs | None" = None
    # BLDX-878: inter-app calls deactivated pending review.
    # _client: "WorkflowAppClient | None" = None
    _task_context: "TaskExecutionContext | None" = None

    def __init_subclass__(cls, **kwargs: object) -> None:
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

        # Before the registered-skip: a template or abstract base reports its
        # own bad declarations too. Warn-only until v4.0.0. Not in the
        # workflow sandbox, which re-imports app modules on every run.
        if not workflow.unsafe.in_sandbox():
            validate_app_config(cls)

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
        _require_aliases_not_expired(cls, app_name)
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
            # Own-class read on purpose: aliases name one specific app's wire
            # contract and must not propagate to subclasses through the MRO.
            legacy_workflow_types=cls.__dict__.get("legacy_workflow_types", {}),
        )
        # Rebuild the generated workflow classes on the sandbox's copy of the
        # app module. ``application_sdk.app`` and ``application_sdk.execution``
        # are passed through the sandbox, so the installer is the host's,
        # registered when the worker imported the generation module.
        if workflow.unsafe.in_sandbox() and _sandbox_workflow_installer is not None:
            _sandbox_workflow_installer(cls)

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
    def run_scratch_dir(self) -> str:
        """The run's local scratch directory, removed when the run ends.

        ``{TEMPORARY_PATH}/artifacts/apps/{app}/workflows/{workflow_id}/{run_id}``,
        e.g. ``./local/tmp/artifacts/apps/my-app/workflows/wf-123/run-456``.
        Use it instead of ``tempfile.gettempdir()`` for files a run writes
        locally:

        * It is the same string in ``run()`` and in every ``@task`` of the
          same run, so a path the workflow builds and passes to a task is the
          one the task writes to.
        * ``cleanup_files`` removes it at the end of the run (the default
          ``on_complete``), so nothing is left on the worker's disk.
        * It is safe in workflow code: it is computed from the run's
          identity with string-only ``os.path`` calls, never from
          ``tempfile``, the filesystem or the clock, which the workflow
          sandbox refuses.
        * It sits under ``TEMPORARY_PATH``, so a file under it maps to an
          object-store key the way every ``./local/tmp/...`` path does.

        The directory is not created; ``os.makedirs(..., exist_ok=True)`` it
        in a task before writing. Do not create it in ``run()``: workflow
        code must not touch the filesystem.

        Raises:
            AppContextError: If accessed outside ``run()`` or a ``@task``.
            RunTempDirEscapeError: If the run's workflow ID would place the
                directory outside ``TEMPORARY_PATH`` (e.g. ``..`` segments).
        """
        context = self.context
        return _run_scratch_dir(context.app_name, context.workflow_id, context.run_id)

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

    def heartbeat(self, *details: object) -> None:
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
        self,
        func: Callable[_P, T],
        *args: _P.args,
        **kwargs: _P.kwargs,
    ) -> T:
        """Run a blocking function in a thread pool.

        Only available in @task methods. The offload is automatically wrapped in
        an unbounded progress hold (ADR-0018), so a legitimately long blocking
        call is never read as a stall. To bound it instead, wrap the offload in
        :meth:`holding_progress` with the allowance you would give this one
        call::

            async with self.holding_progress("full table scan", timeout=7200):
                rows = await self.run_in_thread(cursor.execute, sql)

        Args:
            func: Blocking function to run. Bind a
                :class:`~application_sdk.execution.heartbeat.CancelHandle` to it
                (``handle.bind(func)``) to cancel the driver call when this task
                is cancelled.
            *args: Positional arguments for func.
            **kwargs: Keyword arguments for func, all passed through.

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

    def holding_progress(
        self, label: str, *, timeout: float | None
    ) -> AbstractAsyncContextManager[None]:
        """Vouch for one opaque operation for as long as you would let it run.

        Pauses the stall watchdog (ADR-0018) for a call the SDK cannot see into,
        and records the completed call as progress on exit::

            async with self.holding_progress("snapshot metadata query", timeout=1800):
                rows = await long_single_query(...)

        Only available in @task methods — outside one there is no attempt to
        vouch for, so a hold there would claim coverage it cannot provide.

        Args:
            label: The call site being vouched for. Must identify a *site*,
                never a query, key, credential or customer value.
            timeout: How long you would let this one operation run before you
                would rather it failed — not a prediction of its duration.
                Keyword-only and required; ``None`` declares an unbounded hold.

        Returns:
            An async context manager. Use it with ``async with``.

        Raises:
            AppContextError: If called outside a @task method.
        """
        if self._task_context is None:
            raise AppContextError(
                "holding_progress() can only be called inside @task methods."
            )
        return self._task_context.holding_progress(label, timeout=timeout)

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

    def get_build_id(self) -> str:
        """Get the build identity of the image this process runs from.

        ``""`` when the image carries no stamp. NOT a substitute for
        :meth:`get_version`: that is the app's declared semver and is the same
        for every build of the same source, while this changes whenever the
        image content does. See :mod:`application_sdk.common.build_identity`.
        """
        return self._app_build_id

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
        run = _current_run_key()
        with _app_state_lock:
            return _app_state.get(run, {}).get(key)

    def set_app_state(self, key: str, value: object) -> None:
        """Set in-memory state for the current app execution.

        Only available inside @task methods.

        Args:
            key: State key to store.
            value: Value to store (any Python object). One with ``aclose()``
                or ``close()`` is closed when the run's slot is released.

        Raises:
            AppContextError: If called outside a @task method.
        """
        run = _current_run_key()
        with _app_state_lock:
            _app_state.setdefault(run, {})[key] = value

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

    # No ``timeout_seconds`` (ADR-0018). 600s was a duration budget for "upload
    # whatever this caller has", and what that caller has scales with the tenant
    # — a directory upload of a large transformed tree can exceed it on a big
    # crawl and be killed mid-transfer, then killed again on each retry. The
    # transfer layer marks progress per file and per multipart part, so the stall
    # watchdog bounds a wedge in minutes and the backstop is the last resort.
    #
    # Residual, accepted: a *small* single-PUT file that hangs emits nothing
    # until it completes, so it holds until ``max_no_progress_seconds``. That is
    # the same quiet spot ADR-0018 accepts fleet-wide, and it is bounded by the
    # stall alert rather than by a number nobody can pick.
    @task(
        retry_max_attempts=_STORE_TRANSFER_MAX_ATTEMPTS,
        retry_initial_interval_seconds=_STORE_TRANSFER_INITIAL_INTERVAL_SECONDS,
        retry_max_interval_seconds=_STORE_TRANSFER_MAX_INTERVAL_SECONDS,
    )
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
        store otherwise.  When the upstream component is absent
        ``upstream_storage`` is ``None`` and routing falls back to the
        deployment store; when both store names name one component (the
        in-cluster wiring) ``upstream_storage`` *is* the deployment store, so
        the dual-write collapses to a single write against that one bucket.  In
        SDR deployments the upstream store is a distinct component — Atlan's
        bucket, the correct destination for extracted artifacts handed off to
        the publish app.

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
        return await self._upload_impl(input)

    async def _upload_impl(
        self, input: UploadInput, *, validate_assets: bool = True
    ) -> UploadOutput:
        """Body of :meth:`upload`, callable from inside another ``@task``.

        ``upload`` is a ``@task``, and a ``@task`` cannot call another one — so
        every framework task that needs the same store routing, dual-write
        fan-out and pre-handoff validation calls this instead of duplicating
        them. :meth:`upload_refs` is the other caller; it passes
        ``validate_assets=False`` because it validates its whole declaration as
        one batch instead of one file at a time (FND-3414).
        """
        return await _app_tasks.upload(self, input, validate_assets=validate_assets)

    # No ``timeout_seconds``, for the same reason as ``upload`` — symmetric knob,
    # symmetric problem, and ``transfer.download`` marks progress per file and
    # per range chunk.
    @task(
        retry_max_attempts=_STORE_TRANSFER_MAX_ATTEMPTS,
        retry_initial_interval_seconds=_STORE_TRANSFER_INITIAL_INTERVAL_SECONDS,
        retry_max_interval_seconds=_STORE_TRANSFER_MAX_INTERVAL_SECONDS,
    )
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
            containing both ``storage_path`` and ``local_path``.  A prefix
            (a ``storage_path`` ending in ``/``) with no objects is not an
            error: the ref names an empty local directory with
            ``file_count=0``, so check ``ref.file_count`` before handing the
            ref to another task.  A missing single key still raises.

        Example — SDR publish app consuming artifacts from the extract app::

            async def run(self, input: PublishInput) -> PublishOutput:
                dl = await self.download(DownloadInput(ref=input.artifacts_ref))
                return await self.publish_data(
                    PublishInput(local_path=dl.ref.local_path)
                )

        Example — re-materialise an existing FileReference::

            dl = await self.download(DownloadInput(ref=input.model_ref))
        """
        return await _app_tasks.download(self, input)

    # No ``timeout_seconds``: the work here is one store round-trip per declared
    # ref, so any number picked would scale with the size of the declaration —
    # which is the unguessable question ADR-0018 removed the knob for. The task
    # takes the 24h backstop and is bounded in minutes by the stall watchdog
    # instead, which the per-ref ``mark_progress`` below feeds.
    @task(
        retry_max_attempts=_STORE_TRANSFER_MAX_ATTEMPTS,
        retry_initial_interval_seconds=_STORE_TRANSFER_INITIAL_INTERVAL_SECONDS,
        retry_max_interval_seconds=_STORE_TRANSFER_MAX_INTERVAL_SECONDS,
    )
    async def verify_refs(self, input: VerifyRefsInput) -> VerifyRefsOutput:
        """Framework task: assert every declared ``FileReference`` is really there.

        Call this from ``run()`` at the point a step stops passing typed refs
        and starts passing a prefix — the moment the producer's declaration
        would otherwise be thrown away.

        A prefix is not a declaration.  Once a step hands one downstream, the
        consumer walks it and takes whatever the listing returns; a tree that
        is short by two entities is indistinguishable from a run that produced
        two fewer.  ``APP-CORRECTNESS-001`` is the rule this task implements:
        *verify cross-activity handoffs against a producer-written declaration,
        never by listing a storage prefix*.  The refs the producing tasks
        returned **are** that declaration — this task is what turns them from a
        value nobody read into an assertion.

        Each ref is checked by its ``storage_path``, used verbatim — matching
        how ``persist_file_reference`` wrote it, so the check asks about the
        key that actually exists rather than a re-normalised guess at it.

        A ref is a **directory** ref when its key ends in ``/`` (what the
        interceptor's directory branch writes) *or* its ``file_count`` is
        anything other than 1 (which catches a hand-pinned directory key with
        no slash).  Those are listed, excluding SHA-256 sidecars, and must
        yield at least ``file_count`` data objects.  Everything else is a
        single-file ref and is checked with a HEAD.

        When ``input.prefix`` is set, every ref must also resolve underneath
        it — a ref outside the prefix is data the downstream walk will never
        reach, which is the same hole by a different route.

        Args:
            input: ``VerifyRefsInput`` with the declared refs, the prefix they
                are expected to live under, and which store to check.

        Returns:
            ``VerifyRefsOutput`` with the verified counts.  Success is the only
            thing this task returns.

        Raises:
            StorageHandoffIncompleteError: If any declared ref is missing from
                the store, is short of its declared object count, declares zero
                files, or resolves outside ``input.prefix``.
            ObjectStoreNotConfiguredError: If the requested store is not bound.

        Example — SQL extraction asserting its transform outputs before
        handing the prefix to publish::

            results = await asyncio.gather(*transforms)
            refs = [r.transformed_file for r in results if r.transformed_file]
            await self.verify_refs(
                VerifyRefsInput(refs=refs, prefix=transformed_data_prefix)
            )
        """
        return await self._verify_refs_impl(input)

    async def _verify_refs_impl(self, input: VerifyRefsInput) -> VerifyRefsOutput:
        """Body of :meth:`verify_refs`, callable from inside another ``@task``.

        :meth:`upload_refs` uses it to check its own delivery without calling a
        ``@task`` from inside a ``@task``.
        """
        return await _app_tasks.verify_refs(self, input)

    # No ``timeout_seconds``, for the same reason as ``verify_refs`` — more
    # sharply here, because a declared ref can point at a directory of any size.
    # A duration budget for "upload this tenant's transformed tree" is a moving
    # target by construction (ADR-0018 Problem 1). Progress is already marked per
    # file by ``storage.transfer.upload``, so the watchdog covers the loop.
    @task(
        retry_max_attempts=_STORE_TRANSFER_MAX_ATTEMPTS,
        retry_initial_interval_seconds=_STORE_TRANSFER_INITIAL_INTERVAL_SECONDS,
        retry_max_interval_seconds=_STORE_TRANSFER_MAX_INTERVAL_SECONDS,
    )
    async def upload_refs(self, input: UploadRefsInput) -> UploadRefsOutput:
        """Framework task: deliver a declaration as one outbound tree.

        The fan-in counterpart to :meth:`verify_refs`.  Where ``verify_refs``
        *asserts* a declaration, this one *delivers* it: it takes the
        ``FileReference`` list a fanned-out step produced and lands every one
        of them under a single destination prefix — by reference, never by
        scanning a directory.

        This is the shape every connector that crosses the SDR store boundary
        needs and has so far hand-rolled.  The hand-rolled version that scans
        ``os.path.join(output_path, "transformed")`` is wrong on any run where
        the transforms fanned out: that directory was written by the activities,
        so the calling pod sees only the subset that happened to run locally —
        and on a fully distributed run, nothing at all.  Passing each ref lets
        the SDK stream from the deployment store for the files this pod never
        held, which is the normal case once activities are distributed.

        Three behaviours the hand-rolled loop has to remember and this does not
        make optional:

        * **Every ref is uploaded with** ``raise_on_empty=True``.  A declared
          file that contributes zero objects is a bug, not a quiet day.
        * **An empty declaration returns an empty prefix.**  ``prefix`` comes
          back as ``""`` when there was nothing to deliver, so the caller hands
          ``""`` downstream rather than a prefix naming an empty tree — an
          empty tree is not a no-op to a consumer that diffs against it, it is
          "delete everything".
        * **The delivery is verified against the declaration** before it is
          returned (unless ``verify=False``), in the store it was written to.

        Destination keys: ``{prefix}/{label}`` when a
        :class:`~application_sdk.contracts.storage.DeclaredFile` carries a
        ``label``, otherwise ``{prefix}/{leaf}`` where *leaf* is the ref's own
        key with ``input.source_prefix`` removed — so a set of refs at
        ``<run>/transformed/<entity>/entities.json`` keeps its
        ``<entity>/entities.json`` shape under the new prefix.  A declared file
        that yields neither is an error rather than a guess: a rule that
        recovers the entity segment from four refs and drops it from one turns
        a small run into a differently-shaped tree, which is the failure this
        task exists to stop.

        Store routing matches :meth:`upload`: the upstream store when one is
        configured (SDR), the deployment store otherwise.

        Args:
            input: ``UploadRefsInput`` with the declaration, the destination
                prefix, the tier, and whether to verify the delivery.

        Returns:
            ``UploadRefsOutput`` with the prefix actually delivered to (``""``
            for an empty declaration), the durable destination refs, and the
            total object count.

        Raises:
            StorageEmptyUploadError: If a declared file uploaded zero objects.
            StorageHandoffIncompleteError: If the delivered tree does not check
                out against the declaration.

        Example — a fanned-out connector handing its transformed tree to
        publish::

            delivered = await self.upload_refs(
                UploadRefsInput(
                    files=[DeclaredFile(ref=r, label=t) for t, r in transformed],
                    prefix=f"artifacts/apps/{app}/workflows/{wf_id}/{run_id}/transformed",
                )
            )
            return MyOutput(transformed_data_prefix=delivered.prefix)

        Example — a ``SqlApp`` connector bridging its own declaration across
        the SDR store boundary, keeping the key shape publish expects::

            delivered = await self.upload_refs(
                UploadRefsInput(
                    files=[DeclaredFile(ref=r) for r in out.transformed_files],
                    source_prefix=out.transformed_data_prefix,
                    prefix=out.transformed_data_prefix,
                )
            )
        """
        return await _app_tasks.upload_refs(self, input)

    # Keeps its ``timeout_seconds``, unlike upload/download/verify_refs/upload_refs.
    # The ADR-0018 backstop only replaces a duration bound where the work emits
    # progress the stall watchdog can read; this task deletes local paths and
    # marks nothing, so the duration is still its only bound. Removing it here
    # would trade a 300s kill for a silent hold, which is the opposite of the
    # trade the ADR makes. Giving cleanup a progress hook is the prerequisite.
    @task(timeout_seconds=300, retry_max_attempts=3, heartbeat_timeout_seconds=60)
    async def cleanup_files(self, input: CleanupInput) -> CleanupOutput:
        """Framework task: clean up local files after a workflow run.

        Removes three categories of local files:

        1. ``FileReference`` local paths tracked during the run (auto-materialised
           or persisted files, including their ``.sha256`` sidecars).
        2. The private directories ``resolve_credential_file`` and
           ``credential_dir`` made for the run on this worker. Always removed,
           whatever ``input.extra_paths`` says. Those on another worker are
           removed by that worker's sweeper once the run has closed.
        3. Convention-based temp directories: ``input.extra_paths`` if provided,
           otherwise ``CLEANUP_BASE_PATHS`` / ``TEMPORARY_PATH + build_output_path()``
           — which is also where path-less ``FileReference`` objects materialise.

        All errors are swallowed per-path so a cleanup failure never fails the
        workflow.

        Call this from ``on_complete()`` (the default implementation does so
        automatically).  Do not call it directly from ``run()``.
        """
        return await _app_tasks.cleanup_files(self, input, state=TaskStateAccessor())

    # Keeps its ``timeout_seconds`` for the same reason as ``cleanup_files``, and
    # more strongly: heartbeating is disabled on this task outright, so there is
    # no watchdog here at all. The duration is the only thing bounding it.
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
           Tracked means the refs this worker's tasks took or returned, plus
           ``input.file_refs`` — the stored refs the workflow read off every
           task's input and result, whichever worker ran it. The default
           ``on_complete`` fills it in.
           Each key and its ``.sha256`` sidecar are deleted, and a directory
           ref's manifest (``manifest_path``) with it.
           ``RETAINED`` and ``PERSISTENT`` tier refs are skipped, and so is
           any key a ``TRANSIENT`` ref shares with one of them — a file key,
           its sidecar, or a key under a durable directory ref. Each such
           clash counts as skipped and logs a WARNING.
        2. **Run-scoped prefix** (opt-in via ``input.include_prefix_cleanup``):
           all objects under ``artifacts/apps/{app}/workflows/{wf_id}/{run_id}/``,
           which includes any ``RETAINED``-tier refs from this run. This pass
           deliberately ignores tracked tiers: opting in is the documented
           way to remove ``RETAINED`` refs.

        Objects under ``PROTECTED_STORAGE_PREFIXES`` (``persistent-artifacts/``,
        ``connection-cache/``) are never deleted. Pass 1 also skips every ref
        tagged ``store=UPSTREAM``: those bytes are in the upstream store, not
        the deployment store this task deletes from. Pass 2 does not consult
        refs: every deployment key under this run's prefix belongs to this
        run, including the deployment copy ``App.upload`` dual-writes beside
        an UPSTREAM ref in a two-store deployment.

        If no object store is configured (local dev), returns immediately with
        zero counts.  Individual delete errors increment ``error_count`` but
        never abort the task.
        """
        return await _app_tasks.cleanup_storage(self, input, state=TaskStateAccessor())

    # A bounded merge of at most MAX_SKIPPED_RECORDS lines; the duration bound
    # is enough, as for cleanup_files.
    @task(timeout_seconds=300, retry_max_attempts=3, heartbeat_timeout_seconds=60)
    async def merge_skipped_records(
        self, input: MergeSkippedRecordsInput
    ) -> MergeSkippedRecordsOutput:
        """Framework task: merge the run's skipped-record parts into one artifact.

        Scheduled by the workflow after ``run()`` returns, and only when the
        run's ``skipped-records`` metric is above zero, so a run that skipped
        nothing has no ``skipped-records`` artifact at all. Concatenates the
        per-activity parts under ``{run prefix}/skipped-records/parts/`` into
        ``{run prefix}/skipped-records.ndjson`` (capped at
        ``MAX_SKIPPED_RECORDS`` lines), adds it as the run artifact
        ``skipped-records``, and records ``skipped-records-truncated`` for every
        skip that was counted but neither kept nor lost to a storage failure.
        See ``application_sdk.outputs.skipped``.
        """
        from application_sdk.execution import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
            build_output_path,
        )
        from application_sdk.execution._temporal.activity_utils import (  # noqa: PLC0415 — circular: as above
            run_temp_dir,
        )
        from application_sdk.outputs.skipped import (  # noqa: PLC0415
            SKIPPED_RECORDS_TRUNCATED_METRIC,
            merge_run_skips,
        )

        merged = await merge_run_skips(build_output_path(), run_temp_dir())
        truncated = max(0, input.skipped_count - input.unpersisted_count - merged.kept)
        if truncated:
            get_outputs().add_metric(
                Metric(name=SKIPPED_RECORDS_TRUNCATED_METRIC, value=truncated)
            )
        return MergeSkippedRecordsOutput(
            key=merged.key, kept=merged.kept, truncated=truncated
        )

    # A bounded merge of at most MAX_SKIPPED_RECORDS lines, as above.
    @task(timeout_seconds=300, retry_max_attempts=3, heartbeat_timeout_seconds=60)
    async def merge_fetch_failures(
        self, input: MergeFetchFailuresInput
    ) -> MergeFetchFailuresOutput:
        """Framework task: merge the run's fetch-failure parts into one artifact.

        Scheduled by the workflow after ``run()`` returns, and only when the
        run's ``fetch-failures`` metric is above zero. Concatenates the
        per-activity parts under ``{run prefix}/fetch-failures/parts/`` into
        ``{run prefix}/fetch-failures.ndjson`` (capped at
        ``MAX_SKIPPED_RECORDS`` lines), adds it as the run artifact
        ``fetch-failures``, and records ``fetch-failures-truncated`` for every
        failure that was counted but neither kept nor lost to a storage
        failure. See ``application_sdk.outputs.fetch_failures``.
        """
        from application_sdk.execution import (  # noqa: PLC0415 — circular: execution/__init__.py loads _temporal which imports app.base
            build_output_path,
        )
        from application_sdk.execution._temporal.activity_utils import (  # noqa: PLC0415 — circular: as above
            run_temp_dir,
        )
        from application_sdk.outputs.skipped import merge_run_log  # noqa: PLC0415

        merged = await merge_run_log(
            build_output_path(), run_temp_dir(), FETCH_FAILURES_LAYOUT
        )
        truncated = max(0, input.failure_count - input.unpersisted_count - merged.kept)
        if truncated:
            get_outputs().add_metric(
                Metric(name=FETCH_FAILURES_TRUNCATED_METRIC, value=truncated)
            )
        return MergeFetchFailuresOutput(
            key=merged.key, kept=merged.kept, truncated=truncated
        )

    async def on_complete(self) -> None:
        """Lifecycle hook called after ``run()`` finishes (success or failure).

        The default implementation deletes local files produced during the run
        when cleanup is enabled (``APPLICATION_SDK_ENABLE_CLEANUP_INTERCEPTOR``
        not set to a falsy value). Inside a workflow the worker reads that
        variable once at start and pins its value on each run at the run's
        first workflow task, so a run replays with the cleanup it started
        with whatever the replaying pod's value is. Called outside a
        workflow, it reads the variable.

        Override this method to add custom post-run logic.  Call
        ``await super().on_complete()`` to preserve the default file cleanup::

            async def on_complete(self) -> None:
                await self.send_notification()
                await super().on_complete()
        """
        # Passed through so the sandbox sees the module the worker's
        # RunFlagsInterceptor set the run's flags in, not a fresh copy.
        with workflow.unsafe.imports_passed_through():
            from application_sdk.execution._temporal.run_flags import (  # noqa: PLC0415 — circular: execution imports app.base
                cleanup_enabled,
            )

        if cleanup_enabled():

            async def _local_cleanup() -> None:
                try:
                    await self.cleanup_files(CleanupInput())
                except Exception:  # noqa: BLE001 — logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
                    _safe_log(
                        "warning",
                        "cleanup_files task failed during on_complete",
                        exc_info=True,
                    )

            async def _storage_cleanup() -> None:
                try:
                    stored = self._run_stored_refs
                    await self.cleanup_storage(
                        StorageCleanupInput(
                            file_refs=stored.for_cleanup() if stored else []
                        )
                    )
                except Exception:  # noqa: BLE001 — logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
                    _safe_log(
                        "warning",
                        "cleanup_storage task failed during on_complete",
                        exc_info=True,
                    )

            await asyncio.gather(_local_cleanup(), _storage_cleanup())

        # Inert when this runs inside the generated Temporal workflow (the
        # normal case — on_complete() is called from _run()'s finally). Both
        # legs self-guard on ``utils.in_temporal_workflow``: the object-store
        # sink in ``AtlanObservability._flush_records`` (which returns the
        # records to the buffer for the worker's periodic flush) and the
        # Segment drain in ``SegmentClient.flush()``. Neither is a loss — OTLP
        # export is per-call, and queued Segment events go out on the worker's
        # batch timer, with ``close()`` draining the remainder at process exit. The call is kept
        # for the non-workflow callers of on_complete() (direct invocation,
        # tests, and any subclass that calls super() outside a workflow).
        try:
            await AtlanObservability.flush_all()
        except Exception:  # noqa: BLE001 — logged via _safe_log with exc_info=True; checker does not recognise _safe_log as a logger attribute call
            _safe_log("warning", "flush_all() failed during on_complete", exc_info=True)


#: Rebuilds an app's generated workflow classes inside the Temporal sandbox.
#: Set by ``execution._temporal.app_workflow`` when it is imported, which the
#: worker does before any workflow runs, so this module never imports the
#: execution runtime (FND-4263). ``None`` only in a process that has not built
#: a worker, where nothing runs in a sandbox.
_sandbox_workflow_installer: "Callable[[type[App]], None] | None" = None


# =============================================================================
# Registration helpers — see application_sdk/app/_ep_registration.py
# =============================================================================
# _register_tasks, _collect_implicit_ep, _scan_entrypoints,
# _build_entry_points, _apply_app_registration are imported at the top of this
# module from _ep_registration and re-exported via __all__ for backward compat.


async def _run_preflight_gate(
    input_data: Input,
    app_name: str,
    entrypoint: str,
    budget_seconds: int | None = None,
    max_attempts: int | None = None,
    gate_mode: object = None,
    warmup_ceiling_seconds: object = None,
    warmup_probe_timeout_seconds: object = None,
    warmup_mode: object = None,
) -> None:
    """Run the SDK-owned pre-extraction preflight gate (HYP-1883).

    Dispatches the app's preflight handler as a mandatory first activity. The
    activity holds the verdict and the mode, so it emits the ``proceeded``,
    ``would_block`` and ``blocked`` rows and raises the deliberate
    ``PreflightFailed`` block itself; this frame re-raises that block unchanged.

    When the activity returns no verdict, this frame classifies the failure from
    what survived in the chain (``classify_gate_failure``) and applies the mode
    to anything the chain attributes to the source or the handler: the typed
    evidence of an earlier attempt, or a frame Temporal ended without one. Only
    the gate's own plumbing fails open, as ``no_verdict`` with ``gate_broken``.
    Nothing on this path may raise anything but the block: an escaping error is
    a workflow *task* failure, which Temporal retries indefinitely.

    Guards: ``workflow.patched("preflight-gate")`` keeps pre-gate runs replaying
    deterministically; ``isinstance(input_data, CredentialResolvable)`` skips
    source-less apps. Both emit a ``skipped`` row so a gate that never ran is
    visible rather than silent. Credential resolution happens inside the activity
    — the deterministic workflow forwards only secret-free references.

    The patch id is deliberately not bumped for the hard-mode block this frame
    now raises on a dead gate: a run in flight across the upgrade whose gate
    failed without a block would replay into a raise here, but workers pin
    their deployment version (``default_versioning_behavior = PINNED``), so
    such a run finishes on the build that started it. Only a deployment with no
    ``ATLAN_APP_BUILD_ID`` — local, self-deployed — can replay across the change.

    ``budget_seconds``, ``max_attempts`` and ``gate_mode`` come from the app's
    declared ``App.preflight_gate_*`` attributes (``ClassVar``s, so reading them
    here is replay-deterministic); ``None`` means the SDK default, and the clamps
    and the mode coercion live in the gate module so this frame and the worker
    cannot disagree. Every row this frame emits is built by the same
    ``gate_outcome_row`` the activity uses, so a consumer parsing ``gate_mode``
    or ``gate_attempt`` never drops the rows that prove a gate never ran or never
    returned.

    **Warmup phase (FND-3039).** Before any check, this frame dispatches one
    ``{app}:preflight_warmup`` activity: one ``Handler.warmup`` probe with its
    own timeout (the app's probe timeout plus slack), so the probe never spends
    the check activity's budget. ``READY`` — what an app that does not override
    ``warmup`` always answers — is followed by one check dispatch with every
    tier, exactly as before tiers. Otherwise the check activity runs the
    ``PREFLIGHT`` tier, and once that dispatch lets the run go on, this frame
    waits: further probes separated by durable timers, so the wait holds no
    worker slot. On ``READY`` it dispatches the check activity again with
    ``tiers={WARMUP}``. ``UNAVAILABLE`` and the ceiling (from gate start) are
    attributed to the source and ``warmup_mode`` alone decides whether they
    block; a typed AUTH / PERMISSION / NOT_FOUND raise from a probe is a
    verdict, gated on its category under ``gate_mode``. A first probe whose own
    plumbing fails falls back to one dispatch with every tier (its row says
    ``warmup_outcome=broken``); a later one fails open as ``no_verdict`` /
    ``gate_broken``.

    **Replay.** ``workflow.patched("preflight-gate-warmup")`` guards this
    phase. PINNED workers drain a run on the build that started it, but an app
    can opt into ``AUTO_UPGRADE`` (``TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR``),
    which migrates in-flight runs onto a new build, and a deployment with no
    ``ATLAN_APP_BUILD_ID`` is unversioned. A run started before the phase
    recorded the check activity as its first command, so on replay it takes the
    unpatched branch: one check dispatch with every tier and no probe, exactly
    the sequence it recorded. Every new run records the patch marker.
    """
    with workflow.unsafe.imports_passed_through():
        from application_sdk.credentials.ref import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            CredentialResolvable,
        )
        from application_sdk.errors.categories import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            Audience,
        )
        from application_sdk.errors.wire import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            FailureDetails,
        )
        from application_sdk.execution._temporal.preflight_gate import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            PREFLIGHT_OUTCOME_EVENT,
            WARMUP_TRANSITIONS_MAX,
            PreflightClassification,
            PreflightGateInput,
            PreflightRowOutcome,
            WarmupOutcome,
            WarmupPoll,
            WarmupTransition,
            WarmupWait,
            build_workflow_block,
            classify_gate_failure,
            coerce_gate_mode,
            frame_death_details,
            gate_blocks,
            gate_budget_seconds,
            gate_heartbeat_timings,
            gate_outcome_level,
            gate_outcome_row,
            gate_retry_policy,
            gate_timeouts,
            is_preflight_block,
            preflight_gate_activity_name,
            preflight_warmup_activity_name,
            underlying_error_type,
            warmup_activity_timeouts,
            warmup_exhausted_details,
            warmup_retry_policy,
            warmup_unavailable_details,
            warmup_waiting_details,
        )
        from application_sdk.handler._warmup import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            warmup_ceiling_seconds as _warmup_ceiling_seconds,
        )
        from application_sdk.handler._warmup import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            warmup_poll_delay,
        )
        from application_sdk.handler._warmup import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            warmup_probe_timeout_seconds as _warmup_probe_timeout_seconds,
        )
        from application_sdk.handler.contracts import (  # noqa: PLC0415 — temporal workflow sandbox: import must be inside imports_passed_through()
            CheckTier,
            PreflightCheck,
            WarmupObservation,
            WarmupState,
        )

    entry = entrypoint or "<implicit>"
    mode = coerce_gate_mode(gate_mode)
    budget, _ = gate_budget_seconds(budget_seconds)

    def _emit_row(
        outcome: PreflightRowOutcome,
        reason: str,
        classification: PreflightClassification,
        duration_ms: float,
        *,
        attempt: int,
        checks: list[PreflightCheck] | None = None,
        audience: str | None = None,
        primary: FailureDetails | None = None,
        exc_info: bool = False,
        tier: CheckTier | None = None,
        warmup_seen: WarmupWait | None = None,
        row_mode: PreflightGateMode | None = None,
    ) -> None:
        checks = checks or []
        row = gate_outcome_row(
            app_name=app_name,
            entrypoint=entry,
            outcome=outcome,
            reason=reason,
            checks=checks,
            mode=row_mode or mode,
            classification=classification,
            duration_ms=duration_ms,
            budget_seconds=budget,
            attempt=attempt,
            audience=audience,
            primary=primary,
            tier=tier,
            warmup=warmup_seen,
        )
        if exc_info:
            row["exc_info"] = True
        _safe_log(
            gate_outcome_level(outcome, classification, checks),
            PREFLIGHT_OUTCOME_EVENT,
            **row,
        )

    def _emit_skipped(reason: str) -> None:
        _emit_row(
            PreflightRowOutcome.SKIPPED,
            reason,
            PreflightClassification.NOT_RUN,
            0.0,
            attempt=0,
        )

    if not workflow.patched("preflight-gate"):
        _emit_skipped("pre_gate_replay")
        return

    if not isinstance(input_data, CredentialResolvable):
        _emit_skipped("input_not_credential_resolvable")
        return

    def _elapsed_ms() -> float:
        return round((workflow.now() - dispatched_at).total_seconds() * 1000, 1)

    def _no_verdict(
        e: Exception,
        tier: CheckTier | None,
        warmup_seen: WarmupWait | None = None,
    ) -> None:
        """Apply the mode to a check dispatch that returned no verdict.

        Re-raises the activity's deliberate block unchanged; otherwise
        classifies from the chain, fails open on the gate's own plumbing, and
        blocks or reports anything attributed to the source, as
        ``gate_blocks`` decides from the posture and the evidence's category.
        """
        # The activity emits the blocked outcome event before it raises; the
        # workflow re-raises the deliberate block unchanged.
        if is_preflight_block(e):
            raise e
        failure = classify_gate_failure(e)
        if failure.classification is PreflightClassification.GATE_BROKEN:
            _emit_row(
                PreflightRowOutcome.NO_VERDICT,
                underlying_error_type(e),
                failure.classification,
                _elapsed_ms(),
                attempt=failure.attempt,
                audience=Audience.APP_OWNER.value,
                # The plumbing error's own details[0], read off the chain by
                # classify_gate_failure when it can be parsed — _plumbing_error
                # leaves it there for exactly this consumer. None only for a bare
                # exception that carried none; the row then has the stack trace
                # and no sentence. Same ladder as the other two branches.
                primary=failure.evidence,
                exc_info=True,
                tier=tier,
                warmup_seen=warmup_seen,
            )
            return
        evidence = failure.evidence or frame_death_details(e, app_name, budget)
        blocks = gate_blocks(mode, evidence.category)
        _emit_row(
            PreflightRowOutcome.BLOCKED if blocks else PreflightRowOutcome.WOULD_BLOCK,
            evidence.code,
            failure.classification,
            _elapsed_ms(),
            attempt=failure.attempt,
            checks=failure.checks,
            audience=evidence.audience.value,
            primary=evidence,
            exc_info=True,
            tier=tier,
            warmup_seen=warmup_seen,
        )
        if blocks:
            raise build_workflow_block(
                evidence, failure.checks, app_name, failure.attempt
            ) from e

    ceiling, _ = _warmup_ceiling_seconds(warmup_ceiling_seconds)
    probe_timeout, _ = _warmup_probe_timeout_seconds(
        warmup_probe_timeout_seconds, ceiling
    )
    dispatched_at = workflow.now()
    warmup_posture = coerce_gate_mode(warmup_mode)
    deadline = dispatched_at + timedelta(seconds=ceiling)
    transitions: list[WarmupTransition] = []
    warmup_s2c, warmup_sc2 = warmup_activity_timeouts(probe_timeout)

    def _observe(observation: WarmupObservation) -> None:
        """Record *observation* if its state changed; refresh the health line."""
        if not transitions or transitions[-1].state is not observation.state:
            transitions.append(
                WarmupTransition(state=observation.state, at_ms=_elapsed_ms())
            )
        if observation.state.is_pending:
            _set_health_line(warmup_waiting_details(observation, _elapsed_ms() / 1000))

    def _wait(outcome: WarmupOutcome) -> WarmupWait:
        return WarmupWait(
            outcome=outcome,
            duration_ms=_elapsed_ms(),
            transitions=transitions[:WARMUP_TRANSITIONS_MAX],
        )

    async def _poll() -> WarmupPoll:
        """One ``{app}:preflight_warmup`` activity: its own timeout, never the
        check activity's budget."""
        return await workflow.execute_activity(
            preflight_warmup_activity_name(app_name),
            gate_input,
            result_type=WarmupPoll,
            schedule_to_close_timeout=warmup_sc2,
            start_to_close_timeout=warmup_s2c,
            retry_policy=warmup_retry_policy(),
        )

    def _terminal(error: FailureDetails) -> None:
        """A typed AUTH / PERMISSION / NOT_FOUND raise from the probe: a verdict
        on the source like any handler raise, so category gating under the
        gate posture."""
        _set_health_line("")
        blocks = gate_blocks(mode, error.category)
        _emit_row(
            PreflightRowOutcome.BLOCKED if blocks else PreflightRowOutcome.WOULD_BLOCK,
            error.code,
            PreflightClassification.SOURCE_UNVERIFIABLE,
            _elapsed_ms(),
            attempt=0,
            audience=error.audience.value,
            primary=error,
            tier=CheckTier.WARMUP,
            warmup_seen=_wait(WarmupOutcome.FAILED),
        )
        if blocks:
            raise build_workflow_block(error, [], app_name, 0)

    try:
        # Inside the guard: an exception escaping here would become a workflow
        # *task* failure, which Temporal retries indefinitely (see
        # _validate_workflow_input). Nothing on the gate's own path may do that.
        start_to_close, schedule_to_close = gate_timeouts(budget, max_attempts)
        heartbeat_timeout, _ = gate_heartbeat_timings(start_to_close.total_seconds())
        gate_input = PreflightGateInput.from_extraction_input(input_data, entrypoint)
    except Exception as e:  # noqa: BLE001 — _no_verdict emits the NO_VERDICT row with exc_info and raises the block when the posture blocks
        _no_verdict(e, None, None)
        return

    async def _dispatch_checks(
        tiers: frozenset[CheckTier] | None, warmup_seen: WarmupWait | None
    ) -> None:
        await workflow.execute_activity(
            preflight_gate_activity_name(app_name),
            gate_input.model_copy(update={"tiers": tiers, "warmup": warmup_seen}),
            schedule_to_close_timeout=schedule_to_close,
            start_to_close_timeout=start_to_close,
            heartbeat_timeout=timedelta(seconds=heartbeat_timeout),
            retry_policy=gate_retry_policy(max_attempts),
        )

    # A run started before the warmup phase existed recorded the check activity
    # as its first command. An AUTO_UPGRADE app (TEMPORAL_DEFAULT_VERSIONING_
    # BEHAVIOR) migrates such a run onto this build, so it must replay the old
    # sequence: one check dispatch with every tier, no probe.
    if not workflow.patched("preflight-gate-warmup"):
        try:
            await _dispatch_checks(None, None)
        except Exception as e:  # noqa: BLE001 — _no_verdict emits the NO_VERDICT row with exc_info and raises the block when the posture blocks
            _no_verdict(e, None, None)
        return

    # The first warmup probe is its own activity, ahead of the checks, so it
    # never spends the check budget. READY — what every app that does not
    # override Handler.warmup answers — sends one check dispatch with every
    # tier, exactly as before tiers.
    first_poll: WarmupPoll | None = None
    try:
        first_poll = await _poll()
    except Exception:  # noqa: BLE001 — _safe_log reports the probe failure at WARNING with exc_info; the gate fails open by design
        # The probe's own plumbing failed: run every tier, as before tiers, and
        # say so on that row. A broken probe must not cost the run its verdict.
        _safe_log(
            "warning",
            "Preflight warmup probe failed; running every check tier",
            app_name=app_name,
            entrypoint=entry,
            exc_info=True,
        )
    if first_poll is not None and first_poll.is_terminal and first_poll.error:
        _observe(first_poll.observation)
        _terminal(first_poll.error)
        return
    seen = first_poll.observation if first_poll is not None else None
    if seen is None or seen.state is WarmupState.READY:
        try:
            await _dispatch_checks(
                None,
                None if seen is not None else WarmupWait(outcome=WarmupOutcome.BROKEN),
            )
        except Exception as e:  # noqa: BLE001 — _no_verdict emits the NO_VERDICT row with exc_info and raises the block when the posture blocks
            _no_verdict(e, None, None)
        # Success: the activity already emitted the proceeded outcome event.
        return

    _observe(seen)
    first_wait = WarmupWait(
        outcome=WarmupOutcome.UNAVAILABLE
        if seen.state is WarmupState.UNAVAILABLE
        else WarmupOutcome.WARMING
    )
    try:
        await _dispatch_checks(frozenset({CheckTier.PREFLIGHT}), first_wait)
    except Exception as e:  # noqa: BLE001 — _no_verdict emits the NO_VERDICT row with exc_info and raises the block when the posture blocks
        _set_health_line("")
        _no_verdict(e, CheckTier.PREFLIGHT, first_wait)
        return

    terminal: FailureDetails | None = None
    last_error: FailureDetails | None = (
        first_poll.error if first_poll is not None else None
    )
    unhinted = 0
    # Set only by a poll after the PREFLIGHT dispatch; the first probe ran
    # before it, so it is in time by construction.
    late_ready = False
    try:
        while seen.state.is_pending:
            remaining = (deadline - workflow.now()).total_seconds()
            if remaining <= 0:
                break
            delay = warmup_poll_delay(unhinted, seen.next_poll_seconds, remaining)
            if seen.next_poll_seconds is None:
                unhinted += 1
            if delay > 0:
                await workflow.sleep(timedelta(seconds=delay))
            poll = await _poll()
            if poll.is_terminal:
                terminal = poll.error
                break
            last_error = poll.error
            seen = poll.observation
            _observe(seen)
            # The deadline is checked again when the poll returns: one that
            # started inside the ceiling can finish outside it, and a READY that
            # arrives late does not buy the WARMUP checks extra time.
            late_ready = seen.state is WarmupState.READY and workflow.now() >= deadline
    except Exception as e:  # noqa: BLE001 — _emit_row records the GATE_BROKEN row with exc_info; the gate fails open by design
        # The poll activity turns everything the probe does into a WarmupPoll,
        # so what reaches here is the gate's own plumbing: fail open.
        _set_health_line("")
        _emit_row(
            PreflightRowOutcome.NO_VERDICT,
            underlying_error_type(e),
            PreflightClassification.GATE_BROKEN,
            _elapsed_ms(),
            attempt=0,
            audience=Audience.APP_OWNER.value,
            exc_info=True,
            tier=CheckTier.WARMUP,
            warmup_seen=_wait(WarmupOutcome.BROKEN),
        )
        return
    _set_health_line("")

    if terminal is not None:
        _terminal(terminal)
        return

    if seen.state is WarmupState.READY and not late_ready:
        ready = _wait(WarmupOutcome.READY)
        try:
            await _dispatch_checks(frozenset({CheckTier.WARMUP}), ready)
        except Exception as e:  # noqa: BLE001 — _no_verdict emits the NO_VERDICT row with exc_info and raises the block when the posture blocks
            _no_verdict(e, CheckTier.WARMUP, ready)
        return

    # UNAVAILABLE, or still warming at the ceiling: the WARMUP checks cannot
    # run, and the reason is the source's compute. The warmup posture alone
    # decides here, not gate_blocks: both are SOURCE_UNAVAILABLE, which
    # gate_blocks would block on, and waiting for compute is the warmup
    # posture's call (FND-3036, FND-3774).
    blocks = warmup_posture.enforces
    if seen.state is WarmupState.UNAVAILABLE:
        evidence = warmup_unavailable_details(seen, app_name)
        _emit_row(
            PreflightRowOutcome.BLOCKED if blocks else PreflightRowOutcome.WOULD_BLOCK,
            evidence.code,
            PreflightClassification.SOURCE_UNVERIFIABLE,
            _elapsed_ms(),
            attempt=0,
            audience=evidence.audience.value,
            primary=evidence,
            tier=CheckTier.WARMUP,
            warmup_seen=_wait(WarmupOutcome.UNAVAILABLE),
            row_mode=warmup_posture,
        )
    else:
        evidence = warmup_exhausted_details(seen, app_name, ceiling, last_error)
        _emit_row(
            PreflightRowOutcome.WARMUP_EXHAUSTED,
            evidence.code,
            PreflightClassification.SOURCE_UNVERIFIABLE,
            _elapsed_ms(),
            attempt=0,
            audience=evidence.audience.value,
            primary=evidence,
            tier=CheckTier.WARMUP,
            warmup_seen=_wait(WarmupOutcome.EXHAUSTED),
            # The posture that decided this row, so a reader can tell a stopped
            # run from a reported one without the app's config.
            row_mode=warmup_posture,
        )
    if blocks:
        raise build_workflow_block(evidence, [], app_name, 0)


def _set_health_line(text: str) -> None:
    """Set the run's Temporal current details — the line its health view shows.

    Best effort: the line is a courtesy, and anything escaping here would be a
    workflow *task* failure, which Temporal retries indefinitely. Emits no
    command, so it is replay-safe and needs no patch marker. ``""`` clears it.
    """
    try:
        workflow.set_current_details(text)
    except Exception:  # noqa: BLE001 — a status line must never fail the gate; logged at DEBUG with exc_info=True
        _safe_log("debug", "Could not set the gate's health line", exc_info=True)


# Keep FileReference accessible via base module for convenience
__all__ = [
    "AppContextError",
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
    "_pascal_to_kebab",
    "_register_tasks",
    "_safe_log",
    "_safe_now",
    "_safe_uuid",
    "_scan_entrypoints",
    "task",
]

#: Names that moved out of this module and are served, with a warning, from
#: where they live now.
_DEPRECATED_CONSTANTS: dict[str, str | tuple[str, str]] = {
    "generate_workflow_class": (
        "generate_workflow_class is deprecated with no replacement: the worker "
        "builds every App's workflow classes, so an app has no reason to call "
        "it. Will be removed in v4.0.0."
    ),
}

if TYPE_CHECKING:
    # __getattr__ can only be typed -> object; this restores the real type for
    # type checkers without binding the name at runtime.
    from application_sdk.execution._temporal.app_workflow import (
        generate_workflow_class as generate_workflow_class,
    )

__getattr__ = _deprecation.deprecated_module_getattr(
    __name__,
    _DEPRECATED_CONSTANTS,
    remove_in="4.0.0",
    resolve=_deprecation.lazy_resolver(
        {
            "generate_workflow_class": (
                "application_sdk.execution._temporal.app_workflow",
                "generate_workflow_class",
            )
        }
    ),
)
