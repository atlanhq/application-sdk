import time
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from application_sdk.observability.logger_adaptor import get_logger, request_context

logger = get_logger(__name__)


class LogMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)
        # Use the existing logger instead of creating a new one
        self.logger = logger
        # Remove any existing handlers to prevent duplicate logging
        self.logger.logger.handlers = []

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid4())

        # Set the request_id in context
        token = request_context.set({"request_id": request_id})
        start_time = time.time()

        self.logger.info(
            f"Request started for {request.method} {request.url.path}",
            extra={
                "method": request.method,
                "path": request.url.path,
                "request_id": request_id,
                "url": str(request.url),
                "client_host": request.client.host if request.client else None,
            },
        )

        try:
            response = await call_next(request)
            duration = time.time() - start_time

            self.logger.info(
                f"Request completed for {request.method} {request.url.path} {response.status_code}",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "status_code": response.status_code,
                    "duration_ms": round(duration * 1000, 2),
                    "request_id": request_id,
                },
            )
            return response

        except Exception as e:
            duration = time.time() - start_time
            self.logger.error(
                f"Request failed for {request.method} path: {request.url.path} with request_id: {request_id}",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "error": str(e),
                    "duration_ms": round(duration * 1000, 2),
                    "request_id": request_id,
                },
                exc_info=True,
            )
            raise
        finally:
            request_context.reset(token)
