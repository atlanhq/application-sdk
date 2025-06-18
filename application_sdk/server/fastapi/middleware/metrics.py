from time import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from application_sdk.observability.metrics_adaptor import MetricType, get_metrics

metrics = get_metrics()


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        metrics = get_metrics()
        start_time = time()

        try:
            response: Response = await call_next(request)
        except Exception as exc:
            response = Response(status_code=500)
            raise exc
        finally:
            process_time = time() - start_time
            path = request.url.path
            method = request.method
            status_code = response.status_code

            labels = {
                "path": path,
                "method": method,
                "status": str(status_code),
            }

            # Record request count
            metrics.record_metric(
                name="http_requests_total",
                value=1,
                metric_type=MetricType.COUNTER,
                labels=labels,
                description="Total number of HTTP requests",
            )

            # Record request latency
            metrics.record_metric(
                name="http_request_duration_seconds",
                value=process_time,
                metric_type=MetricType.HISTOGRAM,
                labels=labels,
                description="Duration of HTTP requests",
                unit="seconds",
            )

        return response
