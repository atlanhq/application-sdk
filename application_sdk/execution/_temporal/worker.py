"""Temporal worker configuration and setup.

Workers execute Apps as workflows and tasks as activities.
Each App becomes its own named workflow, and @task methods become
named activities.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import TYPE_CHECKING, Callable

from pydantic import ValidationError
from temporalio.client import Client
from temporalio.worker import Interceptor as TemporalInterceptor
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from application_sdk.app.registry import (
    AppRegistry,
    TaskRegistry,
    get_activity_name,
    resolve_pool_queue,
)
from application_sdk.constants import (
    APP_BUILD_ID,
    APP_DEPLOYMENT_NAME,
    PREFLIGHT_GATE_MODE_ENV,
    SHUTDOWN_DRAIN_DELAY_SECONDS,
)
from application_sdk.execution._temporal.activities import get_all_task_activities
from application_sdk.execution._temporal.workflows import get_all_app_workflows
from application_sdk.execution.sandbox import SandboxConfig
from application_sdk.execution.settings import (
    load_execution_settings,
    load_interceptor_settings,
)
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    # Imported lazily inside _start_metrics_push (cold path) — type-only here.
    from application_sdk.handler.base import Handler
    from application_sdk.observability.pushgateway import PushGatewayClient

logger = get_logger(__name__)

# Fails the workflow RUN rather than the retryable workflow TASK. Matched by
# type, so only never-retryable types belong here.
_WORKFLOW_FAILURE_EXCEPTION_TYPES: tuple[type[BaseException], ...] = (ValidationError,)

# Depth cap for rendering an exception's cause chain. temporalio's poll fatal is
# two links deep; anything much longer is a runaway chain, not diagnostics.
_MAX_FATAL_CHAIN_DEPTH = 10

# sdk-core publishes its poller gauge under this family name (the ``temporal_``
# prefix is core's default). Both spellings are accepted so a future prefix
# change does not silently zero the reading.
_CORE_POLLER_GAUGE_NAMES = ("temporal_num_pollers", "num_pollers")
# Preferred label to key the gauge by; core sets ``poller_type``
# (``workflow_task`` / ``activity_task`` / ``sticky_workflow_task`` / ...).
_CORE_POLLER_TYPE_LABELS = ("poller_type", "worker_type")


def describe_exception_chain(exc: BaseException) -> list[str]:
    """Render an exception and its ``__cause__``/``__context__`` chain.

    A Temporal poll fatal reaches Python wrapped twice — the Rust bridge raises
    ``RuntimeError("Poll failure: Unhandled grpc error when polling: Status {
    code: PermissionDenied, ... }")`` and ``temporalio.worker`` re-wraps that as
    ``RuntimeError("Activity worker failed") from err``. So ``str(exc)`` says
    nothing at all about *why* the poll died: the gRPC status name only exists
    further down the chain. Each link is rendered ``ClassName: message`` so the
    status reaches the logs.

    Cycle-protected and capped at ``_MAX_FATAL_CHAIN_DEPTH`` links.
    """
    chain: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if len(chain) >= _MAX_FATAL_CHAIN_DEPTH:
            chain.append("... chain truncated")
            break
        seen.add(id(current))
        chain.append(f"{type(current).__name__}: {current}")
        # __cause__ is the explicit ``raise ... from``; __context__ covers an
        # implicit chain raised while handling another error.
        current = current.__cause__ or current.__context__
    return chain


async def read_core_poller_counts() -> dict[str, float] | None:
    """Read sdk-core's live poller gauge from its local Prometheus endpoint.

    This is the in-process answer to "is this worker actually polling?".
    sdk-core maintains ``temporal_num_pollers`` as a gauge, labelled by poller
    type, so a worker whose poll loop has died reads ``0`` while a
    legitimately-idle worker still reads its configured poller count. Unlike
    asking the Temporal frontend (``DescribeTaskQueue``), it needs no network
    call, no task-queue read permission, and cannot go blind during the very
    auth outage that kills the poll loop.

    Returns a ``{poller_type: count}`` mapping, or ``None`` when the endpoint
    could not be read — which means *unknown*, never zero. ``None`` is expected
    when core's Prometheus exporter is disabled
    (``ATLAN_TEMPORAL_PROMETHEUS_BIND_ADDRESS``).
    """
    import httpx  # noqa: PLC0415 — cold path: diagnostics only
    from prometheus_client.parser import (  # noqa: PLC0415 — cold path: diagnostics only
        text_string_to_metric_families,
    )

    from application_sdk.constants import (  # noqa: PLC0415 — cold path: diagnostics only
        TEMPORAL_CORE_METRICS_MAX_BYTES,
        TEMPORAL_CORE_METRICS_PROXY_TIMEOUT_SECONDS,
        TEMPORAL_PROMETHEUS_BIND_ADDRESS,
    )

    url = f"http://{TEMPORAL_PROMETHEUS_BIND_ADDRESS}/metrics"
    try:
        async with httpx.AsyncClient(
            timeout=TEMPORAL_CORE_METRICS_PROXY_TIMEOUT_SECONDS
        ) as http_client:
            # Stream so the read is bounded: reject on a declared oversize
            # Content-Length, else accumulate chunks and bail the moment the
            # running total crosses the cap. Passing chunk_size=cap bounds each
            # chunk yielded to this loop; the running-total check bounds the
            # retained bytes. (httpx may still buffer an individual raw
            # transport chunk internally above the cap, so this bounds what the
            # loop retains rather than every transient allocation.) An
            # unbounded response.text would let a high-cardinality or
            # misbehaving local exporter allocate an unbounded string each
            # interval. Oversize is unknown (None), never zero.
            async with http_client.stream("GET", url) as response:
                if response.status_code != 200:
                    logger.debug(
                        "Temporal-core metrics endpoint %s returned HTTP %d; poller count unknown",
                        url,
                        response.status_code,
                    )
                    return None
                content_length = response.headers.get("content-length")
                if content_length is not None and int(content_length) > (
                    TEMPORAL_CORE_METRICS_MAX_BYTES
                ):
                    logger.debug(
                        "Temporal-core metrics endpoint %s declared %s bytes (cap %d); poller count unknown",
                        url,
                        content_length,
                        TEMPORAL_CORE_METRICS_MAX_BYTES,
                    )
                    return None
                chunks: list[bytes] = []
                received = 0
                oversize = False
                async for chunk in response.aiter_bytes(
                    chunk_size=TEMPORAL_CORE_METRICS_MAX_BYTES
                ):
                    received += len(chunk)
                    if received > TEMPORAL_CORE_METRICS_MAX_BYTES:
                        oversize = True
                        break
                    chunks.append(chunk)
                if oversize:
                    logger.debug(
                        "Temporal-core metrics endpoint %s exceeded %d bytes; poller count unknown",
                        url,
                        TEMPORAL_CORE_METRICS_MAX_BYTES,
                    )
                    return None
                payload = b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:
        logger.debug(
            "Temporal-core metrics endpoint %s unreachable; poller count unknown",
            url,
            exc_info=True,
        )
        return None

    counts: dict[str, float] = {}
    try:
        for family in text_string_to_metric_families(payload):
            if family.name not in _CORE_POLLER_GAUGE_NAMES:
                continue
            for sample in family.samples:
                key = next(
                    (
                        sample.labels[label]
                        for label in _CORE_POLLER_TYPE_LABELS
                        if label in sample.labels
                    ),
                    "unlabelled",
                )
                counts[key] = counts.get(key, 0.0) + sample.value
    except Exception:
        logger.debug(
            "Could not parse Temporal-core metrics from %s; poller count unknown",
            url,
            exc_info=True,
        )
        return None

    # The family being absent entirely is not the same as a zero reading: core
    # only registers the gauge once a worker has started polling at least once.
    return counts or None


async def _log_worker_fatal_error(exc: BaseException) -> None:
    """``on_fatal_error`` hook — log the fatal that took the poll loop down.

    ``Worker.run`` invokes this the moment any poll task raises, *before* the
    ``async with`` teardown runs. That matters: ``Worker.__aexit__`` re-raises a
    captured fatal only when the body exits via ``CancelledError``, so a fatal
    that races a shutdown is otherwise dropped silently. This hook always sees
    it.

    Its *absence* is a signal in its own right. A worker with zero pollers and
    no ``poll loop failed`` line never raised at all, which distinguishes a
    swallowed fatal from a poll loop that parked without erroring (ARUN-1127).
    """
    # exc_info=exc, not True: ``Worker.run`` retrieves the fatal with
    # ``task.exception()`` and calls this hook outside any ``except`` block, so
    # no exception is being handled. ``exc_info=True`` would resolve to
    # ``sys.exc_info()`` — empty here — and render "NoneType: None" instead of
    # the traceback this log exists to capture.
    logger.error(
        "Temporal worker poll loop failed fatally; cause chain: %s",
        " <- ".join(describe_exception_chain(exc)),
        exc_info=exc,
    )


def _resolve_gate_enforcement(app_cls: type | None) -> bool:
    """Resolve the preflight gate's posture for one app.

    ``True`` = hard (block on ``NOT_READY``); ``False`` = soft (emit
    ``would_block``, proceed). Precedence: ``ATLAN_PREFLIGHT_GATE_MODE`` env
    (deploy-time ops lever, no app release needed) > the app's declared
    ``App.preflight_gate_mode`` (git-blamed opt-in) > soft default. Only the
    literal ``"hard"`` enforces; an unknown or malformed value falls back to
    soft — a run is never blocked by accident, blocking is always a deliberate
    opt-in.
    """
    val = os.environ.get(PREFLIGHT_GATE_MODE_ENV)
    if val:
        return val.strip().lower() == "hard"
    declared = getattr(app_cls, "preflight_gate_mode", "soft")
    return str(declared).strip().lower() == "hard"


class AppWorker:
    """Wraps Temporal Worker to emit worker_start on startup and to push
    metrics on shutdown for short-lived deployments.

    Emits the ``worker_start`` lifecycle event on ``__aenter__`` (and ``run()``)
    so that every code path that starts a worker automatically registers the
    agent — regardless of whether the caller uses ``async with worker:`` or
    ``await worker.run()``.

    When ``enable_pushgateway=True`` the wrapper also registers a
    ``TemporalCoreCollector`` and starts a ``PushGatewayClient`` that
    periodically pushes ``prometheus_client.REGISTRY`` to
    ``PROMETHEUS_PUSHGATEWAY_URL``. Combined-mode deployments (FastAPI server
    in the same process) leave this off — ``/metrics`` already exposes the
    same series and pushing would double-count.

    ``shutdown_drain_delay_seconds`` is the pre-teardown yield in
    :meth:`__aexit__`, defaulting to :data:`SHUTDOWN_DRAIN_DELAY_SECONDS`. It is
    a constructor parameter rather than a module constant read at the call site
    so a test can set it to zero: three shutdown tests otherwise spent five real
    seconds each asleep proving something about the *drain*, not about the
    delay. The delay's own behaviour is asserted separately, by driving a worker
    built with a non-zero value (FND-962).
    """

    def __init__(
        self,
        worker: Worker,
        *,
        start_event_params: dict,
        enable_pushgateway: bool = False,
        primary_app_name: str = "",
        task_queue: str = "",
        shutdown_drain_delay_seconds: float = SHUTDOWN_DRAIN_DELAY_SECONDS,
    ) -> None:
        self._worker = worker
        self._start_event_params = start_event_params
        self._enable_pushgateway = enable_pushgateway
        self._primary_app_name = primary_app_name
        self._task_queue = task_queue
        self._shutdown_drain_delay_seconds = shutdown_drain_delay_seconds
        self._pusher: PushGatewayClient | None = None

    async def _start_metrics_push(self) -> None:
        if not self._enable_pushgateway:
            return
        from application_sdk.constants import (  # noqa: PLC0415 — cold path: pushgateway env config only when worker mode enabled
            PROMETHEUS_PUSHGATEWAY_DELETE_ON_SHUTDOWN,
            PROMETHEUS_PUSHGATEWAY_HTTP_TIMEOUT_SECONDS,
            PROMETHEUS_PUSHGATEWAY_INTERVAL_SECONDS,
            PROMETHEUS_PUSHGATEWAY_SHUTDOWN_DELETE_DELAY_SECONDS,
            PROMETHEUS_PUSHGATEWAY_SWEEP_STALE_ON_START,
            PROMETHEUS_PUSHGATEWAY_SWEEP_STALENESS_SECONDS,
            PROMETHEUS_PUSHGATEWAY_URL,
            TEMPORAL_PROMETHEUS_BIND_ADDRESS,
        )

        if not PROMETHEUS_PUSHGATEWAY_URL:
            logger.warning(
                "ATLAN_PROMETHEUS_PUSHGATEWAY_URL is not set; worker will run "
                "without pushing metrics. Set the env var (or run in combined "
                "mode) to enable Prometheus visibility."
            )
            return

        from prometheus_client import REGISTRY  # noqa: PLC0415 — pushgateway cold path

        from application_sdk.observability.pushgateway import (  # noqa: PLC0415 — pushgateway cold path
            PushGatewayClient,
            TemporalCoreCollector,
        )

        # Bridge Temporal Rust-core metrics into the global registry so the
        # Pushgateway push includes them. Idempotent — duplicate registration
        # raises ValueError, which we swallow.
        try:
            REGISTRY.register(
                TemporalCoreCollector(
                    f"http://{TEMPORAL_PROMETHEUS_BIND_ADDRESS}/metrics"
                )
            )
        except ValueError:
            logger.debug(
                "TemporalCoreCollector already registered; skipping",
                exc_info=True,
            )

        self._pusher = PushGatewayClient(
            url=PROMETHEUS_PUSHGATEWAY_URL,
            job=f"{self._primary_app_name or 'application-sdk'}-worker",
            task_queue=self._task_queue,
            interval_s=PROMETHEUS_PUSHGATEWAY_INTERVAL_SECONDS,
            delete_on_shutdown=PROMETHEUS_PUSHGATEWAY_DELETE_ON_SHUTDOWN,
            sweep_stale_on_start=PROMETHEUS_PUSHGATEWAY_SWEEP_STALE_ON_START,
            sweep_staleness_seconds=PROMETHEUS_PUSHGATEWAY_SWEEP_STALENESS_SECONDS,
            http_timeout_s=PROMETHEUS_PUSHGATEWAY_HTTP_TIMEOUT_SECONDS,
            shutdown_delete_delay_s=PROMETHEUS_PUSHGATEWAY_SHUTDOWN_DELETE_DELAY_SECONDS,
        )
        await self._pusher.start()

    async def _stop_metrics_push(self) -> None:
        if self._pusher is not None:
            try:
                await self._pusher.stop()
            except Exception:
                logger.warning("Pushgateway pusher stop failed", exc_info=True)
            finally:
                self._pusher = None

    async def _drain_sizing(self) -> None:
        """Flush buffered sizing rows before the process ends — these pools scale to
        zero, so the last batch would otherwise be lost. Best-effort.
        """
        if not load_interceptor_settings().enable_sizing_telemetry:
            return
        try:
            from application_sdk.observability.sizing_sink import (  # noqa: PLC0415 — cold path: shutdown only, and only when collection is enabled
                drain,
            )

            await drain()
        # conformance: ignore[E004] shutdown-path telemetry; a failed flush must not block or fail shutdown
        except Exception:
            logger.warning("sizing drain failed; buffered rows lost", exc_info=True)

    async def __aenter__(self) -> Worker:
        await _emit_worker_start_event(**self._start_event_params)
        # Metrics is best-effort: never block the worker on a metrics failure.
        try:
            await self._start_metrics_push()
        except Exception:
            logger.error(
                "Pushgateway pusher start failed — worker will run without metrics",
                exc_info=True,
            )
        return await self._worker.__aenter__()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, *args: object
    ) -> None:
        try:
            # Yield to the event loop so in-flight activity result RPCs
            # (e.g. RespondActivityTaskFailed) can complete before we stop
            # the transport. Without this, a race between SIGTERM and
            # activity completion can leave orphaned task slots that block
            # shutdown for the entire graceful_shutdown_timeout.
            await asyncio.sleep(self._shutdown_drain_delay_seconds)
            await self._worker.__aexit__(exc_type, *args)
        finally:
            await self._drain_sizing()
            await self._stop_metrics_push()

    async def run(self) -> None:
        """For callers that use worker.run() directly."""
        await _emit_worker_start_event(**self._start_event_params)
        # Metrics is best-effort: never block the worker on a metrics failure.
        try:
            await self._start_metrics_push()
        except Exception:
            logger.error(
                "Pushgateway pusher start failed — worker will run without metrics",
                exc_info=True,
            )
        try:
            await self._worker.run()
        finally:
            await self._drain_sizing()
            await self._stop_metrics_push()


async def _emit_worker_start_event(
    task_queue: str,
    app_name: str,
    workflow_count: int,
    activity_count: int,
    max_concurrent_activities: int,
    max_concurrent_workflow_tasks: int | None = None,
    host: str = "",
    namespace: str = "",
    build_id: str = "",
    use_worker_versioning: bool = False,
) -> None:
    """Emit a worker_start lifecycle event via the v3 infrastructure event binding."""
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: worker startup config
        APP_SDK_VERSION,
        APP_TYPE,
        APPLICATION_VERSION,
        DEPLOYMENT_OBJECT_STORE_NAME,
        PUBLISHED_AT,
        RELEASE_CHANNEL,
        RELEASE_ID,
        SECRET_STORE_NAME,
        UPSTREAM_OBJECT_STORE_NAME,
    )
    from application_sdk.contracts.events import (  # noqa: PLC0415 — circular: contracts.events imports execution.errors
        ApplicationEventNames,
        Event,
        EventTypes,
        WorkerStartEventData,
    )
    from application_sdk.execution._temporal.interceptors.events import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
        _publish_event_via_binding,
    )
    from application_sdk.infrastructure._dapr.http import (  # noqa: PLC0415 — circular: infrastructure imports execution transitively
        get_dapr_component_types,
    )
    from application_sdk.infrastructure.bindings import (  # noqa: PLC0415 — circular: infrastructure imports execution transitively
        BindingError,
    )

    deployment_name = os.environ.get("ATLAN_DEPLOYMENT_NAME", app_name)
    host_part, _, port_part = host.partition(":")

    # Discover which Dapr binding types back the object/secret stores. Best-effort
    # and deploy-path-agnostic: read from the live sidecar rather than env.
    component_types = await get_dapr_component_types()

    event_data = WorkerStartEventData(
        application_name=app_name,
        deployment_name=deployment_name,
        task_queue=task_queue,
        namespace=namespace,
        host=host_part,
        port=port_part,
        connection_string=host,
        max_concurrent_activities=max_concurrent_activities,
        max_concurrent_workflow_tasks=max_concurrent_workflow_tasks,
        workflow_count=workflow_count,
        activity_count=activity_count,
        build_id=build_id or None,
        use_worker_versioning=use_worker_versioning,
        app_version=APPLICATION_VERSION,
        release_id=RELEASE_ID,
        release_channel=RELEASE_CHANNEL,
        sdk_version=APP_SDK_VERSION,
        app_type=APP_TYPE,
        published_at=PUBLISHED_AT,
        objectstore_binding_type=component_types.get(DEPLOYMENT_OBJECT_STORE_NAME, ""),
        upstream_objectstore_binding_type=component_types.get(
            UPSTREAM_OBJECT_STORE_NAME, ""
        ),
        secretstore_binding_type=component_types.get(SECRET_STORE_NAME, ""),
    )
    event = Event(
        event_type=EventTypes.APPLICATION_EVENT.value,
        event_name=ApplicationEventNames.WORKER_START.value,
        data=event_data.model_dump(),
    )

    try:
        await _publish_event_via_binding(event)
    except BindingError:
        logger.warning(
            "eventstore binding unavailable — worker_start event not emitted",
            exc_info=True,
        )
    except Exception:
        logger.warning("Failed to emit worker_start event", exc_info=True)


def create_worker(
    client: Client,
    task_queue: str = "application-sdk",
    *,
    handler: Handler | None = None,
    enable_sdr: bool = True,
    passthrough_modules: set[str] | None = None,
    service_name: str | None = None,
    max_concurrent_activities: int | None = None,
    max_concurrent_workflow_tasks: int | None = None,
    graceful_shutdown_timeout_seconds: int | None = None,
    interceptors: list[TemporalInterceptor] | None = None,
    enable_pushgateway: bool = False,
    on_activity: Callable[[], None] | None = None,
    on_fatal_error: Callable[[BaseException], None] | None = None,
) -> AppWorker:
    """Create a Temporal worker for registered Apps.

    The worker registers:
    - One workflow per entry point per App
    - All @task methods as named activities (qualified as ``{app}:{task}``)
    - Three SDR workflows (``sdr:test_auth`` / ``sdr:preflight_check`` /
      ``sdr:fetch_metadata``) bound to ``handler`` when one is provided and
      ``enable_sdr`` is true.

    Apps must be imported/registered before creating the worker.

    Args:
        client: Temporal client.
        task_queue: Task queue to listen on.
        handler: Optional Handler instance.  When provided and ``enable_sdr``
            is true, the three SDR workflows are registered so platform
            callers can invoke ``test_auth`` / ``preflight_check`` /
            ``fetch_metadata`` durably as Temporal workflows (in addition to
            the HTTP endpoints served by ``handler/service.py``).
        enable_sdr: Opt-out flag for SDR registration.  Ignored when
            ``handler`` is ``None``.
        passthrough_modules: Additional modules to pass through the sandbox.
        service_name: Service name for observability (traces/metrics).
        max_concurrent_activities: Maximum number of concurrent activity executions.
        max_concurrent_workflow_tasks: Maximum number of in-flight workflow task
            pollers, which bounds the number of workflow sandboxes the worker
            spins up concurrently. Leave ``None`` to use Temporal's default. Pin
            this when many workflows fire simultaneously (e.g. cron bursts) and
            the worker has limited activity capacity — excess sandboxes sitting
            idle past the deadlock-detection timeout trip TMPRL1101 and bloat
            resident memory. A common AE-derived heuristic is to pin it to the
            same value as ``max_concurrent_activities`` so the active sandbox
            count stays bounded by what the worker can actually drain.
        graceful_shutdown_timeout_seconds: Seconds to allow in-flight activities to
            complete after SIGTERM before cancelling them.
        interceptors: Additional Temporal interceptors to register. Log /
            Metrics / Trace observability interceptors are always prepended;
            Output and Event interceptors are prepended based on settings.
        enable_pushgateway: When True (worker-only deployments), the worker
            starts a periodic Prometheus Pushgateway pusher on entry and
            performs a final push on exit. Combined deployments (server +
            worker in one process) should leave this False so /metrics
            doesn't double-count.
        on_activity: Optional callback fired on every activity execution and
            heartbeat. The main entry point wires this to
            ``WorkerHealthServer.record_activity`` so the ``/live`` probe can
            reflect real worker progress.
        on_fatal_error: Optional callback fired when a poll loop dies fatally.
            The SDK always logs the fatal's full cause chain first (that hook is
            the only place a fatal is guaranteed to be observed — see
            ``_log_worker_fatal_error``); this callback is additive, and the main
            entry point wires it to ``WorkerHealthServer.record_worker_fatal``.

    Returns:
        AppWorker wrapping a configured Temporal Worker (not yet started).
        The ``worker_start`` lifecycle event is emitted automatically on
        ``async with worker:`` or ``await worker.run()``.

    Example:
        from my_package.apps import MyPipeline

        client = await create_temporal_client("localhost:7233")
        worker = create_worker(client)
        await worker.run()
    """
    app_workflows = get_all_app_workflows()
    task_activities = get_all_task_activities()

    from application_sdk.execution._temporal.preflight_gate import (  # noqa: PLC0415 — lazy: handler-activity machinery loaded at worker assembly
        build_preflight_gate_activity,
        log_gate_posture,
        preflight_gate_activity_name,
        resolve_gate_attempts,
        resolve_gate_budget_seconds,
    )
    from application_sdk.handler.base import DefaultHandler  # noqa: PLC0415

    # When the app ships no Handler, DefaultHandler's no-op (no checks → never blocks)
    # keeps the gate present but non-blocking.
    gate_handler = handler if handler is not None else DefaultHandler()

    sdr_registry = AppRegistry.get_instance()
    sdr_registered_apps = sdr_registry.list_all()
    resolved_app_name = (
        sdr_registered_apps[0].name
        if sdr_registered_apps
        else (service_name or task_queue)
    )

    # Registered independent of the SDR opt-out — the gate is mandatory. Names
    # deduped: an app registered under multiple versions appears once per version,
    # and two activities under one name crash boot.
    gate_app_names = list(dict.fromkeys(m.name for m in sdr_registered_apps)) or [
        resolved_app_name
    ]
    gate_activity_names = [
        preflight_gate_activity_name(name) for name in gate_app_names
    ]

    # Temporal's own duplicate-activity rejection is an opaque ValueError; surface
    # a descriptive collision error naming the offending task and the fix instead.
    task_activity_names = {
        get_activity_name(tm.app_name, tm.name)
        for tasks in TaskRegistry.get_instance().get_all_tasks().values()
        for tm in tasks
    }
    gate_collisions = sorted(set(gate_activity_names) & task_activity_names)
    if gate_collisions:
        from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
            WorkerActivityNameCollisionError,
        )

        raise WorkerActivityNameCollisionError(
            message=(
                f"App task(s) register activity name(s) {gate_collisions}, which the SDK "
                "reserves for the injected preflight gate. Rename the offending @task "
                "method (a discovery step 'preflight' -> 'fetch_databases'/'discover', or "
                "fold a readiness check into Handler.preflight_check). A worker cannot "
                "register two activities with the same name."
            ),
            field="task_name",
        )

    name_to_app_cls = {m.name: m.app_cls for m in sdr_registered_apps}

    # ADR-0020 step 8. Deferred for the same reason activities.py defers it:
    # importing any `validation` submodule loads the package __init__, which pulls
    # pyatlan_v9 in via `assets`, and only a worker process ever needs it.
    from application_sdk.constants import (  # noqa: PLC0415 — read here, not at module scope, so a test can flip the kill switch and see the posture row follow it
        VALIDATE_ARTIFACTS,
    )
    from application_sdk.validation.interceptor import (  # noqa: PLC0415 — see above
        log_artifact_validation_posture,
        resolve_artifact_enforcement,
    )

    gate_activities = []
    for name in gate_app_names:
        app_cls = name_to_app_cls.get(name)

        # Artifact validation rides the activity interceptor rather than a
        # registered activity of its own, so this loop's only job for it is the
        # boot-time posture row — emitted for every app, soft and switched-off
        # included, because an app whose tasks hand off no artifacts emits no
        # outcome row at all and would otherwise be indistinguishable from one
        # that is not registered. The posture itself is resolved a second time in
        # `create_activity_from_task`, through the same one function, so the row
        # here and the behaviour there cannot disagree.
        artifact_enforce = resolve_artifact_enforcement(app_cls)
        log_artifact_validation_posture(
            name, enforce=artifact_enforce, enabled=VALIDATE_ARTIFACTS
        )
        if artifact_enforce and VALIDATE_ARTIFACTS:
            # conformance: ignore[L006] not a per-item log: the loop is over registered apps (single digits) and this fires once per hard-mode one at boot. Demoting the one notice that a worker will start failing hand-offs to DEBUG would hide it in exactly the deployment that needs it.
            logger.info(
                "Artifact validation is HARD for app %r — a FileReference that "
                "disagrees with the artifact schema the app declared for its "
                "contract field WILL fail the activity, on both sides of a task "
                "(at ingest after materialise, and at hand-off before persist). "
                "An undeclared artifact blocks only on an entrypoint's public "
                "boundary, and a failure of the SDK's own validator always fails "
                "open. This is the per-app opt-in; the default posture is soft "
                "(report only, never block).",
                name,
            )

        enforce = _resolve_gate_enforcement(app_cls)
        budget_seconds = resolve_gate_budget_seconds(
            getattr(app_cls, "preflight_gate_timeout_seconds", None)
        )
        attempts = resolve_gate_attempts(
            getattr(app_cls, "preflight_gate_max_attempts", None)
        )
        # Every app, soft included: this row is the denominator for ranking
        # hard-mode apps that never reach a verdict (such an app emits no outcome
        # row carrying gate_mode, so it is invisible from outcomes alone).
        log_gate_posture(name, enforce=enforce, budget_seconds=budget_seconds)
        if enforce:
            # conformance: ignore[L006] same as the artifact-validation notice above: once per hard-mode app at boot, over a single-digit loop, and it is the one line saying a worker will start aborting runs.
            logger.info(
                "Preflight gate is HARD for app %r — the run WILL abort before "
                "extraction on a NOT_READY verdict, and on any outcome the gate "
                "attributes to the source (probe overrunning the %ds budget, "
                "handler crash, missing credential). Gate plumbing failures still "
                "fail open. This is the per-app opt-in; the default posture is "
                "soft (report only, never block).",
                name,
                budget_seconds,
            )
        gate_activities.append(
            build_preflight_gate_activity(
                gate_handler,
                name,
                enforce=enforce,
                budget_seconds=budget_seconds,
                attempts=attempts,
            )
        )
    task_activities = [*task_activities, *gate_activities]

    # SDR (the control-plane test_auth/preflight_check/fetch_metadata workflows)
    # requires a REAL handler — never the bare DefaultHandler sentinel. Both the
    # worker path (passes None) and the combined path (passes DefaultHandler() to
    # also serve HTTP) fall back for handler-less apps; binding that to SDR would
    # expose sdr:test_auth returning unconditional SUCCESS — a fake green on the
    # Sage "Check". Exact-type check, not isinstance: a DefaultHandler *subclass*
    # with real overrides is a real handler and does get SDR. The gate above uses
    # gate_handler regardless because it must always be dispatchable.
    has_real_handler = handler is not None and type(handler) is not DefaultHandler
    if enable_sdr and has_real_handler:
        from application_sdk.execution._temporal.sdr import (  # noqa: PLC0415 — lazy: only load SDR workflows when SDR is enabled
            SDR_WORKFLOWS,
            build_sdr_activities,
        )

        app_workflows = [*app_workflows, *SDR_WORKFLOWS]
        task_activities = [
            *task_activities,
            *build_sdr_activities(handler, resolved_app_name),
        ]
        logger.info(
            "SDR workflows registered for handler %s (app=%s)",
            type(handler).__name__,
            resolved_app_name,
        )

    interceptor_settings = load_interceptor_settings()

    # The three observability interceptors are unconditional and run first so
    # ContextVars (ExecutionContext, CorrelationContext) and tracing spans are
    # set before product-feature interceptors or user code observe them.
    from application_sdk.execution._temporal.interceptors import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
        LogInterceptor,
        MetricsInterceptor,
        TraceInterceptor,
    )

    # Guard against double-registration: callers migrating from v2 may pass
    # one of these explicitly via ``interceptors=...``. Running them twice
    # would double-count metrics and emit duplicate lifecycle log lines —
    # silent corruption that's hard to diagnose. Fail loudly at startup.
    _builtin_types = (LogInterceptor, MetricsInterceptor, TraceInterceptor)
    _duplicates = [
        type(i).__name__ for i in (interceptors or []) if isinstance(i, _builtin_types)
    ]
    if _duplicates:
        from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
            WorkerInterceptorDuplicateError,
        )

        raise WorkerInterceptorDuplicateError(
            message=f"Duplicate interceptor types: {_duplicates}. The SDK adds "
            "LogInterceptor / MetricsInterceptor / TraceInterceptor automatically. "
            "Remove them from your `interceptors` list.",
            field="interceptors",
        )

    all_interceptors: list[TemporalInterceptor] = [
        LogInterceptor(),
        MetricsInterceptor(),
        TraceInterceptor(),
    ]

    # Liveness recording is appended after the SDK's own Log/Metrics/Trace
    # interceptors but before the user-supplied interceptors and the
    # OutputInterceptor, so a stall in any of those downstream interceptors
    # still counts as "activity observed" — the goal is to detect a dead poll
    # loop, not to gate on downstream success.
    if on_activity is not None:
        from application_sdk.execution._temporal.interceptors.liveness import (  # noqa: PLC0415 — cold path: only when a liveness callback is wired
            LivenessInterceptor,
        )

        all_interceptors.append(LivenessInterceptor(on_activity))

    # Before user interceptors, so what they hold is inside the measured window.
    if interceptor_settings.enable_sizing_telemetry:
        _sizing_activities = interceptor_settings.sizing_telemetry_activities
        if not _sizing_activities:
            # Fail-closed is silent, so say so.
            logger.warning(
                "APPLICATION_SDK_ENABLE_SIZING_TELEMETRY is on but "
                "APPLICATION_SDK_SIZING_TELEMETRY_ACTIVITIES is empty, so nothing "
                "will be collected. Name the activities to measure, or set '*' to "
                "measure all of them."
            )
        else:
            from application_sdk.execution._temporal.interceptors.sizing import (  # noqa: PLC0415 — cold path: only when sizing collection is enabled
                SizingTelemetryInterceptor,
            )

            all_interceptors.append(
                SizingTelemetryInterceptor(
                    poll_interval_seconds=interceptor_settings.sizing_telemetry_poll_seconds,
                    activities=_sizing_activities,
                )
            )
            logger.info(
                "Activity sizing telemetry enabled for %s (poll interval %ss). "
                "Measurement only — no routing decisions are made from it.",
                sorted(_sizing_activities),
                interceptor_settings.sizing_telemetry_poll_seconds,
            )

    all_interceptors.extend(interceptors or [])

    if interceptor_settings.enable_output_interceptor:
        from application_sdk.execution._temporal.interceptors.outputs import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            OutputInterceptor,
        )

        all_interceptors.append(OutputInterceptor())

    registry = AppRegistry.get_instance()
    registered_apps = registry.list_all()
    primary_app_name = (
        registered_apps[0].name if registered_apps else (service_name or task_queue)
    )

    # ADR-0016 §3: log resolved pool→queue map at startup so a misconfigured
    # env var (typo, missing ATLAN_POOL_<POOL>_QUEUE) is diagnosable immediately
    # rather than manifesting as a silent activity backlog hours later.
    task_registry = TaskRegistry.get_instance()
    _pool_queue_map: dict[str, str] = {}
    for _tasks in task_registry.get_all_tasks().values():
        for _tm in _tasks:
            if _tm.pool and _tm.pool not in _pool_queue_map:
                _queue = resolve_pool_queue(_tm.pool)
                if _queue is not None:
                    _pool_queue_map[_tm.pool] = _queue
                else:
                    logger.warning(
                        "Pool %r has no resolvable queue: "
                        "set ATLAN_POOL_%s_QUEUE or ATLAN_TASK_QUEUE. "
                        "Activities dispatched to this pool will run on the workflow's default queue.",
                        _tm.pool,
                        _tm.pool.upper().replace("-", "_"),
                    )
    if _pool_queue_map:
        logger.info("Pool queue map: %s", _pool_queue_map)

    if interceptor_settings.enable_event_interceptor:
        from application_sdk.execution._temporal.interceptors.events import (  # noqa: PLC0415 — circular: execution/__init__.py loads sibling modules + app.base imports execution
            EventInterceptor,
            publish_event,
        )

        all_interceptors.append(EventInterceptor())
        task_activities = [*task_activities, publish_event]

    # Build sandbox configuration
    config = SandboxConfig()

    if passthrough_modules:
        config = config.with_passthrough_modules(*passthrough_modules)

    app_modules = registry.get_all_passthrough_modules()
    if app_modules:
        config = config.with_passthrough_modules(*app_modules)

    # Pass through all app module paths to prevent re-registration in sandbox
    app_module_paths: set[str] = set()
    for app_meta in registered_apps:
        if app_meta.module_path:
            app_module_paths.add(app_meta.module_path)
    if app_module_paths:
        config = config.with_passthrough_modules(*app_module_paths)

    workflow_runner = SandboxedWorkflowRunner(
        restrictions=config.to_temporal_restrictions()
    )

    if max_concurrent_activities is None:
        max_concurrent_activities = load_execution_settings().max_concurrent_activities

    if graceful_shutdown_timeout_seconds is None:
        graceful_shutdown_timeout_seconds = (
            load_execution_settings().graceful_shutdown_timeout_seconds
        )

    # Worker Deployment versioning — set by TWD controller via Kubernetes Downward API.
    # ATLAN_APP_BUILD_ID alone: legacy build-ID mode (build ID doubles as deployment name).
    # ATLAN_APP_BUILD_ID + ATLAN_APP_DEPLOYMENT_NAME: full Worker Deployment versioning.
    # default_versioning_behavior defaults to PINNED; an app may opt into
    # AUTO_UPGRADE via TEMPORAL_DEFAULT_VERSIONING_BEHAVIOR in its own deployment.
    versioning_behavior = load_execution_settings().default_versioning_behavior
    deployment_config: WorkerDeploymentConfig | None = None
    if APP_BUILD_ID and APP_DEPLOYMENT_NAME:
        deployment_config = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=APP_DEPLOYMENT_NAME,
                build_id=APP_BUILD_ID,
            ),
            use_worker_versioning=True,
            default_versioning_behavior=versioning_behavior,
        )
        logger.info(
            "Worker Deployment versioning enabled: deployment=%s build_id=%s behavior=%s",
            APP_DEPLOYMENT_NAME,
            APP_BUILD_ID,
            versioning_behavior.name,
        )
    elif APP_BUILD_ID:
        deployment_config = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=APP_BUILD_ID,
                build_id=APP_BUILD_ID,
            ),
            use_worker_versioning=True,
            default_versioning_behavior=versioning_behavior,
        )
        logger.info(
            "Worker versioning enabled: build_id=%s behavior=%s",
            APP_BUILD_ID,
            versioning_behavior.name,
        )

    async def _fatal_error_hook(exc: BaseException) -> None:
        # Always log — the diagnostic must not depend on a caller opting in.
        # Guard even this mandatory path: if rendering the cause chain itself
        # raises (e.g. an exception whose ``__str__`` blows up), fall back to a
        # minimal line that does not render ``exc`` — otherwise the hook
        # propagates and temporalio's own "Fatal error handler failed" wrapper
        # silently swaps in, losing the rich cause-chain log this hook exists
        # to produce. exc_info=True here logs the traceback of the *rendering
        # failure*, not of ``exc``. That masks the link whose rendering failed
        # (it appears as a ``<exception str() failed>`` placeholder); chain links
        # that rendered before the failure may still appear, so this bounds which
        # link is lost, not what the log can contain. No new exposure either way:
        # those same messages reach the logs on the primary path whenever
        # rendering succeeds.
        try:
            await _log_worker_fatal_error(exc)
        except BaseException:
            logger.error(
                "Temporal worker poll loop failed fatally (cause chain "
                "unavailable: rendering the exception raised %s)",
                type(exc).__name__,
                exc_info=True,
            )
        if on_fatal_error is None:
            return
        try:
            on_fatal_error(exc)
        except Exception:
            logger.warning(
                "on_fatal_error callback failed; the fatal itself is already logged",
                exc_info=True,
            )

    worker_kwargs: dict = dict(
        task_queue=task_queue,
        workflows=app_workflows,
        activities=task_activities,
        on_fatal_error=_fatal_error_hook,
        workflow_runner=workflow_runner,
        interceptors=all_interceptors,
        max_concurrent_activities=max_concurrent_activities,
        # Bypass Temporal's default 80% heartbeat throttle so heartbeats fire
        # at the configured interval (~10s) rather than at 80% of timeout.
        max_heartbeat_throttle_interval=timedelta(seconds=10),
        graceful_shutdown_timeout=timedelta(seconds=graceful_shutdown_timeout_seconds),
        workflow_failure_exception_types=_WORKFLOW_FAILURE_EXCEPTION_TYPES,
    )
    # Only forward max_concurrent_workflow_tasks when explicitly set; passing
    # None would override Temporal's default with None and crash the worker.
    if max_concurrent_workflow_tasks is not None:
        worker_kwargs["max_concurrent_workflow_tasks"] = max_concurrent_workflow_tasks
    if deployment_config is not None:
        worker_kwargs["deployment_config"] = deployment_config

    worker = Worker(client, **worker_kwargs)

    host = getattr(
        getattr(getattr(client, "service_client", None), "config", None),
        "target_host",
        "",
    )
    namespace = getattr(client, "namespace", "")

    return AppWorker(
        worker,
        start_event_params={
            "task_queue": task_queue,
            "app_name": primary_app_name,
            "workflow_count": len(app_workflows),
            "activity_count": len(task_activities),
            "max_concurrent_activities": max_concurrent_activities,
            "max_concurrent_workflow_tasks": max_concurrent_workflow_tasks,
            "host": host,
            "namespace": namespace,
            "build_id": APP_BUILD_ID,
            "use_worker_versioning": deployment_config is not None,
        },
        enable_pushgateway=enable_pushgateway,
        primary_app_name=primary_app_name,
        task_queue=task_queue,
    )
