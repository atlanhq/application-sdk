import asyncio
import concurrent.futures
import gzip
import logging
import os
import posixpath
import threading
import warnings
from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from datetime import datetime
from time import monotonic, time
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

if TYPE_CHECKING:
    from application_sdk.storage.ops import BoundStore

import orjson

from application_sdk._context_errors import ObjectStoreNotConfiguredError
from application_sdk._install import worker_only_missing
from application_sdk.constants import (
    APPLICATION_NAME,
    DEPLOYMENT_NAME,
    DEPLOYMENT_OBJECT_STORE_NAME,
    ENABLE_ATLAN_UPLOAD,
    ENABLE_OBSERVABILITY_STORE_SINK,
    LOG_FILE_NAME,
    METRICS_FILE_NAME,
    SIZING_FILE_NAME,
    TRACES_FILE_NAME,
    UPSTREAM_OBJECT_STORE_NAME,
)
from application_sdk.observability.background_loop import (
    BackgroundTask,
    observability_loop,
    register_after_fork,
)
from application_sdk.observability.utils import in_temporal_workflow


def _has_running_loop() -> bool:
    """Whether the calling thread is running an asyncio event loop."""
    return _running_loop_or_none() is not None


def _running_loop_or_none() -> asyncio.AbstractEventLoop | None:
    """The event loop the calling thread is running, or ``None``."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


# --- Path configuration ---
# Structure: observability/<mode>/<signal>/year=.../hour=.../file.json.gz
# SDR (ENABLE_ATLAN_UPLOAD=true):     sdr/logs/, sdr/metrics/, sdr/traces/
# Non-SDR (ENABLE_ATLAN_UPLOAD=false): non-sdr/logs/, non-sdr/metrics/, non-sdr/traces/
_OBS_MODE = "sdr" if ENABLE_ATLAN_UPLOAD else "non-sdr"

# Map of signal type → local subdirectory (e.g., sdr/logs)
LOCAL_OBS_SUBDIR_MAP = {
    "logs": f"{_OBS_MODE}/logs",
    "metrics": f"{_OBS_MODE}/metrics",
    "traces": f"{_OBS_MODE}/traces",
    # Must be added alongside the S3 prefix below: a signal in only one map writes
    # to ``other/`` locally while uploading to ``sizing/``.
    "sizing": f"{_OBS_MODE}/sizing",
    # OTel-sourced metrics from ``ObjectStoreMetricExporter``. Kept apart from
    # ``metrics`` (the ``record_metric()`` stream) so consumers can tell the
    # two streams apart.
    "prometheus-metrics": f"{_OBS_MODE}/prometheus-metrics",
}

# Map of signal type → S3 remote key prefix
OBSERVABILITY_S3_PREFIX_MAP = {
    "logs": f"artifacts/apps/observability/{_OBS_MODE}/logs",
    "metrics": f"artifacts/apps/observability/{_OBS_MODE}/metrics",
    "traces": f"artifacts/apps/observability/{_OBS_MODE}/traces",
    # Its own prefix, not the "other" fallback: this dataset has to be selectable
    # across tenants without filtering out everything else in "other".
    "sizing": f"artifacts/apps/observability/{_OBS_MODE}/sizing",
    "prometheus-metrics": f"artifacts/apps/observability/{_OBS_MODE}/prometheus-metrics",
}


T = TypeVar("T")

# Upper bound on batch flushes ``add_record`` may have in flight per instance.
# Each one holds a full batch in memory and an object-store upload; during a
# store outage uploads stall, and without a cap every ``batch_size`` records
# would start another. At the cap, records stay in the buffer and the next
# flush (a freed slot, the periodic flush, or ``flush_all``) takes them.
_MAX_INFLIGHT_FLUSHES = 4

# Test-only: total time ``AtlanObservability._reset_for_testing`` waits for the
# flush work of the sinks it drops to stop. The work runs on another thread.
_RESET_STOP_TIMEOUT_SECONDS = 30.0


def _cancel_flush_task_for_testing(task: "asyncio.Task[None]", deadline: float) -> None:
    """Cancel a dropped sink's batch flush, waiting for it until *deadline*.

    Waits only when the task's loop is running on another thread. On the
    caller's own loop the task can only end once the caller yields, and on a
    loop that is not running, once that loop next runs; either way it is
    cancelled before it can finish its upload, so it is not waited for.
    """
    if task.done():
        return
    loop = task.get_loop()
    if loop.is_closed():
        return  # Never runs again.
    if loop is _running_loop_or_none() or not loop.is_running():
        task.cancel()
        return

    async def _cancel_and_wait() -> None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        stopped = asyncio.run_coroutine_threadsafe(_cancel_and_wait(), loop)
    except RuntimeError:
        return  # The loop closed meanwhile.
    try:
        stopped.result(timeout=max(0.0, deadline - monotonic()))
    except concurrent.futures.TimeoutError:
        stopped.cancel()
        logging.warning(
            "A dropped observability sink's batch flush did not stop within %ss",
            _RESET_STOP_TIMEOUT_SECONDS,
        )


@dataclass
class _PartitionWriter:
    """State for the single open gzip partition file inside ``_flush_records``.

    Invariant: an instance is live (file open, local file on disk) from the moment
    it is created until ``_upload_and_delete`` completes or the outer ``finally``
    block of ``_flush_records`` cleans it up.  The local ``current_writer`` variable
    being ``None`` is the signal that no partition file is currently open.
    """

    partition_path: str
    local_path: str
    remote_key: str
    file: gzip.GzipFile


def _warn_retention_params_deprecated() -> None:
    """Warn that ``retention_days`` / ``cleanup_enabled`` are accepted but ignored.

    A module-level helper rather than inline in ``AtlanObservability.__init__``:
    the deprecation scanner attributes a warn emitted directly by ``__init__`` to
    the whole class, and only these two parameters are deprecated.
    ``stacklevel=3`` points at the code constructing the sink.
    """
    warnings.warn(
        "AtlanObservability retention_days and cleanup_enabled are "
        "deprecated and ignored; use an object-store bucket lifecycle "
        "rule instead — the SDK retention sweep never deleted exported "
        "files. Will be removed in v4.0.0.",
        DeprecationWarning,
        stacklevel=3,
    )


# --- Shared object-store writer ---
# Every observability signal that reaches the object store goes through the
# helpers below: the ``AtlanObservability`` sinks (logs, metrics, traces,
# sizing) and ``ObjectStoreMetricExporter`` (OTel ``prometheus-metrics``). One
# path layout, one store resolution (``DAPR_COMPONENTS_PATH``, the binding's put
# attributes), one upload seam (``upload_file``: retries, gateway no-route
# handling, transfer telemetry), so a fix to one writer reaches all of them.

# Store names whose binding was not resolvable yet (a transient boot state):
# warned about once, then retried quietly on each upload until they resolve.
_deferred_stores: set[str] = set()
# Store names whose resolution failed for a reason a retry will not fix: warned
# about once, then never retried for the life of the process.
_disabled_stores: set[str] = set()
_store_state_lock = threading.Lock()


def _partition_segments(timestamp: datetime) -> tuple[str, str, str, str]:
    """The ``year=/month=/day=/hour=`` path segments for *timestamp*."""
    return (
        f"year={timestamp.year}",
        f"month={timestamp.month:02d}",
        f"day={timestamp.day:02d}",
        f"hour={timestamp.hour:02d}",
    )


def _local_partition_dir(data_dir: str, signal: str, timestamp: datetime) -> str:
    """Local hour-partition directory for *signal* under *data_dir*."""
    local_subdir = LOCAL_OBS_SUBDIR_MAP.get(signal, f"{_OBS_MODE}/other")
    return os.path.join(data_dir, local_subdir, *_partition_segments(timestamp))


def _remote_partition_key(signal: str, timestamp: datetime, filename: str) -> str:
    """Object-store key for *filename* in *signal*'s hour partition."""
    prefix = OBSERVABILITY_S3_PREFIX_MAP.get(
        signal, f"artifacts/apps/observability/{_OBS_MODE}/other"
    )
    return posixpath.join(prefix, *_partition_segments(timestamp), filename)


