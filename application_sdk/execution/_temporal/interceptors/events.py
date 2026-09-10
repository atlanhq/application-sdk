"""Event tracking interceptors for Temporal workflows and activities.

Publishes lifecycle events (workflow/activity start/end, worker start) via
the v3 infrastructure event binding. Falls back silently when no event
binding is configured.

When the binding *is* configured but the Dapr call fails, the event is
re-sent directly to Event Ingress over HTTPS from this process (see
:func:`_publish_event_direct`), with a WARNING that names the fallback. The
Dapr sidecar is the only Go TLS client in an SDR pod and has been seen
rejected by customer middleboxes that pass every other client; without the
fallback that leaves the agent unregistered. ``ATLAN_EVENT_INGRESS_DIRECT_FALLBACK=false``
turns it off.
"""

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.contracts.events import (
    ApplicationEventNames,
    Event,
    EventMetadata,
    EventTypes,
    WorkflowStates,
)
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)
activity.logger = logger
workflow.logger = logger

# Module-level OAuthTokenService for event-publishing auth headers.
# Constructed lazily on first use; None when auth is not configured.
_event_token_service: "OAuthTokenService | None" = None


async def _get_event_token_service() -> "OAuthTokenService | None":
    """Return the singleton OAuthTokenService for event auth, or None if unconfigured."""
    global _event_token_service

    from application_sdk.constants import (  # noqa: PLC0415 — cold path: AUTH_ENABLED guard
        AUTH_ENABLED,
    )

    if not AUTH_ENABLED:
        return None

    if _event_token_service is None:
        from application_sdk.constants import (  # noqa: PLC0415 — cold path: only when AUTH_ENABLED
            AUTH_URL,
            WORKFLOW_AUTH_CLIENT_ID_KEY,
            WORKFLOW_AUTH_CLIENT_SECRET_KEY,
        )
        from application_sdk.credentials.oauth import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
            OAuthTokenService,
        )
        from application_sdk.credentials.types import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
            OAuthClientCredential,
        )
        from application_sdk.infrastructure.secrets import (  # noqa: PLC0415 — circular: infrastructure imports execution transitively
            get_deployment_secret,
        )

        client_id = await get_deployment_secret(WORKFLOW_AUTH_CLIENT_ID_KEY)
        client_secret = await get_deployment_secret(WORKFLOW_AUTH_CLIENT_SECRET_KEY)
        token_url = AUTH_URL

        if not client_id or not client_secret or not token_url:
            return None

        cred = OAuthClientCredential(
            client_id=client_id,
            client_secret=client_secret,
            token_url=token_url,
        )
        _event_token_service = OAuthTokenService(cred)

    return _event_token_service


# Type alias for the annotation above (resolved at import time by TYPE_CHECKING
# would be circular; plain string forward-reference is fine here).

if TYPE_CHECKING:
    from application_sdk.credentials.oauth import OAuthTokenService

TEMPORAL_NOT_FOUND_FAILURE = (
    "type.googleapis.com/temporal.api.errordetails.v1.NotFoundFailure"
)

# Lifecycle event names that should be sent to Segment
LIFECYCLE_EVENTS = {
    ApplicationEventNames.WORKFLOW_START.value,
    ApplicationEventNames.WORKFLOW_END.value,
    ApplicationEventNames.ACTIVITY_START.value,
    ApplicationEventNames.ACTIVITY_END.value,
}


