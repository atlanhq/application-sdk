"""Middleware for backward compatibility with v2 credential format."""

import json
from typing import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class CredentialFormatMiddleware(BaseHTTPMiddleware):
    """Convert v2 flat credential format to v3 array format.

    Provides backward compatibility for apps that send credentials as:
        {"host": "...", "password": "...", ...}

    Converts to v3 format expected by AuthInput:
        {"credentials": [{"key": "host", "value": "..."}, ...]}

    Both formats work after this middleware is applied.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        # Only process auth endpoint POST requests
        if request.url.path == "/workflows/v1/auth" and request.method == "POST":
            try:
                body = await request.body()
                data = json.loads(body) if body else {}

                # Check if already in v3 format (has credentials array)
                has_credentials = (
                    "credentials" in data
                    and isinstance(data.get("credentials"), list)
                    and len(data.get("credentials", [])) > 0
                )

                # Check if in v2 format (has flat fields)
                has_flat_fields = any(
                    k not in ["credentials", "connection_id", "timeout_seconds"]
                    for k in data.keys()
                )

                # Only convert if v2 format detected
                if not has_credentials and has_flat_fields:
                    logger.info("Converting v2 flat credential format to v3 array")

                    # Extract flat credential fields
                    flat_creds = {
                        k: v
                        for k, v in data.items()
                        if k not in ["credentials", "connection_id", "timeout_seconds"]
                    }

                    # Flatten nested 'extra' object
                    extra = flat_creds.pop("extra", {})
                    if extra and isinstance(extra, dict):
                        flat_creds.update(extra)

                    # Convert to v3 array format
                    credentials_array = [
                        {"key": k, "value": v}
                        for k, v in flat_creds.items()
                        if v is not None
                    ]

                    # Build v3 format request
                    converted_data = {
                        "credentials": credentials_array,
                        "connection_id": data.get("connection_id", ""),
                        "timeout_seconds": data.get("timeout_seconds", 30),
                    }

                    logger.debug(
                        f"Converted {len(credentials_array)} credential fields"
                    )

                    # Replace request body with converted format
                    async def receive():
                        return {
                            "type": "http.request",
                            "body": json.dumps(converted_data).encode(),
                            "more_body": False,
                        }

                    request._receive = receive

            except Exception as e:
                logger.error(f"Credential format conversion error: {e}", exc_info=True)
                # Let request proceed unchanged on error

        return await call_next(request)
