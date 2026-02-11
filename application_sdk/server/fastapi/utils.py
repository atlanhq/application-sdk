"""FastAPI utility functions.

This module provides utility functions for FastAPI application, including
error handlers and response formatters.
"""

import mimetypes
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import UploadFile, status
from fastapi.responses import JSONResponse

from application_sdk.constants import UPSTREAM_OBJECT_STORE_NAME
from application_sdk.server.fastapi.models import FileUploadResponse
from application_sdk.services.objectstore import ObjectStore

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


def _resolve_extension(original_filename: str, resolved_content_type: str) -> str:
    """Determine file extension from filename, falling back to content type."""
    extension = Path(original_filename).suffix
    if not extension:
        guessed = mimetypes.guess_all_extensions(resolved_content_type)
        extension = guessed[0] if guessed else ""
    return extension


def _build_object_store_key(stored_filename: str, prefix: Optional[str]) -> str:
    """Build object store key with optional prefix directory."""
    if prefix:
        return f"{prefix}/{stored_filename}"
    return stored_filename


async def upload_file_to_object_store(
    file: UploadFile,
    filename: Optional[str] = None,
    prefix: Optional[str] = "workflow_file_upload",
    content_type: Optional[str] = None,
) -> FileUploadResponse:
    """Upload file to object store and return metadata.

    Args:
        file: FastAPI UploadFile object containing the file.
        filename: Original filename from form data. Takes precedence
            over file.filename. Defaults to None.
        prefix: Prefix for file organization in object store.
            Defaults to "workflow_file_upload".
        content_type: Explicit content type. Takes precedence
            over file.content_type. Defaults to None.

    Returns:
        FileUploadResponse with file metadata (id, key, rawName, etc.).

    Raises:
        Exception: If there's an error uploading to the object store.
    """
    content_bytes = await file.read()

    original_filename = filename or file.filename or "uploaded_file"
    resolved_content_type = (
        content_type or file.content_type or "application/octet-stream"
    )

    file_id = str(uuid.uuid4())
    extension = _resolve_extension(original_filename, resolved_content_type)
    stored_filename = f"{file_id}{extension}"
    object_store_key = _build_object_store_key(stored_filename, prefix)

    await ObjectStore.upload_file_from_bytes(
        file_content=content_bytes,
        destination=object_store_key,
        store_name=UPSTREAM_OBJECT_STORE_NAME,
    )

    now_ms = int(time.time() * 1000)

    return FileUploadResponse(
        id=file_id,
        version=file_id[:8],
        isActive=True,
        createdAt=now_ms,
        updatedAt=now_ms,
        fileName=stored_filename,
        rawName=original_filename,
        key=object_store_key,
        extension=extension,
        contentType=resolved_content_type,
        fileSize=len(content_bytes),
        isEncrypted=False,
        redirectUrl="",
        isUploaded=True,
        uploadedAt=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        isArchived=False,
    )
