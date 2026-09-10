"""Event tracking interceptors for Temporal workflows and activities.

Publishes lifecycle events (workflow/activity start/end, worker start) via
the v3 infrastructure event binding. Falls back silently when no event
binding is configured.

When the binding *is* configured but the Dapr call fails in a way that
proves the event never got a response (connection/TLS/DNS failure, missing
binding), the event is re-sent directly to Event Ingress over HTTPS from this
process (see :func:`_publish_event_direct`), with a WARNING that names the
fallback. Errors that may already have been delivered are not re-sent. The
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


async def _publish_event_via_binding(
    event: Event,
    *,
    direct_publish_timeout: float | None = None,
) -> None:
    """Publish an event using the v3 infrastructure event binding.

    Silently skips if no event binding is configured. Enriches event
    metadata and sends Segment metrics as a side-channel.

    If the binding call fails in a way that proves the event never reached
    Event Ingress, re-sends it directly over HTTPS (see
    :func:`_publish_event_direct`). ``direct_publish_timeout`` bounds that
    direct call; the default (:data:`_DIRECT_PUBLISH_TIMEOUT_SECONDS`) is
    sized for the call sites that run inline in the activity path and inside
    the 30 s ``publish_event`` activity. ``worker_start`` — once per boot,
    never retried, awaited directly — passes a larger budget.
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

    binding_error: BindingError | None = None
    try:
        await infra.event_binding.invoke(
            operation="create",
            data=payload,
            metadata=binding_metadata,
        )
    except BindingError as e:
        binding_error = e

    if binding_error is None:
        logger.info(
            "Published event via binding: name=%s type=%s topic=%s",
            event.event_name,
            event.event_type,
            event.get_topic_name(),
        )
        return

    # ---- direct-HTTPS fallback (decided outside the except block so the
    # success line below carries no traceback) ----
    from application_sdk.constants import (  # noqa: PLC0415 — deferred so the switch stays patchable in tests
        EVENT_INGRESS_DIRECT_FALLBACK,
    )

    if not EVENT_INGRESS_DIRECT_FALLBACK:
        raise binding_error

    if not _proves_non_delivery(binding_error):
        logger.warning(
            "FALLBACK SKIPPED: Dapr eventstore binding failed for event %s but the error "
            "does not prove the event never reached Event Ingress, so it is not re-sent "
            "(re-sending could double-deliver). dapr_error=%s",
            event.event_name,
            binding_error,
            exc_info=binding_error,
        )
        raise binding_error

    remaining = _direct_publish_suspended_for()
    if remaining > 0:
        logger.info(
            "FALLBACK SUSPENDED: direct HTTPS publish failed %.0fs ago; not retrying it "
            "for event %s (%.0fs remaining). dapr_error=%s",
            _DIRECT_PUBLISH_BACKOFF_SECONDS - remaining,
            event.event_name,
            remaining,
            binding_error,
        )
        raise binding_error

    url = _resolve_event_ingress_url()
    if url is None:
        _suspend_direct_publish()
        logger.error(
            "FALLBACK UNAVAILABLE: Dapr eventstore binding failed for event %s and the "
            "'%s' component on disk is not an HTTP binding with a url (or is unreadable); "
            "event not published. Direct publish is suspended for %.0fs. dapr_error=%s",
            event.event_name,
            _event_store_name(),
            _DIRECT_PUBLISH_BACKOFF_SECONDS,
            binding_error,
            exc_info=binding_error,
        )
        raise binding_error

    logger.warning(
        "FALLBACK ACTIVE: Dapr eventstore binding failed for event %s; publishing "
        "directly to Event Ingress over HTTPS from this process instead. url=%s "
        "timeout=%.0fs dapr_error=%s",
        event.event_name,
        url,
        direct_publish_timeout or _DIRECT_PUBLISH_TIMEOUT_SECONDS,
        binding_error,
        exc_info=binding_error,
    )
    try:
        await _publish_event_direct(
            url,
            payload,
            binding_metadata,
            timeout=direct_publish_timeout or _DIRECT_PUBLISH_TIMEOUT_SECONDS,
        )
    # conformance: ignore[E004] both channels failed; the original BindingError is re-raised unchanged so callers' handling and its Dapr cause chain are preserved, and the direct failure is logged with its own traceback here
    except Exception as direct_error:
        _suspend_direct_publish()
        logger.exception(
            "FALLBACK FAILED: direct HTTPS publish of event %s to %s also failed; event "
            "not published. Direct publish is suspended for %.0fs. dapr_error=%s",
            event.event_name,
            url,
            _DIRECT_PUBLISH_BACKOFF_SECONDS,
            binding_error,
        )
        binding_error.add_note(
            f"direct HTTPS fallback to {url} also failed: "
            f"{type(direct_error).__name__}: {direct_error}"
        )
        raise binding_error

    logger.warning(
        "Published event via direct HTTPS fallback (Dapr eventstore binding "
        "unavailable): name=%s type=%s topic=%s url=%s",
        event.event_name,
        event.event_type,
        event.get_topic_name(),
        url,
    )


