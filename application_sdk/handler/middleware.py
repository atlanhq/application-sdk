"""Middleware for backward compatibility with v2 credential format.

Converts v2 flat credential payloads to v3 array format for all
handler endpoints (/auth, /check, /metadata).
"""

import json
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Endpoints that accept credentials
_CREDENTIAL_PATHS = {
    "/workflows/v1/auth",
    "/workflows/v1/check",
    "/workflows/v1/metadata",
}

# Keys that belong to v3 contract models (not credential fields)
_V3_ONLY_KEYS = {
    "credentials", "connection_id", "timeout_seconds",
    "connection_config", "checks_to_run",
    "object_filter", "include_fields", "max_objects",
}


def _is_v2_flat_format(data: dict[str, Any]) -> bool:
    """Detect if the payload is v2 flat credential format."""
    has_v3_credentials = (
        "credentials" in data
        and isinstance(data.get("credentials"), list)
        and len(data.get("credentials", [])) > 0
    )
    if has_v3_credentials:
        return False
    return any(k not in _V3_ONLY_KEYS for k in data.keys())


def _convert_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """Convert v2 flat credential payload to v3 array format."""
    flat_creds = {k: v for k, v in data.items() if k not in _V3_ONLY_KEYS}

    # Flatten nested 'extra' object
    extra = flat_creds.pop("extra", {})
    if extra and isinstance(extra, dict):
        flat_creds.update(extra)

    # Convert to v3 array (values must be strings for HandlerCredential)
    credentials_array = [
        {"key": str(k), "value": str(v)}
        for k, v in flat_creds.items()
        if v is not None
    ]

    logger.info(
        "Converted v2 flat credentials to v3 array: %d fields",
        len(credentials_array),
    )

    # Preserve any v3-specific fields
    converted: dict[str, Any] = {"credentials": credentials_array}
    for key in _V3_ONLY_KEYS:
        if key in data and key != "credentials":
            converted[key] = data[key]

    return converted


class CredentialFormatMiddleware:
    """Raw ASGI middleware to convert v2 flat credential format to v3 array.

    Handles /auth, /check, and /metadata endpoints.
    Uses raw ASGI (not BaseHTTPMiddleware) to properly replace the request body.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        path = request.url.path
        method = request.method

        if method == "POST" and path in _CREDENTIAL_PATHS:
            body = await request.body()
            try:
                data = json.loads(body) if body else {}
                if _is_v2_flat_format(data):
                    converted = _convert_v2_to_v3(data)
                    body = json.dumps(converted).encode()
            except Exception as e:
                logger.warning("Credential format conversion error for %s: %s", path, e)

            body_sent = False

            async def new_receive() -> Message:
                nonlocal body_sent
                if not body_sent:
                    body_sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return {"type": "http.disconnect"}

            await self.app(scope, new_receive, send)
        else:
            await self.app(scope, receive, send)