def _partition_filename(seq: int) -> str:
    """Lexically sortable, unique partition filename.

    ``datetime.now()`` rather than ``time.time_ns()``: the latter is restricted
    inside Temporal's workflow sandbox, while ``datetime.now()`` is patched to be
    safe there. *seq* is the writer's monotonic tie-breaker for when the clock
    has too coarse a resolution (~15 ms on Windows) to tell two files apart.
    """
    ts_ns = int(datetime.now().timestamp() * 1e9)
    return f"{ts_ns}_{seq:06d}_{DEPLOYMENT_NAME}_{APPLICATION_NAME}.json.gz"


def _store_or_none(
    name: str, get_store: "Callable[[], BoundStore]"
) -> "BoundStore | None":
    """Resolve the store *get_store* builds for binding *name*, or ``None``.

    Uploads are best-effort, so a store that cannot be built yet is skipped
    rather than raised. Two cases:

    * A transient boot state: the Dapr component is not written yet, or its
      placeholders / ``secretKeyRef`` env vars are not substituted yet
      (``StorageBindingNotFoundError`` / ``StorageBindingBrokenError``). The
      metrics-flush thread can upload during app-server boot, before either is
      done, so this is retried on every upload and heals on its own. Warned
      about once with no stacktrace (it is expected, and a persistent gap must
      stay visible where DEBUG is filtered out), then quiet at DEBUG; the heal
      is logged at INFO, so an unmatched warning means the gap never closed.
    * Anything else (an unsupported binding type, an api-only install without
      the storage extras): a retry will not fix it, so it is logged once with
      its stacktrace and the store is not tried again.

    The successful store is cached by *get_store* itself.
    """
    with _store_state_lock:
        if name in _disabled_stores:
            return None
    try:
        store = get_store()
    except Exception as exc:
        if _is_transient_binding_error(exc):
            with _store_state_lock:
                first = name not in _deferred_stores
                _deferred_stores.add(name)
            if first:
                # conformance: ignore[L004] transient boot-race deferral, not a failure — logged once without a stacktrace by design; a traceback for "not ready yet" reads as a failure.
                logging.warning(
                    "Object store '%s' binding not resolvable yet; observability "
                    "upload deferred, retrying each flush until it appears",
                    name,
                )
            else:
                logging.debug(
                    "Object store '%s' binding still not resolvable; observability "
                    "upload still deferred",
                    name,
                )
            return None
        with _store_state_lock:
            _disabled_stores.add(name)
        logging.warning(
            "Object store '%s' could not be set up; observability upload to it disabled",
            name,
            exc_info=True,
        )
        return None
    with _store_state_lock:
        healed = name in _deferred_stores
        _deferred_stores.discard(name)
    if healed:
        logging.info(
            "Object store '%s' binding now resolved; observability upload active",
            name,
        )
    return store


