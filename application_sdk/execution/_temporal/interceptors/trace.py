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

from typing import Any

from temporalio.worker import (
    ActivityInboundInterceptor,
    Interceptor,
    WorkflowInboundInterceptor,
    WorkflowInterceptorClassInput,
)

from application_sdk import constants
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class TraceInterceptor(Interceptor):
    """OpenTelemetry tracing interceptor for Temporal.

    Delegates to ``temporalio.contrib.opentelemetry.TracingInterceptor`` when
    ``ATLAN_ENABLE_OTLP_TRACES=true``; otherwise behaves as an identity
    interceptor.

    When enabled it also makes sure the process-wide tracer provider is
    installed (``setup_tracer_provider`` is idempotent; ``run_main`` normally
    did it already). The upstream interceptor emits through the global
    provider, so a worker built with ``create_worker`` outside ``run_main``
    would otherwise export nothing.
    """

    def __init__(self) -> None:
        self._delegate: Any = None
        if constants.ENABLE_OTLP_TRACES:
            from application_sdk.observability.traces_adaptor import (  # noqa: PLC0415 — only loaded when tracing is on; keeps the OTLP exporter off the default import path
                setup_tracer_provider,
            )

            setup_tracer_provider()
            try:
                from temporalio.contrib.opentelemetry import (  # noqa: PLC0415 — optional dependency soft-import; ImportError handled below
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
                    "tracing disabled",
                    exc_info=True,
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
