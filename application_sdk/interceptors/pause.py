"""Pause interceptor for Temporal workflows.

Automatically checks pause state before every activity execution, allowing
workflows to be paused and resumed via signals without manual checkpoints.
"""

from typing import Any, Optional, Type

from temporalio import workflow
from temporalio.worker import (
    HandleSignalInput,
    Interceptor,
    StartActivityInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

from application_sdk.constants import PAUSE_SIGNAL, RESUME_SIGNAL
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class PauseOutboundInterceptor(WorkflowOutboundInterceptor):
    """Outbound interceptor that blocks activity execution while workflow is paused."""

    def __init__(
        self,
        next: WorkflowOutboundInterceptor,
        inbound: "PauseWorkflowInboundInterceptor",
    ):
        """Initialize the outbound interceptor.

        Args:
            next: The next interceptor in the chain.
            inbound: The inbound interceptor holding pause state.
        """
        super().__init__(next)
        self.inbound = inbound

    async def start_activity(  # type: ignore[override]
        self, input: StartActivityInput
    ) -> workflow.ActivityHandle[Any]:
        """Start activity, blocking if workflow is paused.

        Args:
            input: The activity start input.

        Returns:
            The activity handle.
        """
        if self.inbound._paused:
            logger.info("Workflow is paused, waiting for resume signal before activity")
            await workflow.wait_condition(lambda: not self.inbound._paused)
            logger.info("Workflow resumed, starting activity")

        return await self.next.start_activity(input)


class PauseWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Inbound workflow interceptor that manages pause state via signal interception."""

    def __init__(self, next: WorkflowInboundInterceptor):
        """Initialize the inbound interceptor."""
        super().__init__(next)
        self._paused = False

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        """Initialize with pause outbound interceptor."""
        pause_outbound = PauseOutboundInterceptor(outbound, self)
        super().init(pause_outbound)

    async def handle_signal(self, input: HandleSignalInput) -> None:
        """Intercept pause and resume signals to manage pause state.

        Args:
            input: The signal input containing signal name and args.
        """
        if input.signal == PAUSE_SIGNAL:
            self._paused = True
            logger.info("Workflow received pause signal")
        elif input.signal == RESUME_SIGNAL:
            self._paused = False
            logger.info("Workflow received resume signal")

        await super().handle_signal(input)


class PauseInterceptor(Interceptor):
    """Temporal interceptor for automatic workflow pause/resume.

    This interceptor automatically blocks activity execution when a workflow
    is paused, removing the need for manual pause checkpoints in workflow code.
    """

    def workflow_interceptor_class(
        self, input: WorkflowInterceptorClassInput
    ) -> Optional[Type[WorkflowInboundInterceptor]]:
        """Get the workflow interceptor class.

        Args:
            input: The interceptor input.

        Returns:
            The workflow interceptor class.
        """
        return PauseWorkflowInboundInterceptor