def _is_transient_binding_error(exc: Exception) -> bool:
    """Whether *exc* is a binding that is missing or unresolved *for now*."""
    try:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — deferred to break the observability→storage circular import
            StorageBindingBrokenError,
            StorageBindingNotFoundError,
        )
    except ModuleNotFoundError:
        # api-only install: no storage layer, so nothing here can heal.
        return False
    return isinstance(exc, (StorageBindingNotFoundError, StorageBindingBrokenError))


async def _upload_to_observability_stores(
    local_path: str,
    remote_key: str,
    *,
    get_deployment_store: "Callable[[], BoundStore]",
    get_upstream_store: "Callable[[], BoundStore]",
) -> None:
    """Upload *local_path* to *remote_key* in both stores, then delete it.

    The deployment-store upload is non-fatal (logged at WARNING). The
    upstream-store upload (only when ``ENABLE_ATLAN_UPLOAD``) propagates its
    failure to the caller. A store whose binding cannot be resolved is skipped
    (see :func:`_store_or_none`). The local file is always deleted.
    """
    try:
        try:
            from application_sdk.storage import upload_file  # noqa: PLC0415
        except ModuleNotFoundError as exc:
            if not worker_only_missing(exc):
                raise
            # api-only install (the API host): no object store to export to.
            raise ObjectStoreNotConfiguredError() from exc

        # write_sidecar=False: telemetry exports are not artifacts anyone
        # downloads through the SDK, and the ingestion pipelines that read these
        # prefixes enumerate them directly — a `.sha256` next to every
        # `.json.gz` would double the object count there for no verification
        # anyone performs. The upload-side size validation still applies.
        deployment_store = _store_or_none(
            DEPLOYMENT_OBJECT_STORE_NAME, get_deployment_store
        )
        if deployment_store is not None:
            try:
                await upload_file(
                    remote_key,
                    local_path,
                    store=deployment_store,
                    write_sidecar=False,
                )
            except Exception:
                logging.warning(
                    "Deployment objectstore upload failed (non-fatal)",
                    exc_info=True,
                )

        if ENABLE_ATLAN_UPLOAD:
            upstream_store = _store_or_none(
                UPSTREAM_OBJECT_STORE_NAME, get_upstream_store
            )
            if upstream_store is not None:
                await upload_file(
                    remote_key,
                    local_path,
                    store=upstream_store,
                    write_sidecar=False,
                )

        logging.debug("Exported records → %s", remote_key)
    finally:
        if os.path.exists(local_path):
            os.unlink(local_path)


def _write_and_upload_batch(
    records: Sequence[dict[str, Any]],
    *,
    signal: str,
    data_dir: str,
    seq: int,
    timeout: float,
) -> None:
    """Write *records* as one NDJSON.gz file under *signal* and upload it.

    For a synchronous caller on its own thread (``ObjectStoreMetricExporter``
    runs on OTel's ``PeriodicExportingMetricReader`` thread). The file is
    written on the calling thread; the upload runs on the shared observability
    loop through :func:`_upload_to_observability_stores`, the same path the
    ``AtlanObservability`` sinks use, and this waits up to *timeout* seconds
    for it. On timeout the upload carries on in the background and still
    deletes the file; the ``TimeoutError`` is raised to the caller.

    Once the observability loop has shut down (interpreter exit) the upload
    runs on the calling thread instead, so a final export is not lost.
    """
    now = datetime.now()
    filename = _partition_filename(seq)
    local_dir = _local_partition_dir(data_dir, signal, now)
    local_path = os.path.join(local_dir, filename)
    remote_key = _remote_partition_key(signal, now, filename)

    os.makedirs(local_dir, exist_ok=True)
    try:
        with gzip.open(local_path, "wb") as fh:
            for record in records:
                fh.write(orjson.dumps(record) + b"\n")
    except BaseException:
        if os.path.exists(local_path):
            os.unlink(local_path)
        raise

    def _upload() -> "Coroutine[Any, Any, None]":
        return _upload_to_observability_stores(
            local_path,
            remote_key,
            get_deployment_store=AtlanObservability._get_deployment_store,
            get_upstream_store=AtlanObservability._get_upstream_store,
        )

    future = observability_loop.submit(_upload())
    if future is None:
        if _has_running_loop():
            # Cannot run a coroutine to completion from inside a running loop
            # without blocking it; drop this batch rather than leak its file.
            os.unlink(local_path)
            raise RuntimeError(
                "observability loop is shut down and the calling thread is "
                "running an event loop; batch dropped"
            )
        asyncio.run(_upload())
        return
    if observability_loop.in_loop_thread():
        # Waiting here would block the loop the upload needs; it deletes the
        # file itself when it finishes.
        return
    future.result(timeout=timeout)


