"""FastAPI utility functions.

This module provides utility functions for FastAPI application, including
error handlers and response formatters.
"""

import mimetypes
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import UploadFile, status
from fastapi.responses import JSONResponse

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


async def upload_file_to_object_store(
    file: UploadFile,
    filename: Optional[str] = None,
    prefix: Optional[str] = "workflow_file_upload",
) -> FileUploadResponse:
    """Upload file to object store and return metadata.

    This function handles file uploads with the following behavior:
    - Extracts filename, content_type, and size from UploadFile object
    - Generates a unique UUID for the file ID
    - Creates fileName as UUID + extension
    - Preserves original filename as rawName
    - Includes prefix in key if provided: prefix + "/" + fileName
    - Uploads file content to object store
    - Returns FileUploadResponse object with file metadata

    Args:
        file (UploadFile): FastAPI UploadFile object containing the file.
        filename (Optional[str]): Original filename from form data. If provided,
            this takes precedence over file.filename. Defaults to None.
        prefix (Optional[str]): Prefix for file organization in object store.
            Defaults to "workflow_file_upload".

    Returns:
        FileUploadResponse: Pydantic model object containing file metadata with
            the following fields:
            - id (str): UUID of the file
            - version (str): Version string (first 8 chars of UUID)
            - isActive (bool): Whether the file is active
            - createdAt (int): Unix timestamp in milliseconds
            - updatedAt (int): Unix timestamp in milliseconds
            - fileName (str): Generated filename (UUID + extension)
            - rawName (str): Original filename
            - key (str): Object store key (includes prefix if provided)
            - extension (str): File extension
            - contentType (str): Content type of the file
            - fileSize (int): File size in bytes
            - isEncrypted (bool): Whether the file is encrypted
            - redirectUrl (str): Redirect URL (empty string)
            - isUploaded (bool): Whether the file is uploaded
            - uploadedAt (str): ISO timestamp of upload
            - isArchived (bool): Whether the file is archived

    Raises:
        Exception: If there's an error uploading to the object store.
    """
    # Read file content
    file_content = await file.read()

    # Extract metadata from UploadFile object
    # Use passed filename first, then file.filename, then fallback to "uploaded_file"
    filename = filename or file.filename or "uploaded_file"
    content_type = file.content_type or "application/octet-stream"
    size = len(file_content)
    # Generate UUID for file ID
    file_id = str(uuid.uuid4())

    # Determine extension from filename or content type
    # First try to get extension from filename, then fall back to content type
    extension = Path(filename).suffix
    if not extension:
        extension_list = mimetypes.guess_all_extensions(content_type)
        if extension_list:
            extension = extension_list[0]
        else:
            extension = ""

    # Generate file name and raw name
    # fileName = id + extension, rawName = original filename
    file_name = f"{file_id}{extension}"
    raw_name = filename  # Keep original filename as rawName

    # Generate key with prefix
    # key = fileName if prefix is empty, else prefix + "/" + fileName
    key = file_name
    if prefix:
        key = f"{prefix}/{file_name}"

    # Upload directly from bytes to object store
    await ObjectStore.upload_file_from_bytes(
        file_content=file_content,
        destination=key,
    )

    # Generate metadata and construct FileUploadResponse object
    now_ms = int(time.time() * 1000)
    # Simple version generation using first 8 chars of UUID
    version = file_id[:8]

    return FileUploadResponse(
        id=file_id,
        version=version,
        isActive=True,
        createdAt=now_ms,
        updatedAt=now_ms,
        fileName=file_name,
        rawName=raw_name,
        key=key,
        extension=extension,
        contentType=content_type,
        fileSize=size,
        isEncrypted=False,
        redirectUrl="",
        isUploaded=True,
        uploadedAt=datetime.utcnow().isoformat() + "Z",
        isArchived=False,
    )
