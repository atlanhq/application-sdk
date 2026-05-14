"""Temporal worker configuration and setup.

Workers execute Apps as workflows and tasks as activities.
Each App becomes its own named workflow, and @task methods become
named activities.
"""

from __future__ import annotations

import asyncio
import os
from datetime import timedelta
from typing import TYPE_CHECKING

from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.worker import Interceptor as TemporalInterceptor
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from application_sdk.app.registry import AppRegistry
from application_sdk.constants import (
    APP_BUILD_ID,
    APP_DEPLOYMENT_NAME,
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
    """

    def __init__(
        self,
        worker: Worker,
        *,
        start_event_params: dict,
        enable_pushgateway: bool = False,
        primary_app_name: str = "",
        task_queue: str = "",
    ) -> None:
        self._worker = worker
        self._start_event_params = start_event_params
        self._enable_pushgateway = enable_pushgateway
        self._primary_app_name = primary_app_name
        self._task_queue = task_queue
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
            pass

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
            await asyncio.sleep(SHUTDOWN_DRAIN_DELAY_SECONDS)
            await self._worker.__aexit__(exc_type, *args)
        finally:
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
            await self._stop_metrics_push()


async def _emit_worker_start_event(
    task_queue: str,
    app_name: str,
    workflow_count: int,
    activity_count: int,
    max_concurrent_activities: int,
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
        PUBLISHED_AT,
        RELEASE_CHANNEL,
        RELEASE_ID,
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
    from application_sdk.infrastructure.bindings import (  # noqa: PLC0415 — circular: infrastructure imports execution transitively
        BindingError,
    )

    deployment_name = os.environ.get("ATLAN_DEPLOYMENT_NAME", app_name)
    host_part, _, port_part = host.partition(":")

    event_data = WorkerStartEventData(
        application_name=app_name,
        deployment_name=deployment_name,
        task_queue=task_queue,
        namespace=namespace,
        host=host_part,
        port=port_part,
        connection_string=host,
        max_concurrent_activities=max_concurrent_activities,
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
    graceful_shutdown_timeout_seconds: int | None = None,
    interceptors: list[TemporalInterceptor] | None = None,
    enable_pushgateway: bool = False,
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

    if enable_sdr and handler is not None:
        from application_sdk.execution._temporal.sdr import (  # noqa: PLC0415 — lazy: only load SDR/handler modules when a Handler is provided
            SDR_WORKFLOWS,
            build_sdr_activities,
        )

        sdr_registry = AppRegistry.get_instance()
        sdr_registered_apps = sdr_registry.list_all()
        sdr_app_name = (
            sdr_registered_apps[0].name
            if sdr_registered_apps
            else (service_name or task_queue)
        )
        app_workflows = [*app_workflows, *SDR_WORKFLOWS]
        task_activities = [
            *task_activities,
            *build_sdr_activities(handler, sdr_app_name),
        ]
        logger.info(
            "SDR workflows registered for handler %s (app=%s)",
            type(handler).__name__,
            sdr_app_name,
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
    deployment_config: WorkerDeploymentConfig | None = None
    if APP_BUILD_ID and APP_DEPLOYMENT_NAME:
        deployment_config = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=APP_DEPLOYMENT_NAME,
                build_id=APP_BUILD_ID,
            ),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        )
        logger.info(
            "Worker Deployment versioning enabled: deployment=%s build_id=%s",
            APP_DEPLOYMENT_NAME,
            APP_BUILD_ID,
        )
    elif APP_BUILD_ID:
        deployment_config = WorkerDeploymentConfig(
            version=WorkerDeploymentVersion(
                deployment_name=APP_BUILD_ID,
                build_id=APP_BUILD_ID,
            ),
            use_worker_versioning=True,
            default_versioning_behavior=VersioningBehavior.PINNED,
        )
        logger.info("Worker versioning enabled: build_id=%s", APP_BUILD_ID)

    worker_kwargs: dict = dict(
        task_queue=task_queue,
        workflows=app_workflows,
        activities=task_activities,
        workflow_runner=workflow_runner,
        interceptors=all_interceptors,
        max_concurrent_activities=max_concurrent_activities,
        # Bypass Temporal's default 80% heartbeat throttle so heartbeats fire
        # at the configured interval (~10s) rather than at 80% of timeout.
        max_heartbeat_throttle_interval=timedelta(seconds=10),
        graceful_shutdown_timeout=timedelta(seconds=graceful_shutdown_timeout_seconds),
    )
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
            "host": host,
            "namespace": namespace,
            "build_id": APP_BUILD_ID,
            "use_worker_versioning": deployment_config is not None,
        },
        enable_pushgateway=enable_pushgateway,
        primary_app_name=primary_app_name,
        task_queue=task_queue,
    )
