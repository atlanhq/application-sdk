import functools
import inspect
import time
import uuid
from typing import Any, Callable, TypeVar, cast

from application_sdk.observability.metrics_adaptor import MetricType

T = TypeVar("T")


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
        is_async = inspect.iscoroutinefunction(func)

        # Debug logging for function decoration
        logger.debug(f"Decorating function {func_name} (async={is_async})")

        @functools.wraps(func)
        async def async_wrapper(*args: Any, **kwargs: Any) -> T:
            return await _execute_with_observability(
                func, args, kwargs, logger, metrics, traces, is_async
            )

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> T:
            return _execute_with_observability(
                func, args, kwargs, logger, metrics, traces, is_async
            )

        # Return appropriate wrapper based on function type
        return cast(Callable[..., T], async_wrapper if is_async else sync_wrapper)

    return decorator


async def _execute_with_observability(
    func: Callable[..., T],
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    logger: Any,
    metrics: Any,
    traces: Any,
    is_async: bool,
) -> T:
    """Execute a function with observability instrumentation.

    Args:
        func: The function to execute
        args: Positional arguments for the function
        kwargs: Keyword arguments for the function
        logger: Logger instance for operation logging
        metrics: Metrics adapter for recording operation metrics
        traces: Traces adapter for recording operation traces
        is_async: Whether the function is async

    Returns:
        The result of the function execution

    Raises:
        Exception: Any exception raised by the function
    """
    # Generate trace ID and span ID
    trace_id = str(uuid.uuid4())
    span_id = str(uuid.uuid4())
    start_time = time.time()
    func_name = func.__name__
    func_doc = func.__doc__ or f"Executing {func_name}"

    try:
        # Log start of operation
        logger.debug(f"Starting {'async' if is_async else 'sync'} function {func_name}")

        # Execute the function
        result = await func(*args, **kwargs) if is_async else func(*args, **kwargs)

        # Calculate duration
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
                    "module": func.__module__,
                },
                events=[{"name": f"{func_name}_success", "timestamp": time.time()}],
                duration_ms=duration_ms,
            )
            logger.debug(f"Successfully recorded trace for {func_name}")
        except Exception as trace_error:
            logger.debug(f"Failed to record trace for {func_name}: {str(trace_error)}")

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
            logger.debug(
                f"Failed to record metric for {func_name}: {str(metric_error)}"
            )

        # Log completion
        logger.debug(f"Completed {func_name} in {duration_ms:.2f}ms")

        return result

    except Exception as e:
        # Calculate duration
        duration_ms = (time.time() - start_time) * 1000

        # Debug logging for error case
        logger.error(
            f"Error in {'async' if is_async else 'sync'} function {func_name}: {str(e)}"
        )

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
                    "module": func.__module__,
                },
                events=[
                    {
                        "name": f"{func_name}_failure",
                        "timestamp": time.time(),
                        "attributes": {"error": str(e)},
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
                labels={"function": func_name, "error": str(e)},
                description=f"Failed {func_name}",
                unit="count",
            )
            logger.debug(f"Successfully recorded error metric for {func_name}")
        except Exception as metric_error:
            logger.error(
                f"Failed to record error metric for {func_name}: {str(metric_error)}"
            )

        # Log error
        logger.error(f"Error in {func_name}: {str(e)}")

        raise
