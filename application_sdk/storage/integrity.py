"""Byte-level integrity validation for every SDK file transfer (FND-306).

Why this module exists
----------------------
A producing app that dies mid-write — the motivating case was ``ENOSPC``
during a carry-forward state write — leaves a *truncated* artifact in the
object store and still reports success. The consuming app then downloads that
file on every retry and fails deep inside its parser (``Malformed JSON …
unexpected end of data``), burning the whole retry budget on a deterministically
corrupt input and attributing the failure to the consumer instead of the
producer.

Nothing in that chain is a parser problem, so no parser can fix it. The check
belongs at the byte layer both apps already share: the transfer primitives in
:mod:`application_sdk.storage.ops` and
:mod:`application_sdk.storage.chunked`. Every upload and download in the SDK —
``App.upload``/``App.download``, ``FileReference`` persist/materialise, batch
prefix transfers, incremental-state fetches, the writer chunk uploads — funnels
through those three functions, so validating there is what makes the guarantee
uniform rather than per-call-site.

The protocol
------------
**Upstream (upload).** ``upload_file`` records the local file's size before it
starts reading and compares it against the bytes it actually streamed. A file
that *shrank* under the reader was truncated mid-upload, so what landed in the
store is a prefix of the artifact the caller asked for →
:class:`~application_sdk.storage.errors.StorageIntegrityError`. (A file that
*grew* is not an error: the reader consumed everything up to EOF, so the object
is self-consistent — it is just newer than the caller's stat.) After the writer
closes, a HEAD confirms the store actually recorded the bytes we sent; a
mismatch is a dependency failure (``StorageError``), not a data defect. Finally
the SHA-256 computed during the upload pass is written to a ``{key}.sha256``
sidecar, which is what gives a *different process* something to verify against.

**Downstream (download).** Every download compares the bytes written to disk
against the size the store declared for the object. When a sidecar exists (or
the caller supplies the expected digest via ``expected_sha256=``), the
downloaded content is hashed and compared: a mismatch means the object is
corrupt at source and is raised as a non-retryable ``StorageIntegrityError``
naming the key, the expected digest, and the observed one.

Both checks are governed by ``ATLAN_STORAGE_VERIFY_TRANSFERS`` (see
:data:`~application_sdk.constants.STORAGE_VERIFY_TRANSFERS`); sidecar *emission*
is separately governed by ``ATLAN_STORAGE_WRITE_SIDECARS``.

What this does NOT prove
------------------------
The sidecar attests to **the bytes the SDK read at upload time**, not to the
artifact being semantically complete. A producer that wrote a truncated file to
disk and *then* handed it to ``upload_file`` gets a sidecar recording the
truncated content as the expected digest, and every downstream check passes: as
far as the transfer layer can tell, exactly the intended bytes moved.

Closing that half needs the producer to never leave partial output at an
artifact's real name in the first place, which is
:mod:`application_sdk.common.atomic` (FND-318): every SDK writer stages its
bytes elsewhere and renames them into place, so a write that fails leaves the
final path either absent or holding the previous complete artifact — there is
nothing partial for this module to faithfully record. That is the producer-side
half, and it belongs in the SDK for the same reason this module does: the SDK
owns the writers apps actually use.

What the machinery here buys *on top of* that is narrower and still worth
having:

* a file truncated *while the upload is reading it* is caught (local-shrink);
* any corruption after a good upload — a rewrite, a partial restore, a
  half-completed multipart — is caught on the next download instead of
  surfacing as a parser error in whatever reads it next;
* the failure is attributed to the artifact and its producing key, rather than
  to the consumer that happened to open it.

This module holds no store state: it is the single definition of the sidecar
key convention, the digest helpers, and the comparison predicates that
``ops``/``chunked`` call. ``storage.batch``, ``storage.transfer`` and
``storage.reference`` re-export from here rather than keeping their own copies.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from application_sdk._runtime.offload import run_in_thread
from application_sdk.observability.logger_adaptor import get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from obstore.store import ObjectStore

    from application_sdk.storage.ops import BoundStore

logger = get_logger(__name__)

#: Suffix of the SHA-256 sidecar written alongside every uploaded object.
SIDECAR_SUFFIX = ".sha256"

#: Suffix of the resumable-download checkpoint written by ``storage.chunked``.
#: Listed here so the sidecar lookup can skip keys that are themselves
#: SDK-internal bookkeeping rather than user artifacts.
_TRANSFER_STATE_SUFFIX = ".transfer-state"


def sidecar_key(key: str) -> str:
    """Object-store key of the SHA-256 sidecar written alongside *key*."""
    return key + SIDECAR_SUFFIX


def is_sidecar_key(key: str) -> bool:
    """Return ``True`` when *key* is a SHA-256 sidecar rather than a data object."""
    return key.endswith(SIDECAR_SUFFIX)


def _is_sha256_hex(value: str) -> bool:
    """Return ``True`` when *value* is a well-formed SHA-256 hex digest."""
    return len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value)


def _is_internal_key(key: str) -> bool:
    """Return ``True`` for SDK bookkeeping objects that never carry a sidecar.

    Looking up ``{key}.sha256.sha256`` would be a guaranteed-miss round trip on
    every sidecar write, and the transfer-state checkpoint is local-only
    bookkeeping whose content is rewritten in place.
    """
    return key.endswith((SIDECAR_SUFFIX, _TRANSFER_STATE_SUFFIX))


def verification_enabled(verify: bool | None) -> bool:
    """Resolve a per-call ``verify`` flag against the deployment kill-switch.

    ``None`` (the default at every call site) follows
    ``ATLAN_STORAGE_VERIFY_TRANSFERS``; an explicit ``True``/``False`` wins so
    a caller that knows better — a probe writing throwaway bytes, a test — can
    opt out without touching the environment.
    """
    if verify is not None:
        return verify
    from application_sdk.constants import STORAGE_VERIFY_TRANSFERS  # noqa: PLC0415

    return STORAGE_VERIFY_TRANSFERS


def sidecar_writes_enabled(write_sidecar: bool | None) -> bool:
    """Resolve a per-call ``write_sidecar`` flag against the kill-switch."""
    if write_sidecar is not None:
        return write_sidecar
    from application_sdk.constants import STORAGE_WRITE_SIDECARS  # noqa: PLC0415

    return STORAGE_WRITE_SIDECARS


def _sha256_file_sync(path: Path) -> str:
    """Compute the SHA-256 hex digest of a local file (streaming, 1 MiB reads)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def sha256_file(path: Path) -> str:
    """Hash a local file in a worker thread.

    :func:`_sha256_file_sync` reads and digests the whole file with no
    ``await``; calling it on the event loop blocks the loop for the full
    read+hash. A blocked loop cannot run the SDK's auto-heartbeat coroutine, so
    a large-file verification would heartbeat-time-out the activity even while
    making progress (ADR-0010, P031).

    ``run_in_thread`` also dispatches to the SDK's dedicated blocking pool
    rather than asyncio's default executor, which Temporal's own SDK uses for
    internal scheduling — sharing that pool risks exhausting it.
    """
    return await run_in_thread(_sha256_file_sync, path)


