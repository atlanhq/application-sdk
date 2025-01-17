from fastapi import status
from fastapi.responses import JSONResponse


def internal_server_error_handler(_, exc: Exception):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "An internal error has occurred.", "details": str(exc)},
    )
