"""Shared HTTP middleware for the v3 handler service.

Exports:
    LogMiddleware: Request/response logging middleware.
    SecurityHeadersMiddleware: Always-on response-hardening headers.
    EXCLUDED_LOG_PATHS: Paths skipped by LogMiddleware and the OTel
        FastAPI auto-instrumentor.
"""

from application_sdk.server.middleware._constants import EXCLUDED_LOG_PATHS
from application_sdk.server.middleware.headers import SecurityHeadersMiddleware
from application_sdk.server.middleware.log import LogMiddleware

__all__ = [
    "EXCLUDED_LOG_PATHS",
    "LogMiddleware",
    "SecurityHeadersMiddleware",
]
