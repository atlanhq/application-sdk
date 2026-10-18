"""Credential parsing utilities."""

import base64
import json
import os
import warnings
from typing import Any, Literal, overload

import orjson

from application_sdk.common.utils import download_file_from_upload_response
from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME, TEMPORARY_PATH
from application_sdk.observability import get_logger

logger = get_logger(__name__)

#: Prefix on credential field values that indicates the referenced file lives
#: in the customer's DEPLOYMENT Dapr object store binding (configured during
#: SDR setup). Intended for **non-secret companion files** that just happen
#: to be bundled into the same credential payload — see ``resolve_credential_file``.
OBJECT_STORE_PREFIX = "objectstore://"


def parse_credentials_extra(
    credentials: dict[str, Any], *, strict: bool = True
) -> dict[str, Any]:
    """Decode the ``extra`` field of a credential dict.

    ``extra`` is stored in two legal shapes — a nested object, or that same
    object serialized to a JSON string — because its producers (the Atlan UI,
    Heracles, Argo templates, agent JSON) straddle the v2/v3 credential
    contract boundary. A reader that handles only one shape silently drops
    whatever the other shape carried, so the credential-resolution and
    gate-flattening paths share this decoder rather than shape-matching
    locally: ``clients/sql.py`` and
    :func:`~application_sdk.handler.contracts.flatten_credentials_to_pairs`.

    It is **not** yet the only reader of ``extra`` in the SDK. These still
    parse it independently and remain to be routed through here:

    * ``storage/cloud.py`` — decodes both shapes, but with its own error type
      and an additional ``extras`` alias key.
    * ``credentials/agent.py`` (secret-reference collection and substitution)
      and ``infrastructure/_dapr/credential_vault.py`` (secret substitution)
      — ``isinstance(extra, dict)`` only, so a JSON-string ``extra`` is
      skipped rather than decoded.

    Always returns a mapping. Absent, null, and empty ``extra`` are all "no
    extra" — handing back the raw ``None`` instead only moved the failure to
    an ``AttributeError`` on the caller's next ``.get()``.

    Args:
        credentials: Credential dict that may carry an ``extra`` field.
        strict: Policy for an ``extra`` that is present but unusable (not
            decodable, or not a JSON object). ``True`` — the runtime-client
            policy — raises: a connector cannot build a DSN without it, and
            a typed credential error beats a downstream ``AttributeError``.
            ``False`` — the flattening policy — returns ``{}`` instead, for
            callers that run where no one is positioned to distinguish a
            malformed credential from an absent one and so must never raise.

    Returns:
        The decoded ``extra`` object, or ``{}`` when it is absent (or
        unusable and ``strict`` is ``False``).

    Raises:
        CredentialParseError: ``strict`` is set and ``extra`` is present but
            is neither valid JSON nor a JSON object.
    """
    extra: Any = credentials.get("extra")

    # ``isinstance`` before the emptiness test: ``extra`` is arbitrarily typed
    # here, and a bare ``== ""`` on a container that overloads equality returns
    # a container rather than a bool, raising on the truthiness check.
    if extra is None or (isinstance(extra, str) and not extra):
        return {}

    if isinstance(extra, dict):
        return extra

    def _reject(message: str, cause: Exception | None = None) -> dict[str, Any]:
        if not strict:
            # Never silent: dropping ``extra`` costs the caller every
            # connection param stored inside it, and a lenient caller by
            # definition has no error to surface. The log line is the only
            # trace, so it must carry the reason — but never the value, which
            # is credential material.
            logger.warning(
                "Dropping unusable credentials extra field, continuing without "
                "it (any connection params stored inside it will be absent): %s",
                message,
                exc_info=cause is not None,
            )
            return {}
        from application_sdk.credentials.errors import (  # noqa: PLC0415
            CredentialParseError,
        )

        raise CredentialParseError(
            message=message, credential_name="extra", cause=cause
        ) from cause

    if isinstance(extra, str):
        try:
            extra = orjson.loads(extra)
        except json.JSONDecodeError as e:
            # conformance: ignore[E007] not swallowed — _reject raises under strict and logs a WARNING with exc_info otherwise; the rule is lexical and cannot follow into the helper
            return _reject("Invalid JSON in credentials extra field", e)

    if not isinstance(extra, dict):
        return _reject(
            "Credentials extra field is not a JSON object "
            f"(decoded to {type(extra).__name__})"
        )

    return extra


_DEFAULT_CREDENTIAL_FILE_DIR = os.path.join(TEMPORARY_PATH, "credential_files")


@overload
async def resolve_credential_file(
    value: str | None,
    filename: str,
    dest_dir: str = ...,
    *,
    raise_on_error: Literal[True],
) -> str: ...


@overload
async def resolve_credential_file(
    value: str | None,
    filename: str,
    dest_dir: str = ...,
    *,
    raise_on_error: bool | None = ...,
) -> str | None: ...