# ---------------------------------------------------------------------------
# Direct-HTTPS fallback machinery
# ---------------------------------------------------------------------------

#: Bound on a direct publish from the interceptor / token-refresh call sites.
#: Must stay well inside every caller's budget: the ``publish_event`` activity
#: is dispatched with ``schedule_to_close_timeout=30s``, and activity_start /
#: activity_end publish inline before heartbeating begins (default heartbeat
#: timeout 60 s). ``worker_start`` overrides this via ``direct_publish_timeout``.
_DIRECT_PUBLISH_TIMEOUT_SECONDS = 5.0

#: Budget for the once-per-boot, never-retried ``worker_start`` publish, which
#: is awaited directly by the worker (no activity or heartbeat budget above it).
WORKER_START_DIRECT_PUBLISH_TIMEOUT_SECONDS = 20.0

#: After a failed direct publish, skip the fallback for this long so a dead
#: Event Ingress costs one timeout, not one per event.
_DIRECT_PUBLISH_BACKOFF_SECONDS = 60.0

#: Substrings of a Dapr ``bindings.http`` error that prove the request never
#: produced a response from the far side: the TCP connection failed, the TLS
#: handshake was closed, DNS failed, or the sidecar never had the binding.
#: Anything else (an HTTP status from upstream, a read timeout after the
#: request was sent) may already have been delivered and is NOT re-sent.
_NON_DELIVERY_MARKERS = (
    "eof",
    "connection reset",
    "connection refused",
    "dial tcp",
    "no such host",
    "tls:",
    "x509:",
    "couldn't find output binding",
    "sidecar unreachable",
    "err_invoke_output_binding_not_found",
)

_resolved_ingress_url: str | None = None
_ingress_url_resolved: bool = False
_direct_publish_suspended_until: float = 0.0


def _proves_non_delivery(binding_error: BaseException) -> bool:
    """True when the binding error text shows the request got no answer at all.

    An upstream HTTP status (``received status code 5xx``) means Event Ingress
    or the gateway answered — the event may have been accepted — so re-sending
    could double-deliver ``worker_start`` or a workflow event. Only errors that
    rule out any response qualify for the fallback.
    """
    text = str(binding_error).lower()
    if "received status code" in text:
        return False
    return any(marker in text for marker in _NON_DELIVERY_MARKERS)


def _event_store_name() -> str:
    from application_sdk.constants import (  # noqa: PLC0415 — deferred: keep patchable
        EVENT_STORE_NAME,
    )

    return EVENT_STORE_NAME


def _direct_publish_suspended_for() -> float:
    import time  # noqa: PLC0415 — cold path

    return max(0.0, _direct_publish_suspended_until - time.monotonic())


def _suspend_direct_publish() -> None:
    global _direct_publish_suspended_until
    import time  # noqa: PLC0415 — cold path

    _direct_publish_suspended_until = time.monotonic() + _DIRECT_PUBLISH_BACKOFF_SECONDS


