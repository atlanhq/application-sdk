"""Credential parsing utilities."""

import base64
import os
import shutil
import tempfile
import threading
import warnings
from typing import Literal, overload

import orjson

from application_sdk._context_errors import ObjectStoreNotConfiguredError
from application_sdk._install import api_only_install, worker_only_missing
from application_sdk._logging import get_logger
from application_sdk._runtime.offload import run_in_thread
from application_sdk.common.utils import download_file_from_upload_response
from application_sdk.constants import DEPLOYMENT_OBJECT_STORE_NAME, TEMPORARY_PATH
from application_sdk.credentials.extra import (  # noqa: F401 — re-exported; ships in the api distribution
    parse_credentials_extra,
)

logger = get_logger(__name__)

#: Prefix on credential field values that indicates the referenced file lives
#: in the customer's DEPLOYMENT Dapr object store binding (configured during
#: SDR setup). Intended for **non-secret companion files** that just happen
#: to be bundled into the same credential payload — see ``resolve_credential_file``.
OBJECT_STORE_PREFIX = "objectstore://"


_DEFAULT_CREDENTIAL_FILE_DIR = os.path.join(TEMPORARY_PATH, "credential_files")


#: Only :func:`credential_dir` holds this, so only it can make a CredentialDir.
_ISSUED = object()

#: Paths :func:`credential_dir` made in this process and nothing has removed
#: yet. A CredentialDir is honoured only if its path is here, so a forged one
#: (``str.__new__``) around any other path — a shared directory, a symlink —
#: is refused without relying on platform permission bits.
_issued_dirs: set[str] = set()
_issued_dirs_lock = threading.Lock()


def _forget_credential_dir(path: str) -> None:
    """Drop *path* from the issued set once its directory has been removed.

    Called by every scope that removes a credential directory, so a later
    directory recreated at the same path is never mistaken for the SDK's.
    """
    with _issued_dirs_lock:
        _issued_dirs.discard(path)


class CredentialDir(str):
    """A private directory for credential files, made by :func:`credential_dir`.

    A ``str`` (its path), so it works anywhere a path does: ``os.path.join``,
    a client's config. The type
    is what tells :func:`resolve_credential_file` the directory is the SDK's
    own owner-only directory, so files go straight into it.

    Not constructible directly: ``CredentialDir("/tmp/shared")`` raises
    ``TypeError``. Use :func:`credential_dir`.
    """

    __slots__ = ()

    def __new__(cls, path: str, *, _issued_by: object = None) -> "CredentialDir":
        if _issued_by is not _ISSUED:
            # conformance: ignore[E012] constructing the type directly is a programming error, which TypeError signals for any constructor misuse; no run or source is involved
            raise TypeError(
                "CredentialDir is made by credential_dir(), not constructed directly"
            )
        return super().__new__(cls, path)


def credential_dir(base_dir: str | None = None) -> CredentialDir:
    """Create a private directory for credential files and return it.

    The directory is new, owner-only (0700), and unique to this call, so no
    other connection's files can land in it and other OS users can't read
    it. It is not hidden from code running as the same OS user (another
    connection in the same worker process can open it); that needs OS-level
    isolation. It is removed for you: inside a
    handler call, when the handler returns; inside a task, by
    ``cleanup_files`` when the run completes. Anywhere else (a script) the
    caller removes it.

    Pass it as ``dest_dir`` to :func:`resolve_credential_file` to put several
    files side by side — for example a client PSE and its ``cred_v2`` file,
    which SAP CommonCryptoLib reads from one ``SECUDIR``. Hand the directory
    to the library per connection; a process-wide setting such as an
    environment variable is shared by every overlapping connection, so it
    needs a lock held until the library has read the files::

        secudir = credential_dir()
        await resolve_credential_file(pse, "SAPSNCS.pse", secudir, raise_on_error=True)
        await resolve_credential_file(cred, "cred_v2", secudir, raise_on_error=True)

    Args:
        base_dir: Where to create the directory. Defaults to
            ``<TEMPORARY_PATH>/credential_files``.

    Returns:
        The new directory's path, as a :class:`CredentialDir`.
    """
    base = base_dir if base_dir is not None else _DEFAULT_CREDENTIAL_FILE_DIR
    os.makedirs(base, exist_ok=True)
    path = tempfile.mkdtemp(prefix="cred-", dir=base)
    with _issued_dirs_lock:
        _issued_dirs.add(path)
    _track_call_dir(path)
    return CredentialDir(path, _issued_by=_ISSUED)


@overload
async def resolve_credential_file(
    value: str | None,
    filename: str,
    dest_dir: CredentialDir | str | None = ...,
    *,
    raise_on_error: Literal[True],
) -> str: ...


@overload
async def resolve_credential_file(
    value: str | None,
    filename: str,
    dest_dir: CredentialDir | str | None = ...,
    *,
    raise_on_error: bool | None = ...,
) -> str | None: ...


