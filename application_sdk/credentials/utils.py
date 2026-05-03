"""Credential parsing utilities."""

import base64
import json
import os
from typing import Any, Dict, Optional, Union

import orjson

from application_sdk.common.error_codes import CommonError
from application_sdk.common.utils import download_file_from_upload_response
from application_sdk.constants import TEMPORARY_PATH
from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


def parse_credentials_extra(credentials: Dict[str, Any]) -> Dict[str, Any]:
    """Parse the 'extra' field from credentials, handling both string and dict inputs.

    Args:
        credentials: Credentials dictionary containing an 'extra' field.

    Returns:
        Parsed extra field as a dictionary.

    Raises:
        CommonError: If the extra field contains invalid JSON.
    """
    extra: Union[str, Dict[str, Any]] = credentials.get("extra", {})

    if isinstance(extra, str):
        try:
            return json.loads(extra)
        except json.JSONDecodeError as e:
            raise CommonError(
                f"{CommonError.CREDENTIALS_PARSE_ERROR}: Invalid JSON in credentials extra field: {e}"
            ) from e

    return extra


async def resolve_credential_file(
    value: Optional[str],
    filename: str,
    dest_dir: str = os.path.join(TEMPORARY_PATH, "credential_files"),
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
        dest_dir: Directory to write or download the file into. Defaults to
                  ``<TEMPORARY_PATH>/credential_files``.

    Returns:
        Absolute path to the resolved file on disk, or ``None`` if ``value`` is
        empty or resolution fails.
    """
    if not value:
        return None

    # Detect format: JSON object-store reference vs raw base64 string
    try:
        parsed = orjson.loads(value)
        if isinstance(parsed, dict) and ("key" in parsed or "fileKey" in parsed):
            # Object-store reference — delegate to existing download utility
            return await download_file_from_upload_response(value)
    except (orjson.JSONDecodeError, TypeError):
        pass

    # Base64-encoded file content — decode and write to disk
    try:
        os.makedirs(dest_dir, exist_ok=True)
        file_path = os.path.join(dest_dir, filename)
        decoded_bytes = base64.b64decode(value.strip(), validate=True)
        with open(file_path, "wb") as f:
            f.write(decoded_bytes)
        logger.info(
            "Resolved credential file from base64 content: path=%s filename=%s",
            file_path,
            filename,
        )
        return file_path
    except Exception:
        logger.error(
            "Failed to resolve credential file from base64 content: filename=%s",
            filename,
            exc_info=True,
        )
        return None
