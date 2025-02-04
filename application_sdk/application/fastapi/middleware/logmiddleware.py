import logging
import time
from typing import Any, Dict, Optional
from uuid import uuid4

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter, request_context

logger = AtlanLoggerAdapter(logging.getLogger(__name__))


class LogMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = str(uuid4())

        # Set the request_id in context
        token = request_context.set({"request_id": request_id})
        start_time = time.time()

        logger.info(
            "Request started",
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

            logger.info(
                "Request completed",
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
            logger.error(
                "Request failed",
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

    def _get_request_info(
        self, request: Request, request_body_str: Optional[str]
    ) -> Dict[str, Any]:
        """Extract and format request information for logging."""
        return {
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "client_host": request.client.host if request.client else None,
            "body": request_body_str,
            "headers": dict(request.headers),
        }
