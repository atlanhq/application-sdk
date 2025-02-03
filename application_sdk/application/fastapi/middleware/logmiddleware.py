import json
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
    # Define paths that should have their response bodies filtered
    SENSITIVE_PATHS = ["/workflows/v1/auth", "/workflows/v1/check"]

    # Define fields to omit from response logging
    OMITTED_FIELDS: list[str] = ["response", "items", "data", "password", "token"]

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

    def _mask_sensitive_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Mask sensitive information in request bodies."""
        SENSITIVE_FIELDS: set[str] = {
            "password",
            "token",
            "secret",
            "key",
            "authorization",
            "access_token",
            "refresh_token",
            "api_key",
        }

        masked_data: Dict[str, Any] = data.copy()
        for key, value in data.items():
            if any(sensitive in key.lower() for sensitive in SENSITIVE_FIELDS):
                masked_data[key] = "********"
            elif isinstance(value, dict):
                masked_data[key] = self._mask_sensitive_data(value)  # type: ignore
            elif isinstance(value, list):
                masked_data[key] = [
                    self._mask_sensitive_data(item) if isinstance(item, dict) else item  # type: ignore
                    for item in value  # type: ignore
                ]
        return masked_data

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

    async def _get_response_info(
        self,
        request: Request,
        response: Response,
        response_body: bytes,
        is_stream: bool,
    ) -> Dict[str, Any]:
        """Extract and format response information for logging."""
        filtered_response = None

        if response_body and not is_stream:
            try:
                response_text = response_body.decode("utf-8")
                if response_text:
                    # Only attempt to parse and filter JSON for specific content types
                    if response.media_type == "application/json":
                        response_data = json.loads(response_text)

                        # Special handling for sensitive paths
                        if request.url.path in self.SENSITIVE_PATHS:
                            filtered_response = (
                                "*** Response filtered for sensitive path ***"
                            )
                        else:
                            # Filter sensitive fields from response
                            filtered_data = self._filter_sensitive_data(response_data)
                            filtered_response = json.dumps(filtered_data)
                    else:
                        filtered_response = "*** Non-JSON response ***"
            except (json.JSONDecodeError, UnicodeDecodeError):
                filtered_response = "*** Invalid JSON or binary response ***"

        return {
            "status_code": response.status_code,
            "method": request.method,
            "url": str(request.url),
            "path": request.url.path,
            "is_stream": is_stream,
            "body": filtered_response,
            "request_type": "outbound",
            "content_type": response.media_type,
            "response_size": len(response_body) if response_body else 0,
        }

    def _filter_sensitive_data(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """Filter out sensitive fields from dictionary data."""
        filtered_data = data.copy()
        for field in self.OMITTED_FIELDS:
            if field in filtered_data:
                filtered_data[field] = "*** FILTERED ***"

        return filtered_data
