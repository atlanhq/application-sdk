import functools
import inspect
import time
import uuid
from typing import Any, Callable, TypeVar, cast

from application_sdk.observability.metrics_adaptor import MetricType

T = TypeVar("T")


def _record_success_observability(
    logger: Any,
    metrics: Any,
    traces: Any,
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
        f"Recording success trace for {func_name} with trace_id={trace_id}, span_id={span_id}"
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
        logger.debug(f"Successfully recorded trace for {func_name}")
    except Exception as trace_error:
        logger.error(f"Failed to record trace for {func_name}: {str(trace_error)}")

    # Debug logging before recording metric
    logger.debug(f"Recording success metric for {func_name}")

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
        logger.debug(f"Successfully recorded metric for {func_name}")
    except Exception as metric_error:
        logger.error(f"Failed to record metric for {func_name}: {str(metric_error)}")

    # Log completion
    logger.debug(f"Completed function {func_name} in {duration_ms:.2f}ms")


def _record_error_observability(
    logger: Any,
    metrics: Any,
    traces: Any,
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
    logger.error(f"Error in function {func_name}: {str(error)}")

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
        logger.debug(f"Successfully recorded error trace for {func_name}")
    except Exception as trace_error:
        logger.error(
            f"Failed to record error trace for {func_name}: {str(trace_error)}"
        )

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
        logger.debug(f"Successfully recorded error metric for {func_name}")
    except Exception as metric_error:
        logger.error(
            f"Failed to record error metric for {func_name}: {str(metric_error)}"
        )

    # Log error
    logger.error(f"Error in {func_name}: {str(error)}")


def observability(
    logger: Any,
    metrics: Any,
    traces: Any,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Decorator for adding observability to functions.

    This decorator records traces and metrics for both successful and failed function executions.
    It handles both synchronous and asynchronous functions.

    Args:
        logger: Logger instance for operation logging
        metrics: Metrics adapter for recording operation metrics
        traces: Traces adapter for recording operation traces

    Returns:
        Callable: Decorated function with observability

    Example:
        ```python
        @observability(logger, metrics, traces)
        async def my_function():
            # Function implementation
            pass
        ```
    """

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        # Get function metadata
        func_name = func.__name__
        func_doc = func.__doc__ or f"Executing {func_name}"
        func_module = func.__module__
        is_async = inspect.iscoroutinefunction(func)

        # Debug logging for function decoration
        logger.debug(f"Decorating function {func_name} (async={is_async})")

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            # Generate trace ID and span ID
            trace_id = str(uuid.uuid4())
            span_id = str(uuid.uuid4())
            start_time = time.time()

            try:
                # Log start of operation
                logger.debug(f"Starting async function {func_name}")

                # Execute the function
                result = await func(*args, **kwargs)

                # Record success observability
                _record_success_observability(
                    logger,
                    metrics,
                    traces,
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
                    logger,
                    metrics,
                    traces,
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
            # Generate trace ID and span ID
            trace_id = str(uuid.uuid4())
            span_id = str(uuid.uuid4())
            start_time = time.time()

            try:
                # Log start of operation
                logger.debug(f"Starting sync function {func_name}")

                # Execute the function
                result = func(*args, **kwargs)

                # Record success observability
                _record_success_observability(
                    logger,
                    metrics,
                    traces,
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
                    logger,
                    metrics,
                    traces,
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
