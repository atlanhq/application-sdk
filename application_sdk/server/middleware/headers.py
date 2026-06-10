"""Response-hardening headers middleware for the v3 handler service.

Pure ASGI (unlike ``log.py``'s ``BaseHTTPMiddleware``) on purpose:
``BaseHTTPMiddleware`` buffers the response through memory streams, which
breaks streaming/SSE responses — and the MCP streamable-HTTP transport
mounted on the same app relies on streaming. A pure ASGI wrapper is
transparent to streaming responses.
"""

from starlette.types import ASGIApp, Message, Receive, Scope, Send

#: Response-hardening headers applied to every HTTP response.
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-frame-options", b"DENY"),
    (b"x-content-type-options", b"nosniff"),
    (b"referrer-policy", b"no-referrer"),
)


class SecurityHeadersMiddleware:
    """Set response-hardening headers on every HTTP response (pure ASGI).

    Adds ``X-Frame-Options: DENY``, ``X-Content-Type-Options: nosniff``,
    and ``Referrer-Policy: no-referrer``. Headers already set by the app
    are left untouched so routes can deliberately override them.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = [tuple(h) for h in (message.get("headers") or [])]
                existing = {name.lower() for name, _ in headers}
                for name, value in SECURITY_HEADERS:
                    if name not in existing:
                        headers.append((name, value))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
