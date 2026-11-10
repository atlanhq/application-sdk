"""Common utilities.

Most functions previously in this module have been moved to more logical homes:

- SQL filter pipeline → ``common.sql_filters``
- Credential parsing → ``credentials.utils``
- CPU/threading → ``common.concurrency``
"""

import os
import warnings

import orjson
from pydantic import BaseModel

from application_sdk._context_errors import ObjectStoreNotConfiguredError
from application_sdk._install import worker_only_missing
from application_sdk._logging import get_logger
from application_sdk.constants import TEMPORARY_PATH

logger = get_logger(__name__)


async def download_file_from_upload_response(
    response: dict | str | BaseModel,
) -> str:
    """Download a file that was uploaded via the /file endpoint.

    Takes the upload response returned by the file upload endpoint (or its
    dict/JSON representation) and downloads the file to a local temporary path
    for processing in workflows or activities.

    Args:
        response: The upload response containing the object store key. Can be
            a dict, a JSON string, or any pydantic model with a ``key`` field —
            :class:`application_sdk.handler.contracts.FileUploadResponse` (what
            the handler's ``POST /workflows/v1/file`` returns) or the deprecated
            ``application_sdk.server.fastapi.models.FileUploadResponse``.

    Returns:
        The local file path where the file was downloaded.

    Raises:
        ValueError: If the input is a string that isn't valid JSON, or if
            the ``key`` field is missing from the response.
    """
    if isinstance(response, str):
        try:
            response = orjson.loads(response)
        except orjson.JSONDecodeError as e:
            from application_sdk.common.errors import JsonParseError  # noqa: PLC0415

            raise JsonParseError(cause=e) from e

    if isinstance(response, BaseModel):
        # Read the declared field, not getattr: a model without one is a type
        # error, not a model whose key happens to be missing.
        if "key" not in type(response).model_fields:
            from application_sdk.common.errors import ResponseTypeError  # noqa: PLC0415

            raise ResponseTypeError(
                message=f"Unsupported response type: {type(response).__name__}"
            )
        key = getattr(response, "key")  # noqa: B009 — BaseModel declares no ``key``; checked above
        # handler.contracts.FileUploadResponse defaults ``key`` to "": an unset
        # key is a missing key, not a request to download the temp directory.
        if not key:
            from application_sdk.common.errors import (  # noqa: PLC0415
                ResponseKeyMissingError,
            )

            raise ResponseKeyMissingError()
    elif isinstance(response, dict):
        if "key" not in response:
            from application_sdk.common.errors import (  # noqa: PLC0415
                ResponseKeyMissingError,
            )

            raise ResponseKeyMissingError()
        key = response["key"]
    else:
        from application_sdk.common.errors import ResponseTypeError  # noqa: PLC0415

        raise ResponseTypeError(
            message=f"Unsupported response type: {type(response).__name__}"
        )

    local_path = os.path.join(TEMPORARY_PATH, key)

    try:
        from application_sdk.storage.ops import (  # noqa: PLC0415 — lazy: storage.ops imports obstore (heavy Rust ext) at module load, and this module sits on the workflow-sandbox import chain (credentials → here); pinned by the preflight gate's import-hygiene test
            download_file_chunked,
        )
    except ModuleNotFoundError as exc:
        if not worker_only_missing(exc):
            raise
        # api-only install (the API host): there is no object store to read from.
        raise ObjectStoreNotConfiguredError() from exc

    # Chunk large uploads (user-supplied files are uncapped) so a big artifact
    # survives slow egress; small files still stream in a single GET. (BLDX-1513)
    await download_file_chunked(
        key=key,
        local_path=local_path,
    )

    return local_path


#: name -> (replacement, why). Served by __getattr__ below (PEP 562).
_DEPRECATED_CONSTANTS: dict[str, tuple[str, str]] = {
    "FileUploadResponse": (
        "application_sdk.handler.contracts.FileUploadResponse",
        "this module only ever re-exported the v2 "
        "application_sdk.server.fastapi.models.FileUploadResponse, which is "
        "itself deprecated",
    ),
}


def _deprecated_constant_value(name: str) -> object:
    if name == "FileUploadResponse":
        # Lazy: this module sits on the workflow-sandbox import chain
        # (credentials → here) and must not pull the v2 server models in.
        from application_sdk.server.fastapi.models import (  # noqa: PLC0415
            FileUploadResponse,
        )

        return FileUploadResponse
    raise AssertionError(name)


def __getattr__(name: str) -> object:
    """Serve the removed re-export once more, with a deprecation warning (PEP 562)."""
    entry = _DEPRECATED_CONSTANTS.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    replacement, note = entry
    warnings.warn(
        f"{name} is deprecated; use {replacement} instead — {note}. "
        "Will be removed in v4.0.",
        DeprecationWarning,
        stacklevel=2,
    )
    return _deprecated_constant_value(name)
