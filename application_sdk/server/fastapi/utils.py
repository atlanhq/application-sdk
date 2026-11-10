"""FastAPI utility functions of the v2 server.

.. deprecated:: 3.56.0
    Every public function here is deprecated and removed in v4.0. The v3
    handler service installs its own error handling and serves file uploads at
    ``POST /workflows/v1/file``; nothing in the SDK imports this module.
"""

import mimetypes
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import UploadFile, status
from fastapi.responses import JSONResponse
from typing_extensions import deprecated

from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.server.fastapi.models import FileUploadResponse
from application_sdk.storage.batch import upload_file_from_bytes


@deprecated(
    "application_sdk.server.fastapi.utils.internal_server_error_handler is deprecated; use the v3 handler "
    "service instead (application_sdk.handler), which installs its own "
    "sanitised 500 handling — will be removed in v4.0."
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
    """
    logger = get_logger(__name__)
    logger.error("Internal server error: %s", exc, exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error": "An internal error has occurred.",
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


@deprecated(
    "application_sdk.server.fastapi.utils.upload_file_to_object_store is deprecated; use the handler's "
    "POST /workflows/v1/file route instead, or "
    "application_sdk.storage.batch.upload_file_from_bytes to write the bytes "
    "yourself — will be removed in v4.0."
)
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

    await upload_file_from_bytes(
        content=content_bytes,
        key=object_store_key,
    )

    now_ms = int(time.time() * 1000)

    # The caller was already warned that this function is deprecated; do not
    # warn a second time for the (equally deprecated) model it returns.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
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
