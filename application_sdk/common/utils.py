"""Common utilities.

Most functions previously in this module have been moved to more logical homes:

- SQL filter pipeline → ``common.sql_filters``
- Credential parsing → ``credentials.utils``
- CPU/threading → ``common.concurrency``
"""

import json
import os
from typing import Union

from application_sdk.constants import TEMPORARY_PATH
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.server.fastapi.models import FileUploadResponse
from application_sdk.storage.ops import download_file

logger = get_logger(__name__)


async def download_file_from_upload_response(
    response: Union[dict, str, FileUploadResponse],
) -> str:
    """Download a file that was uploaded via the /file endpoint.

    Takes the FileUploadResponse (or its dict/JSON representation) returned by
    the file upload endpoint and downloads the file to a local temporary path
    for processing in workflows or activities.

    Args:
        response: The upload response containing the object store key.
            Can be a dict, a JSON string, or a FileUploadResponse object.

    Returns:
        The local file path where the file was downloaded.

    Raises:
        ValueError: If the input is a string that isn't valid JSON, or if
            the ``key`` field is missing from the response.
    """
    if isinstance(response, str):
        try:
            response = json.loads(response)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON string: {e}") from e

    if isinstance(response, FileUploadResponse):
        key = response.key
    elif isinstance(response, dict):
        if "key" not in response:
            raise ValueError("Response dict is missing required 'key' field")
        key = response["key"]
    else:
        raise ValueError(f"Unsupported response type: {type(response)}")

    local_path = os.path.join(TEMPORARY_PATH, key)

    await download_file(
        key=key,
        local_path=local_path,
    )

    return local_path
