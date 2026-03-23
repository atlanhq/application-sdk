"""Temporal interceptor for correlation context propagation.

- Workflow inbound: seeds a CorrelationContext from the workflow memo
  (continue-as-new restoration) or generates a new one.
- Workflow outbound: injects ``x-correlation-id`` into Temporal headers on
  both ``start_activity`` and ``start_child_workflow`` calls.
- Activity inbound: reads the header and sets the CorrelationContext ContextVar.

Registration order: this interceptor must come BEFORE EventInterceptor
so the ContextVar is set before EventInterceptor reads it.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from temporalio import workflow
from temporalio.converter import default as default_converter
from temporalio.worker import (
    ActivityInboundInterceptor,
    ExecuteActivityInput,
    ExecuteWorkflowInput,
    Interceptor,
    StartActivityInput,
    StartChildWorkflowInput,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
    WorkflowOutboundInterceptor,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from temporalio.api.common.v1 import Payload

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

_HEADER_CORRELATION_ID = "x-correlation-id"


class _LazyCorrelationOutboundInterceptor(WorkflowOutboundInterceptor):
    """Outbound interceptor that reads correlation_id lazily from the inbound interceptor."""

    def __init__(
        self,
        next: WorkflowOutboundInterceptor,
        inbound: _CorrelationWorkflowInboundInterceptor,
    ) -> None:
        super().__init__(next)
        self._inbound = inbound

    def _inject(self, headers: Mapping[str, Payload]) -> dict[str, Payload]:
        correlation_id = self._inbound._correlation_id
        if not correlation_id:
            return dict(headers)
        try:
            converter = default_converter().payload_converter
            new_headers: dict[str, Payload] = dict(headers)
            new_headers[_HEADER_CORRELATION_ID] = converter.to_payload(correlation_id)
            return new_headers
        except Exception:
            logger.warning("Failed to inject correlation header", exc_info=True)
            return dict(headers)

    def start_activity(self, input: StartActivityInput) -> Any:
        return self.next.start_activity(
            dataclasses.replace(input, headers=self._inject(input.headers))
        )

    def start_child_workflow(self, input: StartChildWorkflowInput) -> Any:
        return self.next.start_child_workflow(
            dataclasses.replace(input, headers=self._inject(input.headers))
        )


class _CorrelationWorkflowInboundInterceptor(WorkflowInboundInterceptor):
    """Seeds CorrelationContext from memo (continue-as-new) or generates a new one."""

    def __init__(self, next: WorkflowInboundInterceptor) -> None:
        super().__init__(next)
        self._correlation_id: str = ""

    def init(self, outbound: WorkflowOutboundInterceptor) -> None:
        super().init(_LazyCorrelationOutboundInterceptor(outbound, self))

    async def execute_workflow(self, input: ExecuteWorkflowInput) -> Any:
        from application_sdk.observability.correlation import (
            CorrelationContext,
            set_correlation_context,
        )

        # Priority 1: restore from memo (continue-as-new path)
        try:
            memo = dict(workflow.memo())
            ctx = CorrelationContext.from_temporal_memo(memo)
        except Exception:
            logger.warning(
                "Failed to restore correlation context from memo", exc_info=True
            )
            ctx = None

        # Priority 2: inherit from parent via headers (child workflow call path)
        if ctx is None:
            try:
                payload = input.headers.get(_HEADER_CORRELATION_ID)
                if payload is not None:
                    converter = default_converter().payload_converter
                    correlation_id: str = converter.from_payload(payload, type_hint=str)
                    if correlation_id:
                        ctx = CorrelationContext(correlation_id=correlation_id)
            except Exception:
                logger.warning(
                    "Failed to read correlation header in workflow", exc_info=True
                )

        # Priority 3: top-level workflow — generate a fresh correlation ID
        if ctx is None:
            ctx = CorrelationContext(correlation_id=str(uuid4()))

        self._correlation_id = ctx.correlation_id
        set_correlation_context(ctx)

        return await self.next.execute_workflow(input)


class _CorrelationActivityInboundInterceptor(ActivityInboundInterceptor):
    """Reads the x-correlation-id header and sets the CorrelationContext ContextVar."""

    async def execute_activity(self, input: ExecuteActivityInput) -> Any:
        from application_sdk.observability.correlation import (
            CorrelationContext,
            set_correlation_context,
        )

        try:
            payload = input.headers.get(_HEADER_CORRELATION_ID)
            if payload is not None:
                converter = default_converter().payload_converter
                correlation_id: str = converter.from_payload(payload, type_hint=str)
                if correlation_id:
                    set_correlation_context(
                        CorrelationContext(correlation_id=correlation_id)
                    )
        except Exception:
            logger.warning(
                "Failed to read correlation header in activity", exc_info=True
            )

        return await self.next.execute_activity(input)


class CorrelationContextInterceptor(Interceptor):
    """Temporal interceptor that propagates correlation context via headers.

    Must be registered BEFORE EventInterceptor so the ContextVar is set
    before lifecycle events read the correlation_id.
    """

    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,  # noqa: ARG002
    ) -> type[WorkflowInboundInterceptor] | None:
        return _CorrelationWorkflowInboundInterceptor

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        return _CorrelationActivityInboundInterceptor(next)
