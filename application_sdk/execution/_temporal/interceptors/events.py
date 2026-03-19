"""Event tracking interceptors for Temporal workflows and activities.

Publishes lifecycle events (workflow/activity start/end, worker start) via
the v3 infrastructure event binding. Falls back silently when no event
binding is configured.
"""

import json
from datetime import datetime, timedelta
from typing import Any, Optional, Type

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
    from application_sdk.constants import APPLICATION_NAME

    if not event.metadata:
        event.metadata = EventMetadata()

    event.metadata.application_name = APPLICATION_NAME
    event.metadata.created_timestamp = int(datetime.now().timestamp())
    event.metadata.topic_name = event.get_topic_name()

    try:
        workflow_info = workflow.info()
        if workflow_info:
            event.metadata.workflow_type = workflow_info.workflow_type
            event.metadata.workflow_id = workflow_info.workflow_id
            event.metadata.workflow_run_id = workflow_info.run_id
    except Exception:
        pass

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
    except Exception:
        pass

    return event


def _send_lifecycle_event_to_segment(event: Event) -> None:
    """Send lifecycle event to Segment (best-effort side-channel).

    Mirrors the logic from the v2 EventStore._send_lifecycle_event_to_segment.
    Never raises — failures are logged at DEBUG level.
    """
    if event.event_name not in LIFECYCLE_EVENTS:
        return

    try:
        import time

        from application_sdk.constants import APP_TENANT_ID, ATLAN_BASE_URL
        from application_sdk.observability.metrics_adaptor import (
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
    except Exception as e:
        logger.debug(f"Failed to send lifecycle event to Segment: {e}")


async def _publish_event_via_binding(event: Event) -> None:
    """Publish an event using the v3 infrastructure event binding.

    Silently skips if no event binding is configured. Enriches event
    metadata and sends Segment metrics as a side-channel.
    """
    from application_sdk.infrastructure.context import get_infrastructure

    infra = get_infrastructure()
    if infra is None or infra.event_binding is None:
        return

    event = _enrich_event_metadata(event)
    _send_lifecycle_event_to_segment(event)

    payload = json.dumps(event.model_dump(mode="json")).encode()
    binding_metadata: dict[str, str] = {"content-type": "application/json"}

    try:
        from application_sdk.clients.atlan_auth import AtlanAuthClient

        auth_client = AtlanAuthClient()
        binding_metadata.update(await auth_client.get_authenticated_headers())
    except Exception:
        pass  # Degrade gracefully without auth headers

    await infra.event_binding.invoke(
        operation="create",
        data=payload,
        metadata=binding_metadata,
    )
    logger.info(f"Published event via binding on topic: {event.get_topic_name()}")


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
        logger.info(f"Published event: {event_data.get('event_name', '')}")
    except Exception as e:
        logger.error(f"Failed to publish event: {e}")
        raise


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
        import time

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
                    ),
                    "event_type": EventTypes.APPLICATION_EVENT.value,
                    "event_name": ApplicationEventNames.WORKFLOW_START.value,
                    "data": {},
                },
                schedule_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=3),
            )
        except Exception as e:
            logger.warning(f"Failed to publish workflow start event: {e}")
            # Don't fail the workflow if event publishing fails

        output = None
        workflow_state = WorkflowStates.FAILED.value  # Default to failed

        try:
            output = await super().execute_workflow(input)
            workflow_state = (
                WorkflowStates.COMPLETED.value
            )  # Update to completed on success
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
                        "metadata": EventMetadata(workflow_state=workflow_state),
                        "event_type": EventTypes.APPLICATION_EVENT.value,
                        "event_name": ApplicationEventNames.WORKFLOW_END.value,
                        "data": {"duration_ms": round(duration_ms, 2)},
                    },
                    schedule_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except Exception as publish_error:
                logger.warning(f"Failed to publish workflow end event: {publish_error}")

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
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class.

        Args:
            input (WorkflowInterceptorClassInput): The interceptor input.

        Returns:
            Optional[Type[WorkflowInboundInterceptor]]: The workflow interceptor class.
        """
        return EventWorkflowInboundInterceptor