def _reset_direct_publish_state() -> None:
    """Test hook: forget the cached URL and any suspension."""
    global _resolved_ingress_url, _ingress_url_resolved, _direct_publish_suspended_until
    _resolved_ingress_url = None
    _ingress_url_resolved = False
    _direct_publish_suspended_until = 0.0


def _read_http_binding_url(components_dir: str, name: str) -> str | None:
    """Return the ``url`` of the ``bindings.http`` Component *name* in *components_dir*.

    Reads every ``*.yaml`` file with a per-file guard, so one malformed or
    unrendered sibling cannot hide a valid eventstore component. Returns
    ``None`` when the component is absent, is not ``bindings.http`` (e.g. the
    SDK's local-dev ``bindings.localstorage`` eventstore), or has no ``url``.
    """
    from pathlib import Path  # noqa: PLC0415 — cold path

    import yaml  # noqa: PLC0415 — cold path

    directory = Path(components_dir)
    if not directory.is_dir():
        return None
    for yaml_file in sorted(directory.glob("*.yaml")):
        try:
            with yaml_file.open(encoding="utf-8") as fh:
                doc = yaml.safe_load(fh)
        # conformance: ignore[E004] a sibling component that fails to parse must not hide the eventstore component; skip it and keep looking
        except Exception:
            logger.debug(
                "Skipping unreadable Dapr component file %s", yaml_file, exc_info=True
            )
            continue
        metadata = doc.get("metadata") if isinstance(doc, dict) else None
        if not (
            isinstance(doc, dict)
            and doc.get("kind") == "Component"
            and isinstance(metadata, dict)
            and metadata.get("name") == name
        ):
            continue
        spec = doc.get("spec") or {}
        if spec.get("type") != "bindings.http":
            logger.debug(
                "Dapr component %s is %s, not bindings.http; no direct HTTPS fallback",
                name,
                spec.get("type"),
            )
            return None
        for item in spec.get("metadata") or []:
            if (
                isinstance(item, dict)
                and item.get("name") == "url"
                and item.get("value")
            ):
                return str(item["value"])
        return None
    return None


def _resolve_event_ingress_url() -> str | None:
    """Return the URL the ``EVENT_STORE_NAME`` binding posts to, resolved once.

    Taken only from the ``bindings.http`` component on disk — the same file
    daprd loaded — so the fallback hits exactly the endpoint the binding would
    have and nothing else. Looks in ``DAPR_COMPONENTS_PATH`` (the entrypoint
    sets it; ``/app/components`` in the shipped image), then ``./components``.
    There is deliberately no guess from ``ATLAN_BASE_URL``: a component that is
    not HTTP, or that cannot be read, means no fallback. A positive result is
    cached for the life of the process; a miss is re-tried after the backoff.
    """
    global _resolved_ingress_url, _ingress_url_resolved
    if _ingress_url_resolved and _resolved_ingress_url is not None:
        return _resolved_ingress_url

    import os  # noqa: PLC0415 — cold path

    name = _event_store_name()
    for components_dir in (
        os.environ.get("DAPR_COMPONENTS_PATH", "/app/components"),
        "./components",
    ):
        url = _read_http_binding_url(components_dir, name)
        if url:
            _resolved_ingress_url = url
            _ingress_url_resolved = True
            return url
    _ingress_url_resolved = True
    _resolved_ingress_url = None
    return None


async def _publish_event_direct(
    url: str, payload: bytes, headers: dict[str, str], *, timeout: float
) -> None:
    """POST *payload* to Event Ingress with *headers*, as the Dapr binding would.

    Uses the SDK's shared SSL context so a mounted custom CA (``SSL_CERT_DIR``)
    is honoured exactly as it is for every other outbound call this process
    makes. Raises on transport errors and non-2xx responses.
    """
    import httpx  # noqa: PLC0415 — cold path: only after a binding failure

    from application_sdk.clients.ssl_utils import (  # noqa: PLC0415 — cold path
        get_ssl_context,
    )

    async with httpx.AsyncClient(verify=get_ssl_context(), timeout=timeout) as client:
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