class AtlanObservability(Generic[T], ABC):
    """Base class for Atlan observability functionality.

    This abstract base class provides core functionality for observability features
    including buffering and flushing of observability data.

    Retention of exported files is not handled here: bound object-store growth
    with a lifecycle rule on the bucket.

    Features:
    - Buffered record storage
    - Periodic flushing to storage
    - Error handling and signal management
    - Gzip-compressed NDJSON file storage
    - Dapr object store integration

    Attributes:
        _instances: List of all active instances
    """

    _instances: ClassVar[list[Any]] = []
    # Resolved once for the whole hierarchy. Both getters below read and write
    # these through ``AtlanObservability`` explicitly rather than through
    # ``cls``: a ``cls.<attr> = ...`` from a classmethod called on a *subclass*
    # binds a subclass attribute that shadows the base one, so the cache would
    # be per-subclass and anything swapping the base (test fixtures,
    # ``_reset_for_testing``) would be silently ignored. Nothing here varies by
    # subclass — the binding names are module constants and ``_components_dir``
    # is defined only here — so one shared entry is also one resolution instead
    # of one per adapter. Same reason ``_instances`` is appended via the base.
    _deployment_store: ClassVar["BoundStore | None"] = None
    _upstream_store: ClassVar["BoundStore | None"] = None

    @classmethod
    def _components_dir(cls) -> str:
        # Honour ``DAPR_COMPONENTS_PATH`` so the embedded-Dapr local-dev path
        # finds the auto-generated component YAMLs in the temp dir.
        return os.environ.get("DAPR_COMPONENTS_PATH", "./components")

    @classmethod
    def _get_deployment_store(cls) -> "BoundStore":
        if AtlanObservability._deployment_store is None:
            try:
                from application_sdk.storage.binding import (  # noqa: PLC0415
                    create_store_from_binding_with_put_attrs,
                )
                from application_sdk.storage.ops import BoundStore  # noqa: PLC0415
            except ModuleNotFoundError as exc:
                if not worker_only_missing(exc):
                    raise
                # api-only install (the API host): no object store to export to.
                raise ObjectStoreNotConfiguredError() from exc

            store, put_attrs = create_store_from_binding_with_put_attrs(
                DEPLOYMENT_OBJECT_STORE_NAME,
                components_dir=cls._components_dir(),
            )
            AtlanObservability._deployment_store = BoundStore(store, put_attrs)
        return AtlanObservability._deployment_store

    @classmethod
    def _get_upstream_store(cls) -> "BoundStore":
        if AtlanObservability._upstream_store is None:
            try:
                from application_sdk.storage.binding import (  # noqa: PLC0415 — deferred to break the observability→storage circular import
                    create_store_from_binding_with_put_attrs,
                )
                from application_sdk.storage.ops import BoundStore  # noqa: PLC0415
            except ModuleNotFoundError as exc:
                if not worker_only_missing(exc):
                    raise
                # api-only install (the API host): no object store to export to.
                raise ObjectStoreNotConfiguredError() from exc

            store, put_attrs = create_store_from_binding_with_put_attrs(
                UPSTREAM_OBJECT_STORE_NAME,
                components_dir=cls._components_dir(),
            )
            AtlanObservability._upstream_store = BoundStore(store, put_attrs)
        return AtlanObservability._upstream_store

    def __init__(
        self,
        batch_size: int,
        flush_interval: int,
        data_dir: str,
        file_name: str,
        retention_days: int | None = None,
        cleanup_enabled: bool | None = None,
    ):
        """Initialize the observability base class.

        Args:
            batch_size: Records buffered before a flush.
            flush_interval: Seconds between periodic flushes.
            data_dir: Local directory partition files are written to.
            file_name: Signal discriminator (see ``_get_signal_type``).
            retention_days: Deprecated and ignored. The retention sweep never
                deleted anything from the object store; use a bucket
                lifecycle rule instead. Will be removed in v4.0.0.
            cleanup_enabled: Deprecated and ignored, as ``retention_days``.
                Will be removed in v4.0.0.
        """
        if retention_days is not None or cleanup_enabled is not None:
            _warn_retention_params_deprecated()
        # Initialize instance variables
        self._buffer: list[dict[str, object]] = []
        self._buffer_lock = threading.Lock()
        self._last_flush_time = time()
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self.data_dir = data_dir
        self.file_name = file_name
        self._writer_seq = 0
        # Batch flushes started by ``add_record``. Kept so they are not
        # garbage-collected mid-write, so ``flush_all`` can await them before the
        # loop ends (``asyncio.run`` cancels stragglers, and ``_flush_records``'s
        # ``finally`` then deletes the half-written partition), and so their
        # number can be capped. Guarded by ``_buffer_lock``: ``add_record`` runs
        # on any thread that has a running loop.
        self._flush_tasks: set[asyncio.Task[None]] = set()
        # This sink's periodic flush on the shared observability loop, if it
        # started one; a forked child restarts it on its own loop.
        self._periodic_flush_task: BackgroundTask | None = None

        # Ensure data directory exists
        os.makedirs(data_dir, exist_ok=True)

        # Register this instance
        AtlanObservability._instances.append(self)

    @classmethod
    async def flush_all(cls) -> None:
        """Flush all instances of AtlanObservability.

        For each registered instance, first waits for the batch flushes
        ``add_record`` started on this event loop, then flushes what is left in
        the buffer. Waiting first matters at shutdown: a flush still in flight
        when ``asyncio.run`` returns is cancelled, and its partial file deleted.
        Errors are logged per instance and never raised.
        """
        for instance in list(cls._instances):
            try:
                await instance._await_flush_tasks()
                await instance._flush_buffer(force=True)
                # Records logged while the drain above ran may have started
                # new batch flushes; wait for those too.
                await instance._await_flush_tasks()
            except Exception:
                logging.error("Error flushing instance", exc_info=True)

    async def _await_flush_tasks(self) -> None:
        """Wait for in-flight ``add_record`` flushes owned by the running loop.

        Re-checks after each wait until none are left: while it waits, an
        ``add_record`` can take a freed slot and start another flush, whose
        batch is no longer in the buffer for the caller's drain to find.

        Tasks bound to another loop cannot be awaited from here; that loop's
        own shutdown path flushes them.

        Only tasks that are not done yet are waited on. A finished task stays in
        ``_flush_tasks`` until its ``_discard_flush_task`` callback runs, and
        that callback only runs once the loop turns. ``gather`` over finished
        tasks completes without yielding to the loop (Python 3.12+), so waiting
        on one would re-check forever, never letting the callback run, with the
        loop stalled and a core pinned (ARUN-1391). A task that is not done
        cannot finish without the loop turning, so every pass of this loop
        yields to it. A finished task is removed here instead, and its failure,
        if any, reported the same way as a waited-on one's.
        """
        loop = asyncio.get_running_loop()
        while True:
            with self._buffer_lock:
                owned = [t for t in self._flush_tasks if t.get_loop() is loop]
                finished = [t for t in owned if t.done()]
                pending = [t for t in owned if not t.done()]
                # Removed now so the next pass, or the next call, does not
                # report it again; the queued callback then finds it gone.
                self._flush_tasks.difference_update(finished)
            failures = [
                t.exception() for t in finished if not t.cancelled() and t.exception()
            ]
            if pending:
                # ``_flush_records`` handles its own errors; gather only so one
                # unexpected failure cannot hide the others.
                failures.extend(await asyncio.gather(*pending, return_exceptions=True))
            for failure in failures:
                if isinstance(failure, Exception):
                    logging.warning(
                        "Observability batch flush failed",
                        exc_info=(type(failure), failure, failure.__traceback__),
                    )
            if not pending:
                return

    def _discard_flush_task(self, task: "asyncio.Task[None]") -> None:
        with self._buffer_lock:
            self._flush_tasks.discard(task)

    @classmethod
    def _reset_for_testing(cls) -> None:
        """Reset global state for test isolation.

        This method should only be used in tests to allow fresh initialization
        for each test case.

        Every instance it drops is stopped too (see
        :meth:`_stop_flushes_for_testing`), waiting up to
        ``_RESET_STOP_TIMEOUT_SECONDS`` in all. Dropping only the reference
        left a sink's periodic flush running on the shared observability loop,
        where it delivered its batches into whatever ``upload_file`` a later
        test on the same worker had patched.
        """
        dropped = list(AtlanObservability._instances)
        AtlanObservability._instances.clear()
        deadline = monotonic() + _RESET_STOP_TIMEOUT_SECONDS
        for instance in dropped:
            instance._stop_flushes_for_testing(deadline)
        AtlanObservability._deployment_store = None
        AtlanObservability._upstream_store = None
        with _store_state_lock:
            _deferred_stores.clear()
            _disabled_stores.clear()

    def _stop_flushes_for_testing(self, deadline: float) -> None:
        """Discard this sink's records and stop its flushes, until *deadline*.

        Test-only. The buffer is discarded first, so the final flush that
        cancelling the periodic flush triggers has nothing to write. Then the
        periodic flush is cancelled and waited for, and every batch flush
        ``add_record`` started is cancelled (see
        :func:`_cancel_flush_task_for_testing`). The class-level handle that
        logs, metrics, traces and sizing keep is cleared with it, so the next
        sink of that class starts a fresh flush.
        """
        with self._buffer_lock:
            self._buffer = []
            flush_tasks = list(self._flush_tasks)
        handle = self._periodic_flush_task
        self._periodic_flush_task = None
        if handle is not None:
            handle.cancel()
            # ``wait`` must not run on the handle's own loop thread, and a
            # loop that is not running would never finish the task.
            can_wait = (
                handle.loop.is_running() and handle.loop is not _running_loop_or_none()
            )
            if can_wait and not handle.wait(max(0.0, deadline - monotonic())):
                logging.warning(
                    "A dropped observability sink's periodic flush did not stop "
                    "within %ss",
                    _RESET_STOP_TIMEOUT_SECONDS,
                )
            for owner in type(self).__mro__:
                if "_flush_task" not in vars(owner):
                    continue
                if owner._flush_task is handle:  # type: ignore[attr-defined]  # — declared on this class, checked above
                    owner._flush_task = None  # type: ignore[attr-defined]
                    owner._flush_task_started = False  # type: ignore[attr-defined]
                break
        for task in flush_tasks:
            _cancel_flush_task_for_testing(task, deadline)

    def _get_signal_type(self) -> str:
        """Map file_name to signal type (logs, metrics, traces, sizing).

        Returns:
            str: The signal type based on self.file_name
        """
        if self.file_name == LOG_FILE_NAME:
            return "logs"
        elif self.file_name == METRICS_FILE_NAME:
            return "metrics"
        elif self.file_name == TRACES_FILE_NAME:
            return "traces"
        elif self.file_name == SIZING_FILE_NAME:
            return "sizing"
        return "other"

    def _store_sink_enabled(self) -> bool:
        """Whether this signal writes to the object store. Overridable because the
        shared switch covers three signals, so disabling it can silently drop one.
        """
        return ENABLE_OBSERVABILITY_STORE_SINK

    def _get_partition_path(self, timestamp: datetime) -> str:
        """Generate local partition path based on timestamp.

        Uses signal-type-specific subdirectory (e.g., sdr-logs/, non-sdr-metrics/)
        with hour-level partitioning. The full S3 prefix is applied separately
        when computing the remote key for upload.

        Args:
            timestamp: The timestamp to generate partition path for

        Returns:
            str: The local partition path
        """
        return _local_partition_dir(self.data_dir, self._get_signal_type(), timestamp)

    def _build_remote_key(self, partition_time: datetime, filename: str) -> str:
        """Build the S3 remote key for a partition file.

        Args:
            partition_time: Timestamp used to derive the year/month/day/hour prefix.
            filename: The local filename (e.g. ``<ts>_<seq>_<deployment>_<app>.json.gz``).

        Returns:
            str: The fully qualified S3 key, e.g.
                ``artifacts/apps/observability/sdr/logs/year=2025/month=06/day=29/hour=14/<filename>``.
        """
        return _remote_partition_key(self._get_signal_type(), partition_time, filename)

    async def _upload_and_delete(self, local_path: str, remote_key: str) -> None:
        """Upload *local_path* to object store(s) then remove the local file.

        Delegates to :func:`_upload_to_observability_stores`, the writer
        ``ObjectStoreMetricExporter`` shares. The deployment-store upload is
        non-fatal (failure is logged at WARNING and the method continues). The
        upstream-store upload (when ``ENABLE_ATLAN_UPLOAD`` is true) is allowed
        to propagate — the caller handles it. A store whose binding cannot be
        resolved is skipped. The local file is always deleted.

        Args:
            local_path: Absolute path to the local ``.json.gz`` file.
            remote_key: S3 key (from :meth:`_build_remote_key`) to upload to.
        """
        await _upload_to_observability_stores(
            local_path,
            remote_key,
            get_deployment_store=self._get_deployment_store,
            get_upstream_store=self._get_upstream_store,
        )

    @abstractmethod
    def process_record(self, record: T) -> dict[str, Any]:
        """Process a record into a dictionary format.

        Args:
            record: The record to process

        Returns:
            Dict[str, Any]: Dictionary representation of the record

        This is an abstract method that must be implemented by subclasses.
        """

    @abstractmethod
    def export_record(self, record: T) -> None:
        """Export a record to external systems.

        Args:
            record: The record to export

        This is an abstract method that must be implemented by subclasses.
        """

    def _start_periodic_flush(self) -> BackgroundTask | None:
        """Start :meth:`_periodic_flush` on the shared observability loop.

        Every sink starts its periodic flush here, on the one background loop
        (see ``background_loop``), whatever thread or loop the sink is built
        on. That loop's shutdown at exit cancels the flush, whose
        ``CancelledError`` handler makes the final flush, within one bound.

        Returns the task's handle, or ``None`` when the loop has already shut
        down.
        """
        handle = observability_loop.start(
            self._periodic_flush(),
            name=f"observability-{self._get_signal_type()}-flush",
        )
        self._periodic_flush_task = handle
        return handle

    @classmethod
    def _restart_periodic_flushes_after_fork(cls) -> None:
        """In a forked child, restart each sink's periodic flush on the child's loop.

        The child inherits the sinks and their "flush started" guards, but not
        the thread that ran the flushes, so without this nothing in the child
        would flush. The buffer lock is replaced first: a parent thread may
        have held it at the moment of the fork.
        """
        for instance in list(cls._instances):
            instance._buffer_lock = threading.Lock()
            instance._flush_tasks = set()
            old = instance._periodic_flush_task
            if old is None:
                continue
            new = instance._start_periodic_flush()
            # Logs, metrics, traces and sizing keep a class-level handle to
            # stop the flush with; point it at the child's task.
            owner = type(instance)
            if getattr(owner, "_flush_task", None) is old:
                owner._flush_task = new  # type: ignore[attr-defined]  # — set only on classes that declare it

    async def _periodic_flush(self):
        """Periodically flush the buffer to storage.

        This coroutine:
        - Performs initial flush
        - Runs periodic flushes at configured intervals
        - Survives a failed flush: the error is logged and the next interval
          flushes again (one bad flush used to end periodic flushing for good)
        - Ensures final flush on cancellation
        """
        try:
            first = True
            while True:
                if not first:
                    # conformance: ignore[P060] fixed-cadence flush interval, not a retry ladder
                    await asyncio.sleep(self._flush_interval)
                first = False
                try:
                    await self._flush_buffer(force=True)
                except Exception:
                    logging.warning("Error in periodic flush", exc_info=True)
        except asyncio.CancelledError:
            # Handle task cancellation gracefully
            await self._flush_buffer(force=True)

    async def _flush_buffer(self, force=False):
        """Flush the buffer to storage.

        Args:
            force (bool): Whether to force flush regardless of buffer size

        This method:
        - Safely copies and clears the buffer
        - Flushes records if buffer is not empty
        """
        with self._buffer_lock:
            if self._buffer:
                # Swap rather than copy: O(1) instead of O(n) — the old list reference
                # is handed off to the flusher while a fresh list starts accumulating.
                buffer_copy = self._buffer
                self._buffer = []
            else:
                buffer_copy = None
        if buffer_copy:
            await self._flush_records(buffer_copy)

    async def _flush_records(self, records: list[dict[str, Any]]):
        """Flush records to json.gz files and upload to object stores.

        Args:
            records: List of records to flush

        Uses a **single-pass streaming writer** to reduce peak memory.  Rather than
        grouping all records into a ``partition_records`` dict (which holds two copies
        of the full batch at peak), the method opens one gzip file per active partition
        and writes each record as an NDJSON line as it is consumed.  Only one record
        and one gzip write buffer are alive at any moment — O(1) extra memory
        regardless of batch size.

        Records are assumed to arrive in emission order (chronologically), so
        hour-partitions appear as contiguous runs.  If a rare cross-thread reorder
        causes a stale partition to reappear mid-flush, a second uniquely-named
        ``.json.gz`` file is written for that hour.  Uniqueness is jointly enforced by
        the ``ts_ns`` wall-clock prefix and a per-instance monotonic ``_writer_seq``
        counter, so two writers opened within the same clock tick (e.g. ~15 ms on
        Windows) still produce distinct filenames.

        - Writes gzip-compressed NDJSON (no pandas/pyarrow dependency)
        - SDR path: ``artifacts/apps/observability/sdr/logs/``    (``ENABLE_ATLAN_UPLOAD=true``)
        - Non-SDR path: ``artifacts/apps/observability/non-sdr/logs/``
        - Uploads to customer bucket (DEPLOYMENT_OBJECT_STORE) always
        - Uploads to Atlan bucket (UPSTREAM_OBJECT_STORE) when ``ENABLE_ATLAN_UPLOAD=true``
        """
        if not self._store_sink_enabled() or not records:
            return

        # Blocking file I/O and the object-store upload below are both illegal
        # on Temporal's deterministic workflow loop — so don't write here. Every caller has already taken
        # *records* out of the buffer, so put them back at the front rather
        # than dropping them: the batch holds whatever any workflow or activity
        # on this worker logged since the last flush, and the periodic flush on
        # the worker loop writes it in order. Shares one predicate with
        # SegmentClient.flush() so both guards answer "am I in a workflow" the
        # same way; the previous ``workflow.unsafe.in_sandbox()`` check missed
        # passed-through modules and unsandboxed workers. See
        # utils.in_temporal_workflow.
        if in_temporal_workflow():
            with self._buffer_lock:
                self._buffer[:0] = records
            return

        # ``current_writer`` is non-None iff a gzip partition file is currently open.
        # The _PartitionWriter dataclass bundles all four related state fields so they
        # are always updated atomically — preventing the bug where an upload failure
        # left current_partition_path pointing at the closed partition while
        # current_file was None, causing the next same-partition record to call
        # write() on None.
        current_writer: _PartitionWriter | None = None

        try:
            for record in records:
                try:
                    record_time = datetime.fromtimestamp(record["timestamp"])
                    partition_path = self._get_partition_path(record_time)

                    if (
                        current_writer is None
                        or partition_path != current_writer.partition_path
                    ):
                        # Finalize the current partition before switching.
                        if current_writer is not None:
                            # Swap out before close+upload so the triggering record is
                            # never dropped: current_writer=None first means the code
                            # below always opens a fresh writer for the new partition,
                            # even if the upload raises.  _upload_and_delete's own
                            # finally unlinks the local file, so no disk leak on failure.
                            prev_writer = current_writer
                            current_writer = None
                            try:
                                prev_writer.file.close()
                                await self._upload_and_delete(
                                    prev_writer.local_path,
                                    prev_writer.remote_key,
                                )
                            except Exception:
                                logging.error(
                                    "Error finalizing observability partition",
                                    exc_info=True,
                                )

                        # Open a fresh gzip file for the new partition.
                        os.makedirs(partition_path, exist_ok=True)
                        self._writer_seq += 1
                        filename = _partition_filename(self._writer_seq)
                        local_path = os.path.join(partition_path, filename)
                        current_writer = _PartitionWriter(
                            partition_path=partition_path,
                            local_path=local_path,
                            remote_key=self._build_remote_key(record_time, filename),
                            file=gzip.open(local_path, "wb"),
                        )

                    current_writer.file.write(orjson.dumps(record) + b"\n")

                except Exception:
                    logging.error("Error writing observability record", exc_info=True)

            # Finalize the last open partition.
            if current_writer is not None:
                try:
                    current_writer.file.close()
                    await self._upload_and_delete(
                        current_writer.local_path,
                        current_writer.remote_key,
                    )
                finally:
                    current_writer = None

        except Exception:
            logging.error("Error flushing records batch", exc_info=True)

        finally:
            # Defensive: if an unexpected exception left the writer open, close its
            # file handle and remove any orphaned local file to prevent leaks.
            if current_writer is not None:
                try:
                    current_writer.file.close()
                except Exception:
                    logging.warning("Error closing orphaned gzip file", exc_info=True)
                if os.path.exists(current_writer.local_path):
                    os.unlink(current_writer.local_path)

    def add_record(self, record: T):
        """Add a record to the buffer and process it.

        Args:
            record: The record to add

        This method:
        - Processes the record
        - Adds it to the buffer
        - Triggers flush if needed
        - Exports the record
        """
        try:
            # Process the record
            processed_record = self.process_record(record)

            # Add to buffer. Only append, and leave the batch to the periodic
            # flush, when a flush cannot be started from here:
            # - inside a workflow, it would run on Temporal's workflow loop,
            #   where ``_flush_records`` cannot write;
            # - on a thread with no running event loop (sync activities run in
            #   the worker's thread pool), ``asyncio.create_task`` raises, and
            #   the batch already swapped out of the buffer would be lost;
            # - when ``_MAX_INFLIGHT_FLUSHES`` are already running (a stalled
            #   object store), so tasks and their batches do not pile up.
            in_workflow = in_temporal_workflow()
            can_flush_here = not in_workflow and _has_running_loop()
            with self._buffer_lock:
                self._buffer.append(processed_record)
                now = time()
                if (
                    can_flush_here
                    and len(self._flush_tasks) < _MAX_INFLIGHT_FLUSHES
                    and (
                        len(self._buffer) >= self._batch_size
                        or (now - self._last_flush_time) >= self._flush_interval
                    )
                ):
                    self._last_flush_time = now
                    # Swap rather than copy: O(1) instead of O(n).
                    buffer_copy = self._buffer
                    self._buffer = []
                    # Created under the lock so the cap check and the add are
                    # one step across threads. ``create_task`` only schedules
                    # the coroutine, and the done-callback runs later via the
                    # loop, so neither re-enters this lock here.
                    task = asyncio.create_task(self._flush_records(buffer_copy))
                    self._flush_tasks.add(task)
                    task.add_done_callback(self._discard_flush_task)

            # Export the record
            self.export_record(record)

        except Exception:
            logging.error("Error adding record", exc_info=True)


def _reset_store_state_lock_after_fork() -> None:
    """Give a forked child a fresh ``_store_state_lock``.

    A parent thread may have held it in ``_store_or_none`` at the moment of the
    fork; the child inherits it locked with no thread left to release it, and
    its restarted periodic flushes would block on their next upload.
    """
    global _store_state_lock
    _store_state_lock = threading.Lock()


register_after_fork(_reset_store_state_lock_after_fork)
register_after_fork(
    AtlanObservability._restart_periodic_flushes_after_fork, restarts_work=True
)
