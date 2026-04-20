"""Common utilities.

Most functions previously in this module have been moved to more logical homes:

- SQL filter pipeline → ``common.sql_filters``
- Credential parsing → ``credentials.utils``
- CPU/threading → ``common.concurrency``
"""

import base64
import json
import os
from typing import Optional, Union

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


async def resolve_credential_file(
    value: Optional[str],
    filename: str,
    dest_dir: str = "/tmp/credential_files",
) -> Optional[str]:
    """Resolve a credential file field value to a local file path.

    Handles two input formats transparently, allowing customers to choose
    how they provide sensitive files based on their organisation's security policy:

    1. **Object-store reference** (file uploaded via UI):
       ``{"key": "some/path", "rawName": "hiveadmin.keytab", "extension": ".keytab"}``
       Downloads the binary from the Dapr-backed object store.

    2. **Base64-encoded file content** (stored in customer's own secret store):
       ``"BQIAAAABAAoASElWRS5MT0NBTA..."``
       Decodes the binary and writes it directly to disk.
       Used when the customer base64-encodes the file, stores it in their secret
       store (AWS / Azure / GCP / K8s), and the SDK resolves the value via
       ``SecretStore.get_credentials()`` + Dapr at activity runtime.

    Args:
        value:    Raw credential field value — either a JSON object-store reference
                  or a raw base64-encoded string. Returns ``None`` if empty.
        filename: Destination filename used for the base64 path
                  (e.g. ``"keytab.keytab"``, ``"krb5.conf"``, ``"ca_cert.pem"``).
                  Ignored for the object-store path (filename is derived from the key).
        dest_dir: Directory to write or download the file into.

    Returns:
        Absolute path to the resolved file on disk, or ``None`` if ``value`` is
        empty or resolution fails.
    """
    if not value:
        return None

    # Detect format: JSON object-store reference vs raw base64 string
    try:
        parsed = json.loads(value)
        if isinstance(parsed, dict) and ("key" in parsed or "fileKey" in parsed):
            # Object-store reference — delegate to existing download utility
            return await download_file_from_upload_response(value)
    except (json.JSONDecodeError, TypeError):
        pass

    # Base64-encoded file content — decode and write to disk
    try:
        os.makedirs(dest_dir, exist_ok=True)
        file_path = os.path.join(dest_dir, filename)
        decoded_bytes = base64.b64decode(value.strip())
        with open(file_path, "wb") as f:
            f.write(decoded_bytes)
        logger.info(
            "Resolved credential file from base64 content",
            extra={"path": file_path, "filename": filename},
        )
        return file_path
    except Exception:
        logger.error(
            "Failed to resolve credential file from base64 content",
            extra={"filename": filename},
            exc_info=True,
        )
        return None
