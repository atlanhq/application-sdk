import functools
import inspect
import time
import uuid
from typing import TYPE_CHECKING, Any, Callable, TypeVar, cast

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.observability.metrics_adaptor import MetricType, get_metrics
from application_sdk.observability.traces_adaptor import get_traces

if TYPE_CHECKING:
    from application_sdk.observability.logger_adaptor import AtlanLoggerAdapter
    from application_sdk.observability.metrics_adaptor import AtlanMetricsAdapter
    from application_sdk.observability.traces_adaptor import AtlanTracesAdapter

T = TypeVar("T")


def _record_success_observability(
    logger: "AtlanLoggerAdapter",
    metrics: "AtlanMetricsAdapter",
    traces: "AtlanTracesAdapter",
    func_name: str,
    func_doc: str,
    func_module: str,
    trace_id: str,
    span_id: str,
    start_time: float,
) -> None:
    """Helper function to record success observability data."""
    duration_ms = (time.time() - start_time) * 1000

    # Debug logging before recording trace
    logger.debug(
        "Recording success trace",
        func_name=func_name,
        trace_id=trace_id,
        span_id=span_id,
    )

    try:
        # Record success trace
        traces.record_trace(
            name=func_name,
            trace_id=trace_id,
            span_id=span_id,
            kind="INTERNAL",
            status_code="OK",
            attributes={
                "function": func_name,
                "description": func_doc,
                "module": func_module,
            },
            events=[{"name": f"{func_name}_success", "timestamp": time.time()}],
            duration_ms=duration_ms,
        )
        logger.debug("Successfully recorded trace: %s", func_name)
    except Exception:
        logger.error("Failed to record trace: %s", func_name, exc_info=True)

    # Debug logging before recording metric
    logger.debug("Recording success metric: %s", func_name)

    try:
        # Record success metric
        metrics.record_metric(
            name=f"{func_name}_success",
            value=1,
            metric_type=MetricType.COUNTER,
            labels={"function": func_name},
            description=f"Successful {func_name}",
            unit="count",
        )
        logger.debug("Successfully recorded metric: %s", func_name)
    except Exception:
        logger.error("Failed to record metric: %s", func_name, exc_info=True)

    # Log completion
    logger.debug("Completed function: %s in %.2fms", func_name, round(duration_ms, 2))


def _record_error_observability(
    logger: "AtlanLoggerAdapter",
    metrics: "AtlanMetricsAdapter",
    traces: "AtlanTracesAdapter",
    func_name: str,
    func_doc: str,
    func_module: str,
    trace_id: str,
    span_id: str,
    start_time: float,
    error: Exception,
) -> None:
    """Helper function to record error observability data."""
    duration_ms = (time.time() - start_time) * 1000

    # Debug logging for error case
    logger.error("Error in function: %s", func_name, exc_info=True)

    try:
        # Record failure trace
        traces.record_trace(
            name=func_name,
            trace_id=trace_id,
            span_id=span_id,
            kind="INTERNAL",
            status_code="ERROR",
            attributes={
                "function": func_name,
                "description": func_doc,
                "module": func_module,
            },
            events=[
                {
                    "name": f"{func_name}_failure",
                    "timestamp": time.time(),
                    "attributes": {"error": str(error)},
                }
            ],
            duration_ms=duration_ms,
        )
        logger.debug("Successfully recorded error trace: %s", func_name)
    except Exception:
        logger.error("Failed to record error trace: %s", func_name, exc_info=True)

    try:
        # Record failure metric
        metrics.record_metric(
            name=f"{func_name}_failure",
            value=1,
            metric_type=MetricType.COUNTER,
            labels={"function": func_name, "error": str(error)},
            description=f"Failed {func_name}",
            unit="count",
        )
        logger.debug("Successfully recorded error metric: %s", func_name)
    except Exception:
        logger.error("Failed to record error metric: %s", func_name, exc_info=True)

    # Log error
    logger.error("Error in function: %s", func_name, exc_info=True)


def observability(
    logger: "AtlanLoggerAdapter | None" = None,
    metrics: "AtlanMetricsAdapter | None" = None,
    traces: "AtlanTracesAdapter | None" = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for adding observability to functions.

    This decorator records traces and metrics for both successful and failed function executions.
    It handles both synchronous and asynchronous functions.

    Args:
        logger: Logger instance for operation logging. If None, auto-initializes using get_logger()
        metrics: Metrics adapter for recording operation metrics. If None, auto-initializes using get_metrics()
        traces: Traces adapter for recording operation traces. If None, auto-initializes using get_traces()

    Returns:
        Callable: Decorated function with observability

    Example:
        ```python
        # With explicit observability components
        @observability(logger, metrics, traces)
        async def my_function():
            # Function implementation
            pass

        # With auto-initialization (recommended)
        @observability()
        async def my_function():
            # Function implementation
            pass
        ```
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        # Auto-initialize observability components if not provided
        actual_logger = logger or get_logger(func.__module__)
        actual_metrics = metrics or get_metrics()
        actual_traces = traces or get_traces()

        # Get function metadata
        func_name = func.__name__
        func_doc = func.__doc__ or f"Executing {func_name}"
        func_module = func.__module__
        is_async = inspect.iscoroutinefunction(func)

        # Debug logging for function decoration
        actual_logger.debug(
            "Decorating function", func_name=func_name, is_async=is_async
        )

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            # Safe: observability decorators run in activity context, not workflow sandbox
            trace_id = str(uuid.uuid4())
            span_id = str(uuid.uuid4())
            start_time = time.time()

            try:
                # Log start of operation
                actual_logger.debug("Starting async function: %s", func_name)

                # Execute the function
                result = await func(*args, **kwargs)

                # Record success observability
                _record_success_observability(
                    actual_logger,
                    actual_metrics,
                    actual_traces,
                    func_name,
                    func_doc,
                    func_module,
                    trace_id,
                    span_id,
                    start_time,
                )

                return result

            except Exception as e:
                # Record error observability
                _record_error_observability(
                    actual_logger,
                    actual_metrics,
                    actual_traces,
                    func_name,
                    func_doc,
                    func_module,
                    trace_id,
                    span_id,
                    start_time,
                    e,
                )
                raise

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            # Safe: observability decorators run in activity context, not workflow sandbox
            trace_id = str(uuid.uuid4())
            span_id = str(uuid.uuid4())
            start_time = time.time()

            try:
                # Log start of operation
                actual_logger.debug("Starting sync function: %s", func_name)

                # Execute the function
                result = func(*args, **kwargs)

                # Record success observability
                _record_success_observability(
                    actual_logger,
                    actual_metrics,
                    actual_traces,
                    func_name,
                    func_doc,
                    func_module,
                    trace_id,
                    span_id,
                    start_time,
                )

                return result

            except Exception as e:
                # Record error observability
                _record_error_observability(
                    actual_logger,
                    actual_metrics,
                    actual_traces,
                    func_name,
                    func_doc,
                    func_module,
                    trace_id,
                    span_id,
                    start_time,
                    e,
                )
                raise

        # Return appropriate wrapper based on function type
        return cast(Callable[..., T], async_wrapper if is_async else sync_wrapper)

    return decorator
