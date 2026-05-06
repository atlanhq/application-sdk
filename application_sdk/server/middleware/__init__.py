"""Shared HTTP middleware for the v3 handler service.

Exports:
    LogMiddleware: Request/response logging middleware.
    EXCLUDED_LOG_PATHS: Paths skipped by LogMiddleware and the OTel
        FastAPI auto-instrumentor.
"""

from application_sdk.server.middleware._constants import EXCLUDED_LOG_PATHS
from application_sdk.server.middleware.log import LogMiddleware

__all__ = ["LogMiddleware", "EXCLUDED_LOG_PATHS"]
