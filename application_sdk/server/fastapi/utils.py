"""FastAPI utility functions.

This module provides utility functions for FastAPI application, including
error handlers and response formatters.
"""

from fastapi import status
from fastapi.responses import JSONResponse

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Paths to exclude from logging and metrics (health checks and event ingress)
EXCLUDED_LOG_PATHS: frozenset[str] = frozenset(
    {
        "/server/health",
        "/server/ready",
        "/api/eventingress/",
        "/api/eventingress",
    }
)


def internal_server_error_handler(_, exc: Exception) -> JSONResponse:
    """Handle internal server errors in FastAPI applications.

    This function provides a standardized way to handle internal server errors (500)
    by formatting them into a consistent JSON response structure. Exception details
    are logged server-side but not exposed to clients to prevent information leakage.

    Args:
        _ (Request): The FastAPI request object (unused).
        exc (Exception): The exception that triggered the error handler.

    Returns:
        JSONResponse: A formatted error response with the following structure:
            - success (bool): Always False for errors
            - error (str): A generic error message
    """
    # Log the actual exception for debugging (server-side only)
    logger.error(f"Internal server error: {exc}", exc_info=True)

    # Return generic message to client - do not expose exception details
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": "An internal error has occurred. Check server logs for details.",
        },
    )