async def read_expected_digest(
    store: BoundStore | ObjectStore | None,
    key: str,
    *,
    sidecar_present: bool | None = None,
) -> str | None:
    """Return the digest recorded in ``{key}.sha256``, or ``None`` if absent.

    Absent is the normal case for an object a non-SDK producer wrote, or one
    uploaded before sidecars existed, so it is not an error — the caller simply
    has nothing to verify against and proceeds unverified.

    A sidecar that cannot be *read* (permission denied, I/O error) is logged at
    WARNING and treated as absent. Making verification a hard dependency on a
    second key being readable would let an IAM policy that covers data objects
    but not sidecars break every download in the fleet — a worse failure than
    the unverified transfer it would be protecting against. The WARNING is what
    keeps the lost coverage visible.

    Args:
        store: Store holding the object.
        key: Data-object key (**not** the sidecar key).
        sidecar_present: What a prior listing already established about the
            sidecar. ``False`` short-circuits with no request at all; ``True``
            skips straight to the GET. ``None`` (no prior knowledge) issues a
            HEAD first — a plain GET on a missing key burns the obstore retry
            budget before it reports the 404, which is expensive at the
            one-per-file rate a prefix download hits it.
    """
    if _is_internal_key(key) or sidecar_present is False:
        return None
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: ops imports this module at top level
        _get_bytes,
        exists,
    )

    skey = sidecar_key(key)
    try:
        if sidecar_present is None and not await exists(skey, store, normalize=False):
            return None
        raw = await _get_bytes(skey, store, normalize=False)
    # conformance: ignore[E002] unreadable sidecar degrades to unverified-with-WARNING by design; see docstring
    except Exception:
        logger.warning(
            "Could not read the integrity sidecar '%s' — continuing without "
            "verifying the transfer of '%s'",
            skey,
            key,
            exc_info=True,
        )
        return None
    if raw is None:
        return None

    digest = raw.decode(errors="replace").strip()
    if not _is_sha256_hex(digest):
        # Only our own format is trustworthy as an expectation. Anything else
        # is an object that happens to sit at this key — a stray file, a
        # half-written sidecar, another tool's metadata. Comparing against it
        # would fail a *healthy* artifact with a non-retryable corruption
        # error, which is a worse outcome than not verifying this one file.
        logger.warning(
            "Integrity sidecar '%s' does not contain a SHA-256 hex digest "
            "(%d chars) — continuing without verifying the transfer of '%s'",
            skey,
            len(digest),
            key,
        )
        return None
    # Lowercased to match ``hashlib.hexdigest()``. Hex is case-insensitive, so
    # an uppercase sidecar describes the same bytes — comparing it raw would
    # report a healthy artifact as corrupt.
    return digest.lower()


