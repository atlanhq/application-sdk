"""Credential parsing utilities."""

import base64
import json
import os
from typing import Any

import orjson

from application_sdk.common.utils import download_file_from_upload_response
from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME, TEMPORARY_PATH
from application_sdk.errors import InvalidInputError
from application_sdk.observability import get_logger
from application_sdk.storage.binding import create_store_from_binding
from application_sdk.storage.ops import download_file

logger = get_logger(__name__)

#: Prefix on credential field values that indicates the referenced file lives
#: in the customer's DEPLOYMENT Dapr object store binding (configured during
#: SDR setup). Intended for **non-secret companion files** that just happen
#: to be bundled into the same credential payload — see ``resolve_credential_file``.
OBJECT_STORE_PREFIX = "objectstore://"


def parse_credentials_extra(credentials: dict[str, Any]) -> dict[str, Any]:
    """Parse the 'extra' field from credentials, handling both string and dict inputs.

    Args:
        credentials: Credentials dictionary containing an 'extra' field.

    Returns:
        Parsed extra field as a dictionary.

    Raises:
        CommonError: If the extra field contains invalid JSON.
    """
    extra: str | dict[str, Any] = credentials.get("extra", {})

    if isinstance(extra, str):
        try:
            return json.loads(extra)
        except json.JSONDecodeError as e:
            raise InvalidInputError(
                message=f"Invalid JSON in credentials extra field: {e}",
                field="extra",
                cause=e,
            ) from e

    return extra


async def resolve_credential_file(
    value: str | None,
    filename: str,
    dest_dir: str = os.path.join(TEMPORARY_PATH, "credential_files"),
) -> str | None:
    """Resolve a credential-payload file field to a local file path.

    A "credential payload" in Atlan can carry both true secrets (passwords,
    keytabs, private keys) and non-secret companion files (krb5.conf, public
    CA certificates, kerberos realm configuration) that the connector also
    needs at runtime. This helper picks the right delivery mechanism for each
    file based on the format of ``value``.

    Three input formats are accepted, in priority order:

    1. **Atlan object-store reference** (file uploaded via the UI file picker):
       ``{"key": "workflow_file_upload/...", "rawName": "...", "extension": "..."}``
       The file was uploaded through the Atlan UI to Atlan's Dapr-backed
       upload object store. Used for both secrets (small keytabs) and
       non-secret companion files when the customer is happy to push the
       file through Atlan's hosted upload pipe.

    2. **Customer object-store path** (``objectstore://<key>``):
       e.g. ``"objectstore://kerberos/krb5.conf"``. The file already lives
       in the customer's own bucket — the same one wired up as their
       ``DEPLOYMENT_OBJECT_STORE_NAME`` Dapr binding during SDR setup. The
       SDK streams it down via that existing binding at activity runtime.

       This branch is intended for **non-secret companion files** that
       ride alongside a true credential — e.g. a Kerberos krb5.conf or a
       publicly-signed CA certificate. These files don't need
       secret-manager-grade controls, but they also don't need to be
       transferred through Atlan's infrastructure when the customer
       already has a perfectly good object store in their environment.

       Concrete benefits: no file-size ceiling (obstore streams chunks to
       disk), no new credentials to manage (binding auth is already
       configured), and the file content never traverses Atlan — only the
       path string does.

       **Not** intended for true secrets. Anything sensitive (passwords,
       keytabs, private keys) belongs in the secret-store branch (#3
       below) so it benefits from secret-manager controls (audit, rotation,
       break-glass). Use this branch only for the non-secret companion
       files that ship alongside a credential.

    3. **Base64-encoded file content** (raw string, no prefix):
       ``"BQIAAAABAAoASElWRS5MT0NBTA..."``. Used for **true secrets** — the
       customer base64-encodes the file, stores it as a value in their
       secret manager (AWS Secrets Manager / Azure Key Vault / GCP Secret
       Manager / K8s Secret), and the credential vault resolves the
       reference via ``SecretStore.get_credentials()`` + Dapr at activity
       runtime. The SDK sees the resolved base64 content here and decodes
       it to disk. Bounded by the customer secret manager's value-size cap
       (typically 1–64 KB depending on provider).

    Args:
        value:    Raw credential field value — JSON object-store reference,
                  an ``objectstore://`` prefixed key, or a raw base64-encoded
                  string. Returns ``None`` if empty.
        filename: Destination filename used for the base64 and ``objectstore://``
                  branches (e.g. ``"keytab.keytab"``, ``"krb5.conf"``,
                  ``"ca_cert.pem"``). Ignored for the Atlan upload branch —
                  the filename there is derived from the upload key.
        dest_dir: Directory to write or download the file into. Defaults to
                  ``<TEMPORARY_PATH>/credential_files``.

    Returns:
        Absolute path to the resolved file on disk, or ``None`` if ``value``
        is empty or resolution fails.
    """
    if not value:
        return None

    stripped = value.strip()

    # 1. Atlan upload object store — JSON reference from the UI file picker
    try:
        parsed = orjson.loads(value)
        if isinstance(parsed, dict) and ("key" in parsed or "fileKey" in parsed):
            return await download_file_from_upload_response(value)
    except (orjson.JSONDecodeError, TypeError):
        pass

    # 2. Customer's DEPLOYMENT object store — explicit objectstore:// prefix.
    #    Intended for non-secret companion files (krb5.conf, public CA certs)
    #    bundled with the credential. See docstring for details.
    if stripped.startswith(OBJECT_STORE_PREFIX):
        key = stripped[len(OBJECT_STORE_PREFIX) :]
        # Reject empty keys, absolute paths, and path-traversal segments
        if not key or key.startswith("/") or ".." in key.split("/"):
            logger.error(
                "Invalid object store key (empty / absolute / contains '..'): filename=%s",
                filename,
            )
            return None
        try:
            os.makedirs(dest_dir, exist_ok=True)
            file_path = os.path.join(dest_dir, filename)
            store = create_store_from_binding(DEPLOYMENT_OBJECT_STORE_NAME)
            await download_file(key, file_path, store=store)
            logger.info(
                "Resolved credential file from customer object store: key=%s path=%s",
                key,
                file_path,
            )
            return file_path
        except Exception:
            logger.error(
                "Failed to download credential file from customer object store: key=%s filename=%s",
                key,
                filename,
                exc_info=True,
            )
            return None

    # 3. Base64-encoded file content — decode and write to disk
    try:
        os.makedirs(dest_dir, exist_ok=True)
        file_path = os.path.join(dest_dir, filename)
        decoded_bytes = base64.b64decode(stripped, validate=True)
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