async def resolve_credential_file(
    value: str | None,
    filename: str,
    dest_dir: CredentialDir | str | None = None,
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
        dest_dir: Where the file goes. Every file lands in a private,
                  owner-only (0700) directory, so two connections resolving
                  the same ``filename`` never share a path:

                  * ``None`` (default): a new directory from
                    :func:`credential_dir`, holding just this file.
                  * A :class:`CredentialDir`: the file goes straight into it,
                    so several files can sit side by side. A second file of
                    the same name in it raises ``FileExistsError``, also when
                    two resolutions of the name run concurrently. The name is
                    reserved (an empty 0600 file) while its content arrives.
                  * A plain ``str`` (deprecated): a new private directory is
                    created *inside* it, as ``credential_dir(dest_dir)``
                    would. Emits a ``DeprecationWarning``; removed in v4.0.

        raise_on_error: ``True`` to raise on failure, ``False`` to return
                  ``None``. ``None`` (the default) behaves as ``False`` and
                  emits a ``DeprecationWarning``; the default flips to
                  ``True`` in v4.0. A ``None`` for "resolution failed" is
                  indistinguishable from "no file configured", which is how
                  a failed CA-cert download became a connection with no
                  truststore.

    Returns:
        Path to the resolved file on disk, ``<private dir>/<filename>``, for
        the ``objectstore://`` and base64 branches. Without a
        :class:`CredentialDir`, it is a different path on every call. The
        file is owner-only (0600) and appears at that path complete, never
        half-written. Inside a handler call the directory is removed when the
        handler returns; inside a task, ``cleanup_files`` removes it when the
        run completes; anywhere else, the caller owns it. ``None`` if
        ``value`` is empty or resolution fails, unless ``raise_on_error`` is
        ``True``.

    Raises:
        CredentialNotFoundError: ``raise_on_error`` and ``value`` is empty or
            whitespace only.
        CredentialParseError: ``raise_on_error`` and ``value`` is an invalid
            ``objectstore://`` key or not valid base64, or ``filename`` is not
            a bare file name (it has a path separator, or is ``.``/``..``).
        FileExistsError: ``raise_on_error`` and ``dest_dir`` is a
            :class:`CredentialDir` that already holds ``filename``.
        PermissionError: ``raise_on_error`` and ``dest_dir`` is a
            :class:`CredentialDir` that :func:`credential_dir` did not make
            in this process, or whose directory has already been removed
            (for example one forged around a shared path).
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
    if dest_dir is not None and not isinstance(dest_dir, CredentialDir):
        warnings.warn(
            "resolve_credential_file() with a plain-string dest_dir is "
            "deprecated: the file goes into a new private directory inside "
            "it, not into dest_dir itself. Omit dest_dir, or pass a "
            "credential_dir() to put several files in one directory — "
            "plain strings are rejected in v4.0.",
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
    stripped: str, filename: str, dest_dir: CredentialDir | str | None
) -> str:
    """Resolve an ``objectstore://`` key or base64 content to a local path.

    Raises:
        CredentialParseError: An invalid ``objectstore://`` key, a
            ``filename`` that is not a bare file name, or content that is not
            valid base64.
    """
    from application_sdk.credentials.errors import (  # noqa: PLC0415 — circular: credentials/__init__.py loads sibling modules
        CredentialParseError,
    )

    # The file must land inside this call's own directory: a separator or a
    # dot segment would write it beside, or above, where cleanup looks.
    if not _is_bare_filename(filename):
        raise CredentialParseError(
            "Invalid credential file name (empty, '.', '..', or contains a "
            "path separator)",
            credential_name=filename,
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
        try:
            from application_sdk.infrastructure.context import (  # noqa: PLC0415 — lazy: worker-only module; this module ships in the api distribution
                get_infrastructure,
            )
            from application_sdk.storage.binding import (  # noqa: PLC0415 — lazy: storage imports obstore (heavy Rust ext) at module load, and this module sits on the workflow-sandbox import chain (credentials package init); pinned by the preflight gate's import-hygiene test
                create_store_from_binding,
            )
            from application_sdk.storage.ops import (  # noqa: PLC0415 — lazy: same reason as the binding import above
                download_file,
            )
        # conformance: ignore[E008] api-only install fallback: re-raised as a typed ObjectStoreNotConfiguredError, never hidden
        except ModuleNotFoundError as exc:
            if not worker_only_missing(exc):
                raise
            # api-only install (the API host): no object store to resolve from.
            raise ObjectStoreNotConfiguredError() from exc

        # Reuse the deployment store startup built from the same binding
        # (with its fetched secrets); build one only outside a started app.
        infra = get_infrastructure()
        if infra is not None and infra.storage is not None:
            store = infra.storage
        else:
            store = create_store_from_binding(DEPLOYMENT_OBJECT_STORE_NAME)
        call_dir, owned = _call_dir_for(dest_dir)
        file_path = _reserve_file_path(call_dir, filename)
        try:
            # download_file stages into an mkstemp (0600) file and publishes
            # it over the reservation with os.replace, so the file is
            # owner-only and never partial.
            await download_file(key, file_path, store=store)
        except BaseException:
            await run_in_thread(_discard, call_dir, file_path, owned)
            raise
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
    call_dir, owned = _call_dir_for(dest_dir)
    file_path = _reserve_file_path(call_dir, filename)
    try:
        _write_owner_only(call_dir, file_path, decoded_bytes)
    except BaseException:
        await run_in_thread(_discard, call_dir, file_path, owned)
        raise
    logger.info(
        "Resolved credential file from base64 content: path=%s filename=%s",
        file_path,
        filename,
    )
    return file_path


def _is_bare_filename(filename: str) -> bool:
    """Whether *filename* names a file, not a path: no separator, no dot segment."""
    if filename in ("", ".", ".."):
        return False
    separators = {"/", os.sep} | ({os.altsep} if os.altsep else set())
    return not any(sep in filename for sep in separators)


def _call_dir_for(dest_dir: CredentialDir | str | None) -> tuple[str, bool]:
    """The private directory this call writes into, and whether it made it.

    A :class:`CredentialDir` is used as is, provided :func:`credential_dir`
    issued it in this process and it has not been removed (its scope removes
    it). Otherwise a new one is made — under the default base, or inside a
    legacy plain-string ``dest_dir`` — and this call removes it again if the
    write fails.

    Raises:
        PermissionError: A CredentialDir that was not issued here, or whose
            directory has already been removed.
    """
    if isinstance(dest_dir, CredentialDir):
        with _issued_dirs_lock:
            issued = str(dest_dir) in _issued_dirs
        if not issued:
            raise PermissionError(
                "Credential directory was not made by credential_dir() in this "
                "process, or has already been removed"
            )
        return dest_dir, False
    return credential_dir(dest_dir), True


def _reserve_file_path(call_dir: str, filename: str) -> str:
    """Claim ``call_dir/filename`` atomically, as an empty 0600 file.

    ``O_CREAT | O_EXCL`` makes the claim and the existence check one step, so
    of two concurrent resolutions of one name into a shared
    :class:`CredentialDir`, exactly one gets the path and the other gets
    ``FileExistsError``. Silently replacing the first file is how one
    connection ends up reading another's.
    """
    file_path = os.path.join(call_dir, filename)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(file_path, flags, 0o600)
    except FileExistsError as exc:
        raise FileExistsError(
            f"Credential directory already holds a file named {filename!r}"
        ) from exc
    os.close(fd)
    return file_path


def _discard(call_dir: str, file_path: str, owned: bool) -> None:
    """Undo a failed resolution: its whole directory if this call made it,
    else just the name it reserved in the caller's directory."""
    if owned:
        shutil.rmtree(call_dir, ignore_errors=True)
        if not os.path.lexists(call_dir):
            _forget_credential_dir(call_dir)
    elif os.path.lexists(file_path):
        os.remove(file_path)


def _write_owner_only(call_dir: str, file_path: str, data: bytes) -> None:
    """Write *data* to *file_path* in *call_dir* as an owner-only (0600) file.

    The bytes go to a ``mkstemp`` temp file (``O_EXCL``, 0600) first and are
    published over the reservation with ``os.replace``, so *file_path* never
    holds a partial file. The temp file is removed if the write fails.
    """
    fd, tmp_path = tempfile.mkstemp(dir=call_dir, prefix=".", suffix=".partial")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp_path, file_path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def _track_call_dir(call_dir: str) -> None:
    """Register *call_dir* with whatever scope will remove it.

    The narrowest scope wins. Inside a handler call (HTTP, SDR, the preflight
    gate) the handler context removes it when the handler returns. Otherwise,
    inside a task, the run does, via ``cleanup_files``: ``app.base`` owns both
    the run state and the task-context probe, so this module never touches
    the execution runtime itself. Elsewhere (scripts) the caller owns it.
    """
    from application_sdk._context_errors import (  # noqa: PLC0415 — lazy: keeps the module-level import list unchanged for the api distribution
        AppContextError,
    )
    from application_sdk.handler.context import (  # noqa: PLC0415 — lazy: the handler package is not needed to import this module
        get_handler_context,
    )

    handler_ctx = get_handler_context()
    if handler_ctx is not None:
        handler_ctx._track_credential_dir(call_dir, _forget_credential_dir)
        return

    try:
        from application_sdk.app.base import (  # noqa: PLC0415 — lazy: worker-only module, and circular (app.base imports the credentials package)
            _track_credential_dir,
        )
    # conformance: ignore[E008] api-only install (the API host): it has no tasks, so there is no run to track the directory for
    except ModuleNotFoundError:
        if not api_only_install():
            raise
        return
    try:
        _track_credential_dir(call_dir)
    # conformance: ignore[E002] not inside a task: the caller owns the directory, as documented on resolve_credential_file
    except AppContextError:
        return