async def write_digest_sidecar(
    store: BoundStore | ObjectStore | None,
    key: str,
    digest: str,
) -> None:
    """Write ``{key}.sha256`` so a downstream reader can verify *key*.

    Best-effort: a store that accepted the object but rejects the sidecar
    should not fail the upload — the object is intact, it just cannot be
    verified later. The failure is logged at WARNING so the loss of coverage
    is visible rather than silent.
    """
    if _is_internal_key(key):
        return
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: ops imports this module at top level
        _put,
    )

    try:
        await _put(sidecar_key(key), digest.encode(), store, normalize=False)
    # conformance: ignore[E002] best-effort sidecar; the object itself uploaded fine and the failure is logged with its cause
    except Exception:
        logger.warning(
            "Integrity sidecar write failed for key '%s' — the object uploaded "
            "successfully but downstream readers will not be able to verify it",
            key,
            exc_info=True,
        )


def check_transfer_size(
    op: str,
    key: str,
    *,
    expected: int,
    actual: int,
    local_path: str | Path | None = None,
) -> None:
    """Assert a transfer moved exactly *expected* bytes.

    A shortfall means the transfer ended early without the underlying client
    raising — a truncated stream, a completion the backend recorded partially,
    or an object the store silently dropped. That is a dependency-level
    failure rather than a data defect: the source is presumed fine and the
    operation is worth retrying, so it raises the retryable
    :class:`~application_sdk.storage.errors.StorageError`.

    Raises:
        StorageError: If *actual* differs from *expected*.
    """
    if actual == expected:
        return
    from application_sdk.storage.errors import StorageError  # noqa: PLC0415

    where = f" (local_path={local_path})" if local_path is not None else ""
    raise StorageError(
        f"Incomplete {op} for key '{key}'{where}: expected {expected} bytes, "
        f"transferred {actual}. The transfer ended early — the object in the "
        f"store and the local copy do not agree on length.",
        key=key,
    )


def check_transfer_digest(
    op: str,
    key: str,
    *,
    expected: str,
    actual: str,
    local_path: str | Path | None = None,
) -> None:
    """Assert transferred content hashes to the digest its producer recorded.

    Raises:
        StorageIntegrityError: If the digests differ. Non-retryable — a
            byte-stable corrupt object fails identically on every attempt.
    """
    if actual == expected:
        return
    from application_sdk.storage.errors import StorageIntegrityError  # noqa: PLC0415

    raise StorageIntegrityError(
        f"Corrupt artifact detected on {op} of '{key}': content hashes to "
        f"{actual} but the digest recorded alongside it is {expected}. The "
        f"object in the store is not the object that was uploaded — it has "
        f"been rewritten, partially restored, or its upload completed only in "
        f"part. Retrying this step cannot help: the same bytes come back every "
        f"time. Re-run the step that produced '{key}'.",
        key=key,
        local_path=str(local_path) if local_path is not None else None,
        check="digest",
        expectation=expected,
        observed=actual,
    )


def check_local_file_stable(
    key: str,
    local_path: str | Path,
    *,
    declared_size: int,
    bytes_read: int,
) -> None:
    """Assert the local file did not shrink while it was being uploaded.

    *declared_size* is the size stat'd before the read began; *bytes_read* is
    what the upload pass actually consumed. Fewer bytes than declared means the
    file was truncated under the reader, so the object now in the store is a
    prefix of the artifact the caller asked to upload — exactly the partial
    file this validation exists to stop propagating.

    More bytes than declared is not an error: the reader consumed to EOF, so
    the uploaded object is a complete, self-consistent snapshot that is simply
    newer than the caller's stat (an actively-appended log file does this).

    Raises:
        StorageIntegrityError: If the file shrank mid-upload.
    """
    if bytes_read >= declared_size:
        return
    from application_sdk.storage.errors import StorageIntegrityError  # noqa: PLC0415

    raise StorageIntegrityError(
        f"Local file '{local_path}' shrank while it was being uploaded to "
        f"'{key}': {declared_size} bytes at open, {bytes_read} bytes read. The "
        f"object now in the store is a truncated prefix of the intended "
        f"artifact. Re-run the step that produced the file.",
        key=key,
        local_path=str(local_path),
        check="local_size",
        expectation=f"{declared_size} bytes",
        observed=f"{bytes_read} bytes",
    )
