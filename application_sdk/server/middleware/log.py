"""Request/response logging middleware."""

import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from application_sdk.observability.context import request_context
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.server.middleware._constants import EXCLUDED_LOG_PATHS

logger = get_logger(__name__)


class LogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        self.logger = logger
        # Remove any existing handlers to prevent duplicate logging
        self.logger.logger.handlers = []

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid4())

        token = request_context.set({"request_id": request_id})
        start_time = time.time()

        should_log = request.url.path not in EXCLUDED_LOG_PATHS

        if should_log:
            client_host = request.client.host if request.client else None
            self.logger.info(
                "Request started: method=%s path=%s request_id=%s url=%s client_host=%s",
                request.method,
                request.url.path,
                request_id,
                str(request.url),
                client_host,
            )

        try:
            response = await call_next(request)
            duration = time.time() - start_time

            if should_log:
                self.logger.info(
                    "Request completed: method=%s path=%s status_code=%d duration_ms=%.2f request_id=%s",
                    request.method,
                    request.url.path,
                    response.status_code,
                    round(duration * 1000, 2),
                    request_id,
                )
            return response

        except Exception as exc:
            duration = time.time() - start_time
            self.logger.error(
                "Request failed: method=%s path=%s duration_ms=%.2f request_id=%s error_type=%s",
                request.method,
                request.url.path,
                round(duration * 1000, 2),
                request_id,
                type(exc).__name__,
                exc_info=True,
            )
            raise
        finally:
            request_context.reset(token)
