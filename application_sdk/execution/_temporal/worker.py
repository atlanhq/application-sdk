"""Temporal worker configuration and setup.

Workers execute Apps as workflows and tasks as activities.
Each App becomes its own named workflow, and @task methods become
named activities.
"""

from __future__ import annotations

import os
from datetime import timedelta
from typing import TYPE_CHECKING

from temporalio.client import Client
from temporalio.common import VersioningBehavior
from temporalio.worker import Interceptor as TemporalInterceptor
from temporalio.worker import Worker, WorkerDeploymentConfig, WorkerDeploymentVersion
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from application_sdk.app.registry import AppRegistry
from application_sdk.constants import APP_BUILD_ID, APP_DEPLOYMENT_NAME
from application_sdk.execution._temporal.activities import get_all_task_activities
from application_sdk.execution._temporal.workflows import get_all_app_workflows
from application_sdk.execution.sandbox import SandboxConfig
from application_sdk.execution.settings import (
    load_execution_settings,
    load_interceptor_settings,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

if TYPE_CHECKING:
    pass


class AppWorker:
    """Wraps Temporal Worker to emit worker_start on startup.

    Emits the ``worker_start`` lifecycle event on ``__aenter__`` (and ``run()``)
    so that every code path that starts a worker automatically registers the
    agent — regardless of whether the caller uses ``async with worker:`` or
    ``await worker.run()``.
    """

    def __init__(self, worker: Worker, *, start_event_params: dict) -> None:
        self._worker = worker
        self._start_event_params = start_event_params

    async def __aenter__(self) -> Worker:
        await _emit_worker_start_event(**self._start_event_params)
        return await self._worker.__aenter__()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, *args: object
    ) -> None:
        await self._worker.__aexit__(exc_type, *args)

    async def run(self) -> None:
        """For callers that use worker.run() directly."""
        await _emit_worker_start_event(**self._start_event_params)
        await self._worker.run()


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
    from application_sdk.contracts.events import (
        ApplicationEventNames,
        Event,
        EventTypes,
        WorkerStartEventData,
    )
    from application_sdk.execution._temporal.interceptors.events import (
        _publish_event_via_binding,
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
    )
    event = Event(
        event_type=EventTypes.APPLICATION_EVENT.value,
        event_name=ApplicationEventNames.WORKER_START.value,
        data=event_data.model_dump(),
    )

    try:
        await _publish_event_via_binding(event)
    except Exception:
        logger.warning("Failed to emit worker_start event", exc_info=True)


def create_worker(
    client: Client,
    task_queue: str = "application-sdk",
    *,
    app_names: list[str] | None = None,
    passthrough_modules: set[str] | None = None,
    service_name: str | None = None,
    max_concurrent_activities: int | None = None,
    graceful_shutdown_timeout_seconds: int | None = None,
    interceptors: list[TemporalInterceptor] | None = None,
) -> AppWorker:
    """Create a Temporal worker for registered Apps.

    The worker registers:
    - One workflow per App (named after the app: 'my-pipeline', 'my-loader', etc.)
    - All @task methods as named activities

    Apps must be imported/registered before creating the worker.

    Args:
        client: Temporal client.
        task_queue: Task queue to listen on.
        app_names: Optional list of App names to include. If None, includes all.
        passthrough_modules: Additional modules to pass through the sandbox.
        service_name: Service name for observability (traces/metrics).
        max_concurrent_activities: Maximum number of concurrent activity executions.
        graceful_shutdown_timeout_seconds: Seconds to allow in-flight activities to
            complete after SIGTERM before cancelling them.
        interceptors: Additional Temporal interceptors to register. Correlation and
            event interceptors are automatically prepended based on settings.

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
    task_activities = get_all_task_activities(app_names=app_names)

    interceptor_settings = load_interceptor_settings()

    # ExecutionContextInterceptor is always first — it populates the ContextVar that
    # all downstream interceptors and user code read for observability context.
    from application_sdk.execution._temporal.interceptors.execution_context_interceptor import (
        ExecutionContextInterceptor,
    )

    all_interceptors: list[TemporalInterceptor] = [ExecutionContextInterceptor()]
    all_interceptors.extend(interceptors or [])

    if interceptor_settings.enable_correlation_interceptor:
        from application_sdk.execution._temporal.interceptors.correlation_interceptor import (
            CorrelationContextInterceptor,
        )

        all_interceptors.append(CorrelationContextInterceptor())

    registry = AppRegistry.get_instance()
    registered_apps = registry.list_all()
    primary_app_name = (
        registered_apps[0].name if registered_apps else (service_name or task_queue)
    )

    if interceptor_settings.enable_event_interceptor:
        from application_sdk.execution._temporal.interceptors.events import (
            EventInterceptor,
            publish_event,
        )

        all_interceptors.append(EventInterceptor())
        task_activities = [*task_activities, publish_event]

    from application_sdk.execution._temporal.interceptors.activity_failure_logging import (
        TaskFailureLoggingInterceptor,
    )

    all_interceptors.append(TaskFailureLoggingInterceptor())

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
            default_versioning_behavior=VersioningBehavior.AUTO_UPGRADE,
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
            default_versioning_behavior=VersioningBehavior.AUTO_UPGRADE,
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
    )
