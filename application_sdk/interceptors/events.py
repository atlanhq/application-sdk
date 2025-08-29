from datetime import timedelta
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

from application_sdk.events.models import (
    ApplicationEventNames,
    Event,
    EventMetadata,
    EventTypes,
    WorkflowStates,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.services.eventstore import EventStore

logger = get_logger(__name__)

TEMPORAL_NOT_FOUND_FAILURE = (
    "type.googleapis.com/temporal.api.errordetails.v1.NotFoundFailure"
)


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
        await EventStore.publish_event(event)
        activity.logger.info(f"Published event: {event_data.get('event_name','')}")
    except Exception as e:
        activity.logger.error(f"Failed to publish event: {e}")
        raise


class EventActivityInboundInterceptor(ActivityInboundInterceptor):
    """Interceptor for tracking activity execution events.

    This interceptor captures the start and end of activity executions,
    creating events that can be used for monitoring and tracking.
    Activities run outside the sandbox so they can directly call EventStore.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute an activity with event tracking.

        Args:
            input (ExecuteActivityInput): The activity execution input.

        Returns:
            Any: The result of the activity execution.
        """
        # Extract activity information for tracking

        start_event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.ACTIVITY_START.value,
            data={},
        )
        await EventStore.publish_event(start_event)

        output = None
        try:
            output = await super().execute_activity(input)
        except Exception:
            raise
        finally:
            end_event = Event(
                event_type=EventTypes.APPLICATION_EVENT.value,
                event_name=ApplicationEventNames.ACTIVITY_END.value,
                data={},
            )
            await EventStore.publish_event(end_event)

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
            workflow.logger.warning(f"Failed to publish workflow start event: {e}")
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
            # Always publish workflow end event
            try:
                await workflow.execute_activity(
                    publish_event,
                    {
                        "metadata": EventMetadata(workflow_state=workflow_state),
                        "event_type": EventTypes.APPLICATION_EVENT.value,
                        "event_name": ApplicationEventNames.WORKFLOW_END.value,
                        "data": {},
                    },
                    schedule_to_close_timeout=timedelta(seconds=30),
                    retry_policy=RetryPolicy(maximum_attempts=3),
                )
            except Exception as publish_error:
                workflow.logger.warning(
                    f"Failed to publish workflow end event: {publish_error}"
                )

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