def _enrich_event_metadata(event: Event) -> Event:
    """Enrich event metadata with Temporal workflow/activity context.

    Mirrors the logic from the v2 EventStore.enrich_event_metadata, inlined
    here so we have no dependency on the v2 services layer.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — cold path: only when computing app_id at startup
        APPLICATION_NAME,
    )

    if not event.metadata:
        event.metadata = EventMetadata()

    event.metadata.application_name = APPLICATION_NAME
    event.metadata.created_timestamp = int(datetime.now(tz=UTC).timestamp())
    event.metadata.topic_name = event.get_topic_name()

    try:
        workflow_info = workflow.info()
        if workflow_info:
            event.metadata.workflow_type = workflow_info.workflow_type
            event.metadata.workflow_id = workflow_info.workflow_id
            event.metadata.workflow_run_id = workflow_info.run_id
    # conformance: ignore[E004] probe to detect workflow context; exception is expected when called outside a workflow
    except Exception:
        logger.debug("Not in workflow context, cannot enrich event metadata")

    try:
        activity_info = activity.info()
        if activity_info:
            event.metadata.activity_type = activity_info.activity_type
            event.metadata.activity_id = activity_info.activity_id
            event.metadata.attempt = activity_info.attempt
            event.metadata.workflow_type = activity_info.workflow_type
            event.metadata.workflow_id = activity_info.workflow_id
            event.metadata.workflow_run_id = activity_info.workflow_run_id
            event.metadata.workflow_state = WorkflowStates.RUNNING.value
    # conformance: ignore[E004] probe to detect activity context; exception is expected when called outside an activity
    except Exception:
        logger.debug("Not in activity context, cannot enrich event metadata")

    return event


def _send_lifecycle_event_to_segment(event: Event) -> None:
    """Send lifecycle event to Segment (best-effort side-channel).

    Never raises — failures are logged at DEBUG level.
    """
    if event.event_name not in LIFECYCLE_EVENTS:
        return

    try:
        import time  # noqa: PLC0415 — cold path: only when emitting metrics

        from application_sdk.constants import (  # noqa: PLC0415 — cold path: only when emitting metrics
            APP_TENANT_ID,
            ATLAN_BASE_URL,
        )
        from application_sdk.observability.metrics_adaptor import (  # noqa: PLC0415 — cold path: metrics adaptor only on emit
            MetricRecord,
            MetricType,
            get_metrics,
        )

        metrics = get_metrics()

        segment_event_name_map = {
            ApplicationEventNames.WORKFLOW_START.value: "workflow_started",
            ApplicationEventNames.WORKFLOW_END.value: "workflow_completed",
            ApplicationEventNames.ACTIVITY_START.value: "activity_started",
            ApplicationEventNames.ACTIVITY_END.value: "activity_ended",
        }

        segment_event_name = segment_event_name_map.get(
            event.event_name, event.event_name
        )

        labels: dict[str, str] = {"send_to_segment": "true"}

        if event.metadata.workflow_id:
            labels["workflow_id"] = event.metadata.workflow_id
        if event.metadata.workflow_run_id:
            labels["workflow_run_id"] = event.metadata.workflow_run_id
        if event.metadata.workflow_type:
            labels["workflow_type"] = event.metadata.workflow_type
        if event.metadata.workflow_state:
            labels["workflow_state"] = event.metadata.workflow_state
        if event.metadata.activity_id:
            labels["activity_id"] = event.metadata.activity_id
        if event.metadata.activity_type:
            labels["activity_type"] = event.metadata.activity_type
        if event.metadata.attempt is not None:
            labels["attempt"] = str(event.metadata.attempt)
        if event.metadata.application_name:
            labels["application_name"] = event.metadata.application_name

        labels["tenant_id"] = APP_TENANT_ID
        if ATLAN_BASE_URL:
            labels["atlan_base_url"] = ATLAN_BASE_URL

        if event.data:
            for key, value in event.data.items():
                if isinstance(value, (str, int, float, bool)):
                    labels[str(key)] = str(value)

        timestamp = (
            event.metadata.created_timestamp / 1000.0
            if event.metadata.created_timestamp
            and event.metadata.created_timestamp > 1e10
            else (
                event.metadata.created_timestamp
                if event.metadata.created_timestamp
                else time.time()
            )
        )

        metric_record = MetricRecord(
            timestamp=timestamp,
            name=segment_event_name,
            value=1.0,
            type=MetricType.COUNTER,
            labels=labels,
            description=f"Lifecycle event: {segment_event_name}",
        )

        metrics.segment_client.send_metric(metric_record)
    # conformance: ignore[E004] best-effort side-channel; exc_info already captured in the debug log below
    except Exception:
        logger.debug("Failed to send lifecycle event to Segment", exc_info=True)


async def _publish_event_via_binding(event: Event) -> None:
    """Publish an event using the v3 infrastructure event binding.

    Silently skips if no event binding is configured. Enriches event
    metadata and sends Segment metrics as a side-channel.
    """
    from application_sdk.infrastructure.context import (  # noqa: PLC0415 — circular: infrastructure.context imports execution transitively
        get_infrastructure,
    )

    infra = get_infrastructure()
    if infra is None or infra.event_binding is None:
        return

    event = _enrich_event_metadata(event)
    _send_lifecycle_event_to_segment(event)

    import orjson  # lazy import: avoid top-level for interceptor module load time  # noqa: PLC0415 — cold path: only on auth refresh

    payload = orjson.dumps(event.model_dump(mode="json"))
    binding_metadata: dict[str, str] = {"content-type": "application/json"}

    try:
        token_service = await _get_event_token_service()
        if token_service is not None:
            binding_metadata.update(await token_service.get_headers())
    except Exception:
        logger.warning(
            "Failed to get auth headers for event binding, proceeding without authentication",
            exc_info=True,
        )

    from application_sdk.infrastructure.bindings import (  # noqa: PLC0415 — circular: infrastructure imports execution transitively
        BindingError,
    )

    try:
        await infra.event_binding.invoke(
            operation="create",
            data=payload,
            metadata=binding_metadata,
        )
    except BindingError as binding_error:
        from application_sdk.constants import (  # noqa: PLC0415 — read at call time so the switch is env-fresh/patchable
            EVENT_INGRESS_DIRECT_FALLBACK,
        )

        if not EVENT_INGRESS_DIRECT_FALLBACK:
            raise
        url = _resolve_event_ingress_url()
        if url is None:
            logger.error(
                "Dapr eventstore binding failed for event %s and no Event Ingress URL "
                "could be resolved for the direct HTTP fallback (no eventstore component "
                "on disk and ATLAN_BASE_URL unset); event not published. dapr_error=%s",
                event.event_name,
                binding_error,
            )
            raise
        logger.warning(
            "FALLBACK ACTIVE: Dapr eventstore binding failed for event %s; publishing "
            "directly to Event Ingress over HTTPS from this process instead. url=%s "
            "dapr_error=%s",
            event.event_name,
            url,
            binding_error,
        )
        try:
            await _publish_event_direct(url, payload, binding_metadata)
        # conformance: ignore[E004] both channels failed; the original BindingError is re-raised so callers' handling is unchanged, and the fallback failure is logged with its own cause
        except Exception as direct_error:
            logger.error(
                "FALLBACK FAILED: direct HTTPS publish of event %s to %s also failed; "
                "event not published. direct_error=%s: %s dapr_error=%s",
                event.event_name,
                url,
                type(direct_error).__name__,
                direct_error,
                binding_error,
            )
            raise binding_error from direct_error
        logger.warning(
            "Published event via direct HTTPS fallback (Dapr eventstore binding "
            "unavailable): name=%s type=%s topic=%s url=%s",
            event.event_name,
            event.event_type,
            event.get_topic_name(),
            url,
        )
        return

    logger.info(
        "Published event via binding: name=%s type=%s topic=%s",
        event.event_name,
        event.event_type,
        event.get_topic_name(),
    )


def _resolve_event_ingress_url() -> str | None:
    """Return the Event Ingress URL the ``eventstore`` binding posts to.

    Prefers the ``url`` metadata of the ``eventstore`` Dapr component on disk —
    the same file daprd loaded, so the fallback hits exactly the endpoint the
    binding would have. Looks in ``DAPR_COMPONENTS_PATH`` (the entrypoint sets
    it; ``/app/components`` in the shipped image) and then ``./components``.
    Falls back to ``ATLAN_BASE_URL`` + ``/api/eventingress/``, which is how the
    Helm charts render that component. ``None`` when neither is available.
    """
    import os  # noqa: PLC0415 — cold path: only after a binding failure
    from pathlib import Path  # noqa: PLC0415 — cold path

    from application_sdk.storage.binding import (  # noqa: PLC0415 — circular: storage imports infrastructure
        _find_component,
    )

    candidates = [
        os.environ.get("DAPR_COMPONENTS_PATH", "/app/components"),
        "./components",
    ]
    for components_dir in candidates:
        try:
            if not Path(components_dir).is_dir():
                continue
            component = _find_component("eventstore", components_dir)
        # conformance: ignore[E004] an unreadable components dir must not mask the fallback; the next candidate or ATLAN_BASE_URL is tried
        except Exception:
            logger.debug(
                "Could not read Dapr components from %s", components_dir, exc_info=True
            )
            continue
        if not component:
            continue
        for item in component.get("spec", {}).get("metadata", []) or []:
            if item.get("name") == "url" and item.get("value"):
                return str(item["value"])

    from application_sdk.constants import ATLAN_BASE_URL  # noqa: PLC0415 — cold path

    if ATLAN_BASE_URL:
        return ATLAN_BASE_URL.rstrip("/") + "/api/eventingress/"
    return None


#: Bound on the direct publish. Generous on purpose: ``worker_start`` is sent
#: once per boot and never retried, so completing it matters more than latency.
_DIRECT_PUBLISH_TIMEOUT_SECONDS = 60.0


async def _publish_event_direct(
    url: str, payload: bytes, headers: dict[str, str]
) -> None:
    """POST *payload* to Event Ingress with *headers*, as the Dapr binding would.

    Uses the SDK's shared SSL context so a mounted custom CA
    (``SSL_CERT_DIR``) is honoured exactly as it is for every other outbound
    call this process makes. Raises on transport errors and non-2xx responses.
    """
    import httpx  # noqa: PLC0415 — cold path: only after a binding failure

    from application_sdk.clients.ssl_utils import (  # noqa: PLC0415 — cold path
        get_ssl_context,
    )

    async with httpx.AsyncClient(
        verify=get_ssl_context(), timeout=_DIRECT_PUBLISH_TIMEOUT_SECONDS
    ) as client:
        response = await client.post(url, content=payload, headers=headers)
        response.raise_for_status()


# Activity for publishing events (runs outside sandbox)
@activity.defn
async def publish_event(event_data: dict) -> None:
    """Activity to publish events outside the workflow sandbox.

    Args:
        event_data (dict): Event data to publish containing event_type, event_name,
                          metadata, and data fields.
    """
    try:
        event = Event(**event_data)
        await _publish_event_via_binding(event)
        logger.info("Published event: %s", event_data.get("event_name", ""))
    # conformance: ignore[E004] re-raises as typed EventPublishError; exception is propagated to caller
    except Exception as e:
        from application_sdk.execution._temporal._activity_errors import (  # noqa: PLC0415
            EventPublishError,
        )

        raise EventPublishError(cause=e) from e


class EventActivityInboundInterceptor(ActivityInboundInterceptor):
    """Interceptor for tracking activity execution events.

    This interceptor captures the start and end of activity executions,
    creating events that can be used for monitoring and tracking.
    Activities run outside the sandbox so they can directly publish events.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute an activity with event tracking.

        Args:
            input (ExecuteActivityInput): The activity execution input.

        Returns:
            Any: The result of the activity execution.
        """
        import time  # noqa: PLC0415 — cold path: only on event emit

        start_event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.ACTIVITY_START.value,
            data={},
        )
        try:
            await _publish_event_via_binding(start_event)
        except Exception:
            logger.warning("Failed to publish activity start event", exc_info=True)

        start_time = time.time()
        output = None
        try:
            output = await super().execute_activity(input)
        finally:
            duration_ms = (time.time() - start_time) * 1000
            end_event = Event(
                event_type=EventTypes.APPLICATION_EVENT.value,
                event_name=ApplicationEventNames.ACTIVITY_END.value,
                data={"duration_ms": round(duration_ms, 2)},
            )
            try:
                await _publish_event_via_binding(end_event)
            except Exception:
                logger.warning("Failed to publish activity end event", exc_info=True)

        return output


class EventWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Interceptor for tracking workflow execution events.

    This interceptor captures the start and end of workflow executions,
    creating events that can be used for monitoring and tracking.
    Uses activities to publish events to avoid sandbox restrictions.
    """

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute a workflow with event tracking.

        Args:
            input (ExecuteWorkflowInput): The workflow execution input.

        Returns:
            Any: The result of the workflow execution.
        """
        # Record start time (use workflow.time() for deterministic time in workflows)
        start_time = workflow.time()

        # Publish workflow start event via activity
        try:
            await workflow.execute_activity(
                publish_event,
                {
                    "metadata": EventMetadata(
                        workflow_state=WorkflowStates.RUNNING.value
                    ).model_dump(),
                    "event_type": EventTypes.APPLICATION_EVENT.value,
                    "event_name": ApplicationEventNames.WORKFLOW_START.value,
                    "data": {},
                },
                schedule_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except Exception:
            logger.warning("Failed to publish workflow start event", exc_info=True)
            # Don't fail the workflow if event publishing fails

        output = None
        workflow_state = WorkflowStates.FAILED.value  # Default to failed

        try:
            output = await super().execute_workflow(input)
            workflow_state = (
                WorkflowStates.COMPLETED.value
            )  # Update to completed on success
        # conformance: ignore[E004] captures failure state then re-raises; exception propagates to Temporal runtime
        except Exception:
            workflow_state = WorkflowStates.FAILED.value  # Keep as failed
            raise
        finally:
            # Calculate duration in milliseconds
            duration_ms = (workflow.time() - start_time) * 1000

            # Always publish workflow end event with duration
            try:
                await workflow.execute_activity(
                    publish_event,
                    {
                        "metadata": EventMetadata(
                            workflow_state=workflow_state
                        ).model_dump(),
                        "event_type": EventTypes.APPLICATION_EVENT.value,
                        "event_name": ApplicationEventNames.WORKFLOW_END.value,
                        "data": {"duration_ms": round(duration_ms, 2)},
                    },
                    schedule_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except Exception:
                logger.warning("Failed to publish workflow end event", exc_info=True)

        return output


class EventInterceptor(Interceptor):
    """Temporal interceptor for event tracking.

    This interceptor provides event tracking capabilities for both
    workflow and activity executions.
    """

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        """Intercept activity executions.

        Args:
            next (ActivityInboundInterceptor): The next interceptor in the chain.

        Returns:
            ActivityInboundInterceptor: The activity interceptor.
        """
        return EventActivityInboundInterceptor(super().intercept_activity(next))

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> type[WorkflowInboundInterceptor] | None:
        """Get the workflow interceptor class.

        Args:
            input (WorkflowInterceptorClassInput): The interceptor input.

        Returns:
            Optional[Type[WorkflowInboundInterceptor]]: The workflow interceptor class.
        """
        return EventWorkflowInboundInterceptor