async def resolve_credential_file(
    value: str | None,
    filename: str,
    dest_dir: str = _DEFAULT_CREDENTIAL_FILE_DIR,
    *,
    raise_on_error: bool | None = None,
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
                  string. Empty or whitespace-only means the field was
                  not supplied.
        filename: Destination filename used for the base64 and ``objectstore://``
                  branches (e.g. ``"keytab.keytab"``, ``"krb5.conf"``,
                  ``"ca_cert.pem"``). Ignored for the Atlan upload branch —
                  the filename there is derived from the upload key.
        dest_dir: Directory to write or download the file into. Defaults to
                  ``<TEMPORARY_PATH>/credential_files``.

        raise_on_error: ``True`` to raise on failure, ``False`` to return
                  ``None``. ``None`` (the default) behaves as ``False`` and
                  emits a ``DeprecationWarning``; the default flips to
                  ``True`` in v4.0. A ``None`` for "resolution failed" is
                  indistinguishable from "no file configured", which is how
                  a failed CA-cert download became a connection with no
                  truststore.

    Returns:
        Absolute path to the resolved file on disk. ``None`` if ``value`` is
        empty or resolution fails, unless ``raise_on_error`` is ``True``.

    Raises:
        CredentialNotFoundError: ``raise_on_error`` and ``value`` is empty or
            whitespace only.
        CredentialParseError: ``raise_on_error`` and ``value`` is an invalid
            ``objectstore://`` key or not valid base64.
        Exception: Failures downloading an Atlan upload reference propagate
            in either mode, as they always have; with ``raise_on_error``,
            failures downloading from the customer object store or writing
            the file propagate too, with their own types.
    """
    if raise_on_error is None:
        warnings.warn(
            "resolve_credential_file() without raise_on_error is deprecated: "
            "it returns None for a failed resolution as well as an empty "
            "value. Pass raise_on_error=True for a typed error, or "
            "raise_on_error=False to keep None — the default flips to True "
            "in v4.0.",
            DeprecationWarning,
            stacklevel=2,
        )

    if not value or not value.strip():
        if raise_on_error:
            from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
                CredentialNotFoundError,
            )

            raise CredentialNotFoundError(filename)
        return None

    # 1. Atlan upload object store — JSON reference from the UI file picker.
    #    Download failures here have always propagated, in both modes.
    if _is_upload_reference(value):
        return await download_file_from_upload_response(value)

    try:
        return await _resolve_credential_file_content(value.strip(), filename, dest_dir)
    # conformance: ignore[E004] re-raised unchanged under raise_on_error; otherwise the legacy None contract, logged with the cause
    except Exception:
        if raise_on_error:
            raise
        logger.error(
            "Failed to resolve credential file: filename=%s", filename, exc_info=True
        )
        return None


def _is_upload_reference(value: str) -> bool:
    """Whether *value* is an Atlan upload-store reference (JSON with a file key)."""
    try:
        parsed = orjson.loads(value)
    # conformance: ignore[E002] value isn't a JSON file-reference; the caller falls through to the objectstore:// check
    except (orjson.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and ("key" in parsed or "fileKey" in parsed)


async def _resolve_credential_file_content(
    stripped: str, filename: str, dest_dir: str
) -> str:
    """Resolve an ``objectstore://`` key or base64 content to a local path.

    Raises:
        CredentialParseError: An invalid ``objectstore://`` key, or content
            that is not valid base64.
    """
    from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
        CredentialParseError,
    )

    # 2. Customer's DEPLOYMENT object store — explicit objectstore:// prefix.
    #    Intended for non-secret companion files (krb5.conf, public CA certs)
    #    bundled with the credential. See resolve_credential_file's docstring.
    if stripped.startswith(OBJECT_STORE_PREFIX):
        key = stripped[len(OBJECT_STORE_PREFIX) :]
        # Reject empty keys, absolute paths, and path-traversal segments
        if not key or key.startswith("/") or ".." in key.split("/"):
            raise CredentialParseError(
                "Invalid object store key for credential file (empty, "
                "absolute, or contains '..')",
                credential_name=filename,
            )
        os.makedirs(dest_dir, exist_ok=True)
        file_path = os.path.join(dest_dir, filename)
        from application_sdk.storage.binding import (  # noqa: PLC0415 — lazy: storage imports obstore (heavy Rust ext) at module load, and this module sits on the workflow-sandbox import chain (credentials package init); pinned by the preflight gate's import-hygiene test
            create_store_from_binding,
        )
        from application_sdk.storage.ops import (  # noqa: PLC0415 — lazy: same reason as the binding import above
            download_file,
        )

        store = create_store_from_binding(DEPLOYMENT_OBJECT_STORE_NAME)
        await download_file(key, file_path, store=store)
        logger.info(
            "Resolved credential file from customer object store: key=%s path=%s",
            key,
            file_path,
        )
        return file_path

    # 3. Base64-encoded file content — decode and write to disk
    try:
        decoded_bytes = base64.b64decode(stripped, validate=True)
    except ValueError as exc:
        raise CredentialParseError(
            "Credential file content is not valid base64",
            credential_name=filename,
            cause=exc,
        ) from exc
    os.makedirs(dest_dir, exist_ok=True)
    file_path = os.path.join(dest_dir, filename)
    with open(file_path, "wb") as f:
        f.write(decoded_bytes)
    logger.info(
        "Resolved credential file from base64 content: path=%s filename=%s",
        file_path,
        filename,
    )
    return file_path
