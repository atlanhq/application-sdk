"""FastAPI utility functions.

This module provides utility functions for FastAPI application, including
error handlers and response formatters.
"""

from fastapi import status
from fastapi.responses import JSONResponse


def internal_server_error_handler(_, exc: Exception) -> JSONResponse:
    """Handle internal server errors in FastAPI applications.

    This function provides a standardized way to handle internal server errors (500)
    by formatting them into a consistent JSON response structure.

    Args:
        _ (Request): The FastAPI request object (unused).
        exc (Exception): The exception that triggered the error handler.

    Returns:
        JSONResponse: A formatted error response with the following structure:
            - success (bool): Always False for errors
            - error (str): A generic error message
            - details (str): The string representation of the exception
    """
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": "An internal error has occurred.",
            "details": str(exc),
        },
    )
