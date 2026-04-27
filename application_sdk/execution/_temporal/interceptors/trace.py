"""Unified Trace interceptor.

Thin wrapper around :class:`temporalio.contrib.opentelemetry.TracingInterceptor`
gated on ``ATLAN_ENABLE_OTLP_TRACES``. When the flag is off (production
default — traces are not yet supported in production), all hooks are no-ops
and no spans are created.

Wrapping the upstream interceptor instead of registering it directly keeps
the public worker stack uniform: ``LogInterceptor``, ``MetricsInterceptor``,
``TraceInterceptor``.
"""

from __future__ import annotations

import os
from typing import Any

from temporalio.worker import (
    ActivityInboundInterceptor,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def _traces_enabled() -> bool:
    return os.getenv("ATLAN_ENABLE_OTLP_TRACES", "false").lower() == "true"


class TraceInterceptor(Interceptor):
    """OpenTelemetry tracing interceptor for Temporal.

    Delegates to ``temporalio.contrib.opentelemetry.TracingInterceptor`` when
    ``ATLAN_ENABLE_OTLP_TRACES=true``; otherwise behaves as an identity
    interceptor.
    """

    def __init__(self) -> None:
        self._delegate: Any = None
        if _traces_enabled():
            try:
                from temporalio.contrib.opentelemetry import (
                    TracingInterceptor as _TracingInterceptor,
                )

                self._delegate = _TracingInterceptor()
                logger.info(
                    "Temporal OTel TracingInterceptor enabled — "
                    "ATLAN_ENABLE_OTLP_TRACES=true"
                )
            except ImportError:
                logger.warning(
                    "ATLAN_ENABLE_OTLP_TRACES=true but "
                    "temporalio.contrib.opentelemetry is not installed; "
                    "tracing disabled"
                )

    def workflow_interceptor_class(
        self,
        input: WorkflowInterceptorClassInput,
    ) -> type[WorkflowInboundInterceptor] | None:
        if self._delegate is None:
            return None
        return self._delegate.workflow_interceptor_class(input)

    def intercept_activity(
        self, next: ActivityInboundInterceptor
    ) -> ActivityInboundInterceptor:
        if self._delegate is None:
            return next
        return self._delegate.intercept_activity(next)
