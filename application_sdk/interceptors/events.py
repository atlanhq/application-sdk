from typing import Any, Optional, Type

from temporalio import workflow
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.outputs.eventstore import (
    ApplicationEventNames,
    Event,
    EventMetadata,
    EventStore,
    EventTypes,
    WorkflowStates,
)

logger = get_logger(__name__)

TEMPORAL_NOT_FOUND_FAILURE = (
    "type.googleapis.com/temporal.api.errordetails.v1.NotFoundFailure"
)




class EventActivityInboundInterceptor(ActivityInboundInterceptor):
    """Interceptor for tracking activity execution events.

    This interceptor captures the start and end of activity executions,
    creating events that can be used for monitoring and tracking.
    """

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        """Execute an activity with event tracking.

        Args:
            input (ExecuteActivityInput): The activity execution input.

        Returns:
            Any: The result of the activity execution.
        """
        event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.ACTIVITY_START.value,
            data={},
        )

        EventStore.publish_event(event)

        output = None
        try:
            output = await super().execute_activity(input)
        except Exception as e:
            end_event = Event(
                event_type=EventTypes.APPLICATION_EVENT.value,
                event_name=ApplicationEventNames.ACTIVITY_END.value,
                data={},
            )
            EventStore.publish_event(end_event)
            raise e

        end_event = Event(
            event_type=EventTypes.APPLICATION_EVENT.value,
            event_name=ApplicationEventNames.ACTIVITY_END.value,
            data={},
        )
        EventStore.publish_event(end_event)
        return output


class EventWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Interceptor for tracking workflow execution events.

    This interceptor captures the start and end of workflow executions,
    creating events that can be used for monitoring and tracking.
    """

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        """Execute a workflow with event tracking.

        Args:
            input (ExecuteWorkflowInput): The workflow execution input.

        Returns:
            Any: The result of the workflow execution.
        """
        with workflow.unsafe.sandbox_unrestricted():
            EventStore.publish_event(
                Event(
                    metadata=EventMetadata(workflow_state=WorkflowStates.RUNNING.value),
                    event_type=EventTypes.APPLICATION_EVENT.value,
                    event_name=ApplicationEventNames.WORKFLOW_START.value,
                    data={},
                )
            )
        output = None
        try:
            output = await super().execute_workflow(input)
        except Exception as e:
            with workflow.unsafe.sandbox_unrestricted():
                EventStore.publish_event(
                    Event(
                        metadata=EventMetadata(
                            workflow_state=WorkflowStates.FAILED.value
                        ),
                        event_type=EventTypes.APPLICATION_EVENT.value,
                        event_name=ApplicationEventNames.WORKFLOW_END.value,
                        data={},
                    ),
                )
            raise e

        with workflow.unsafe.sandbox_unrestricted():
            EventStore.publish_event(
                Event(
                    metadata=EventMetadata(
                        workflow_state=WorkflowStates.COMPLETED.value
                    ),
                    event_type=EventTypes.APPLICATION_EVENT.value,
                    event_name=ApplicationEventNames.WORKFLOW_END.value,
                    data={
                        "workflow_id": workflow.info().workflow_id,
                        "workflow_run_id": workflow.info().run_id,
                    },
                ),
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
