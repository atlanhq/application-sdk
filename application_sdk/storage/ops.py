"""Low-level obstore CRUD operations.

All functions accept an optional ``ObjectStore`` instance and a key string.
When ``store`` is omitted (or ``None``), the store is resolved from the
current :func:`~application_sdk.infrastructure.context.get_infrastructure`
context — mirroring the v2 behaviour where the store was always transparent.

By default keys are normalised via :func:`normalize_key` before being sent
to the store, which mirrors the automatic normalisation that the deprecated
``ObjectStore.as_store_key()`` performed in v2 — so staging paths like
``./local/tmp/artifacts/...`` are transparently converted to store keys like
``artifacts/...`` and leading/trailing slashes are stripped.

Pass ``normalize=False`` to bypass normalisation and use the key exactly as
supplied (useful for callers that already hold a clean store key and want to
avoid the constant import overhead, or for tests that need exact key control).

Public helpers (small payloads)
--------------------------------
* ``put_json(key, obj)`` — serialise to JSON and write (configs, metadata)
* ``_get_bytes(key)``    — read bytes (sidecars, metadata, JSON configs)

Internal helpers
----------------
* ``_put(key, data)``    — write raw bytes (use ``put_json`` for JSON)

Streaming API (large files)
----------------------------
* ``upload_file(key, local_path)``   — streaming upload with adaptive multipart
* ``download_file(key, local_path)`` — streaming download with optional hash

All calls that previously went through the implicit singleton store now
resolve from the infrastructure context automatically.  Pass ``store=my_store``
to target a specific store instead.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import os
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import obstore
import orjson

# obstore-rs surfaces a typed exception hierarchy via obstore.exceptions; we
# detect it once at import time so callers don't pay the import cost on every
# error path.  Falls back to substring matching for older obstore versions
# that lack typed exceptions.
try:  # pragma: no cover — defensive import
    from obstore.exceptions import BaseError as _ObstoreBaseError
    from obstore.exceptions import NotFoundError as _ObstoreNotFoundError
    from obstore.exceptions import PreconditionError as _ObstorePreconditionError
except ImportError:  # conformance: ignore[E008,E009] optional dep obstore.exceptions; sentinel fallback for older versions  # pragma: no cover
    _ObstoreBaseError = None  # type: ignore[assignment,misc]
    _ObstoreNotFoundError = None  # type: ignore[assignment,misc]
    _ObstorePreconditionError = None  # type: ignore[assignment,misc]


if TYPE_CHECKING:
    from typing import Any

    from obstore.store import ObjectStore

    JsonValue = dict[str, Any] | list[Any] | str | int | float | bool | None

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)


class BoundStore:
    """An :class:`~obstore.store.ObjectStore` paired with per-write put attributes.

    Returned by :func:`~application_sdk.storage.binding.create_store_from_binding_with_put_attrs`
    (and similar helpers) when the Dapr binding specifies a ``storageClass`` or other
    per-write options.  Pass a ``BoundStore`` anywhere an ``ObjectStore`` is accepted:
    :func:`upload_file`, :func:`_put`, and the SDK I/O helpers automatically extract
    both the underlying store and its attributes without extra plumbing at the call site.
    """

    __slots__ = ("_put_attributes", "_store")

    def __init__(
        self,
        store: ObjectStore,
        put_attributes: dict[str, str] | None = None,
    ) -> None:
        self._store = store
        self._put_attributes = put_attributes

    @property
    def store(self) -> ObjectStore:
        """Underlying obstore instance."""
        return self._store

    @property
    def put_attributes(self) -> dict[str, str] | None:
        """Per-write put attributes (e.g. ``{"Storage-Class": "STANDARD_IA"}``)."""
        return self._put_attributes


def normalize_key(key: str) -> str:
    """Normalise a local or object-store path into a clean object-store key.

    Mirrors the behaviour of the deprecated ``ObjectStore.as_store_key()``
    to provide a smooth v2 → v3 migration path.  Accepts any of:

    * Local SDK staging paths (``./local/tmp/artifacts/...``) — the
      ``TEMPORARY_PATH`` prefix is stripped so callers get
      ``artifacts/...`` regardless of where the temp root is mounted.
    * Absolute paths (``/data/output.parquet`` → ``data/output.parquet``).
    * Already-relative store keys (``artifacts/foo/bar.jsonl``) — returned
      unchanged (normalisation is a no-op for clean keys).

    Backslashes are converted to forward slashes and leading/trailing slashes
    are stripped so the result is always a clean relative key.

    Args:
        key: Raw key, local path, or staging path to normalise.

    Returns:
        Normalised object-store key, or empty string for empty input.
    """
    if not key:
        return ""

    from application_sdk.constants import TEMPORARY_PATH  # noqa: PLC0415

    abs_path = os.path.abspath(key)
    abs_temp_path = os.path.abspath(TEMPORARY_PATH)
    try:
        common_path = os.path.commonpath([abs_path, abs_temp_path])
        if common_path == abs_temp_path:
            # Path is inside the staging area — strip the staging prefix.
            normalized = os.path.relpath(abs_path, abs_temp_path).replace(os.sep, "/")
        else:
            normalized = key.strip("/")
    except ValueError:  # conformance: ignore[E009] os.path.commonpath raises on mixed Windows drives; simple-strip fallback
        # os.path.commonpath raises on mixed Windows drives; fall back to simple strip.
        normalized = key.strip("/")

    normalized = normalized.replace("\\", "/").replace(os.sep, "/").strip("/")
    # os.path.relpath resolves the staging root itself to "."; treat as store root.
    return "" if normalized == "." else normalized


def _safe_join_under(root: Path | str, rel: str) -> Path:
    """Join *rel* under *root* and reject path-traversal escapes.

    S3-style keys use POSIX separators, so *rel* is split with
    :class:`~pathlib.PurePosixPath` before being joined to *root*. The
    candidate path is then resolved and compared against the resolved
    *root*; anything that escapes (``..`` segments, symlinks pointing
    outside, etc.) is rejected before the caller writes to disk.

    Args:
        root: Local destination directory.
        rel: Relative path derived from an object-store key.

    Returns:
        Resolved absolute :class:`~pathlib.Path` guaranteed to be inside
        *root*.

    Raises:
        StorageError: If *rel* resolves outside *root*.
    """
    from application_sdk.storage.errors import StorageError  # noqa: PLC0415

    resolved_root = Path(root).resolve()
    parts = PurePosixPath(rel.lstrip("/")).parts
    candidate = (resolved_root / Path(*parts)).resolve() if parts else resolved_root
    if not candidate.is_relative_to(resolved_root):
        raise StorageError(f"Path traversal detected in key: {rel!r}")
    return candidate


def _normalize_listing_prefix(prefix: str, normalize: bool) -> str:
    """Return *prefix* normalised for a listing call.

    Applies :func:`normalize_key` when *normalize* is ``True``, then ensures
    the result ends with ``"/"`` so prefix matching never bleeds into sibling
    directories.
    """
    if normalize and prefix:
        prefix = normalize_key(prefix)
        if prefix and not prefix.endswith("/"):
            prefix = prefix + "/"
    return prefix


def _resolve_store(store: BoundStore | ObjectStore | None) -> ObjectStore:
    """Return the underlying ObjectStore, resolving from infrastructure when None.

    Accepts a :class:`BoundStore` (unwraps it), a raw ``ObjectStore``, or ``None``
    (resolved from the infrastructure context).

    Raises:
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure context is set.
    """
    if store is not None:
        return store.store if isinstance(store, BoundStore) else store
    from application_sdk.infrastructure.context import (  # noqa: PLC0415
        get_infrastructure,
    )

    infra = get_infrastructure()
    if infra is None or infra.storage is None:
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            ObjectStoreNotProvidedError,
        )

        raise ObjectStoreNotProvidedError()
    return infra.storage


def _resolve_put_attributes(
    store: BoundStore | ObjectStore | None,
) -> dict[str, str] | None:
    """Return binding-level put attributes for *store*, or ``None``.

    Resolution order:
    1. ``BoundStore`` — return its embedded ``put_attributes`` directly.
    2. ``store is None`` — return ``infra.storage_put_attributes``.
    3. ``store is infra.storage`` — same as (2); handles callers that hold an
       explicit reference to the infra deployment store.
    4. ``store is infra.upstream_storage`` — return
       ``infra.upstream_storage_put_attributes``; covers SDR-mode App.upload.
    5. Any other explicit store (CloudStore, test stores, etc.) — ``None``.

    Uses identity (``is``) rather than equality because obstore stores are unhashable.
    """
    if isinstance(store, BoundStore):
        return store.put_attributes
    from application_sdk.infrastructure.context import (  # noqa: PLC0415
        get_infrastructure,
    )

    infra = get_infrastructure()
    if infra is None:
        return None
    if store is None or store is infra.storage:
        return infra.storage_put_attributes
    if infra.upstream_storage is not None and store is infra.upstream_storage:
        return infra.upstream_storage_put_attributes
    return None


def _is_azure_container_not_found(exc: BaseException) -> bool:
    """Return True when *exc* indicates an Azure container does not exist.

    Azure Blob Storage returns HTTP 404 with error code ``ContainerNotFound``
    when a write targets a container that has never been created.  This is
    distinct from a missing *blob* (``BlobNotFound``) and needs a separate,
    actionable error message so operators know to pre-create the container.

    Class-based detection runs first (obstore >=0.9 ``GenericError``); the
    substring fallback catches older obstore versions and future wording drift
    is caught by the recorded-error regression tests.
    """
    if _ObstoreBaseError is not None and isinstance(exc, _ObstoreBaseError):
        msg = str(exc).lower()
        return (
            "containernotfound" in msg
            or "the specified container does not exist" in msg
        )
    msg = str(exc).lower()
    return "containernotfound" in msg or "the specified container does not exist" in msg


def _azure_container_not_found_message(key: str) -> str:
    """Return the standard user-facing message for a missing Azure container."""
    return (
        "Azure container does not exist — v3 does not auto-create "
        "containers (v2 Dapr did); pre-create the container before "
        f"running (failed key: '{key}')"
    )


def _is_not_found(exc: BaseException) -> bool:
    """Return True if the exception indicates a missing key.

    Recognises:

    * Built-in :class:`FileNotFoundError` — what current obstore (>=0.9) raises
      for missing keys after the deprecation of
      ``obstore.exceptions.NotFoundError``.
    * :class:`obstore.exceptions.NotFoundError` — still emitted by older
      obstore versions and present in the type stubs for forward-compat.
    * Substring fallback (``"not found"``, ``"404"``, …) for generic obstore
      errors that surface only as ``GenericError`` with the underlying HTTP
      status in the message.

    Class-based detection runs first so we don't misclassify a generic
    ``GenericError("HTTP 503: 404 not in title")`` style flake.
    """
    if isinstance(exc, FileNotFoundError):
        return True
    if _ObstoreNotFoundError is not None and isinstance(exc, _ObstoreNotFoundError):
        return True
    msg = str(exc).lower()
    return (
        "not found" in msg
        or "no such file" in msg
        or "does not exist" in msg
        or "404" in msg
        or "key not found" in msg
    )


def _exc_class_name(exc: BaseException) -> str:
    """Return a stable short class name for structured-log error_class fields."""
    return type(exc).__name__


def _is_precondition(exc: BaseException) -> bool:
    """True when *exc* is an etag precondition failure (HTTP 412 / if_match miss).

    Raised by version-pinned range GETs when the remote object was rewritten
    between the initial HEAD/listing and the chunk fetch. Class-based detection
    first (obstore ``PreconditionError``), substring fallback for older versions.
    """
    if _ObstorePreconditionError is not None and isinstance(
        exc, _ObstorePreconditionError
    ):
        return True
    msg = str(exc).lower()
    return "precondition" in msg or "412" in msg


def _throughput_mbps(size_bytes: int, elapsed_ms: float) -> float | None:
    """Return MiB/s throughput, or ``None`` when unknown / instantaneous."""
    if elapsed_ms <= 0 or size_bytes <= 0:
        return None
    return round((size_bytes / (1024 * 1024)) / (elapsed_ms / 1000.0), 3)


def _classify_transfer_error(error_class: str | None, store_path: str) -> str:
    """Bucket a transfer failure into a coarse, queryable family.

    Timeouts (slow egress / stalled connections), not-found, permission, and
    everything else fail for different reasons and want different responses —
    a timeout says "look at throughput / the network", a permission error says
    "look at credentials". Pairing this with ``size_bytes`` on the failure event
    also separates a *stall* (≈0 bytes before a timeout) from *slow-but-moving*
    (many bytes before a timeout). (BLDX-1513)
    """
    if error_class is None:
        return "none"
    name = error_class.lower()
    # obstore surfaces both the overall-request timeout and a read_timeout stall
    # as a GenericError whose text mentions timeout; we can't split them from the
    # class name alone, so both land in "timeout" and size_bytes disambiguates.
    if "timeout" in name or "timedout" in name:
        return "timeout"
    if "notfound" in name or "not_found" in name:
        return "not_found"
    if "generic" in name:
        # GenericError is obstore's catch-all; the timeout variant is by far the
        # most common large-transfer failure, but don't assume — label it generic.
        return "generic"
    return "other"


def _record_transfer_metric(
    op: str,
    *,
    outcome: str,
    elapsed_ms: float | None,
    size_bytes: int | None,
    throughput_mibps: float | None,
    error_class: str | None,
) -> None:
    """Emit transfer metrics so throughput / failures are dashboardable + alertable.

    Best-effort: a missing or misconfigured metrics backend must never break a
    transfer. Metrics land on the same OTel / Prometheus / object-store rails as
    the rest of the SDK. The throughput histogram is the fleet-wide signal for
    "this tenant's egress is slow" — alert on it and a slow tenant surfaces
    before its workflows start failing, rather than after. (BLDX-1513)
    """
    try:
        from application_sdk.observability.metrics_adaptor import (  # noqa: PLC0415 — lazy: avoid import-time cycle observability<->storage
            MetricType,
            get_metrics,
        )

        metrics = get_metrics()
        family = _classify_transfer_error(error_class, "")
        labels: dict[str, str | int | float | bool] = {
            "storage_op": op,
            "outcome": outcome,
            "error_family": family,
        }
        metrics.record_metric(
            name="storage_transfer_total",
            value=1,
            metric_type=MetricType.COUNTER,
            labels=labels,
            description="Object-store transfers by op/outcome/error family",
            unit="count",
        )
        if size_bytes is not None and size_bytes >= 0:
            metrics.record_metric(
                name="storage_transfer_bytes",
                value=size_bytes,
                metric_type=MetricType.COUNTER,
                labels={"storage_op": op, "outcome": outcome},
                description="Bytes transferred to/from object store",
                unit="By",
            )
        if elapsed_ms is not None and elapsed_ms >= 0:
            metrics.record_metric(
                name="storage_transfer_duration_ms",
                value=round(elapsed_ms, 3),
                metric_type=MetricType.HISTOGRAM,
                labels={"storage_op": op, "outcome": outcome},
                description="Object-store transfer wall-clock duration",
                unit="ms",
            )
        if throughput_mibps is not None:
            metrics.record_metric(
                name="storage_transfer_throughput_mibps",
                value=throughput_mibps,
                metric_type=MetricType.HISTOGRAM,
                labels={"storage_op": op, "outcome": outcome},
                description="Object-store transfer throughput (MiB/s) — alert when p50 is abnormally low",
                unit="MiBy/s",
            )
    # conformance: ignore[E004] metrics are best-effort telemetry; a backend failure must never break a transfer — logged at debug and swallowed
    except Exception:
        logger.debug("Failed to record transfer metric (best-effort)", exc_info=True)


def _log_storage_event(
    level: int,
    op: str,
    store_path: str,
    *,
    outcome: str,
    elapsed_ms: float | None = None,
    size_bytes: int | None = None,
    error_class: str | None = None,
) -> None:
    """Emit a single structured per-attempt storage event.

    Fields are placed on ``extra`` so structured-log backends and pytest's
    caplog see them as record attributes; the human-readable message stays
    short for unstructured tail / grep workflows.
    """
    extra: dict[str, object] = {
        "storage_op": op,
        "store_path": store_path,
        "outcome": outcome,
    }
    tput: float | None = None
    if elapsed_ms is not None:
        extra["elapsed_ms"] = round(elapsed_ms, 3)
    if size_bytes is not None:
        extra["size_bytes"] = size_bytes
        if elapsed_ms is not None:
            tput = _throughput_mbps(size_bytes, elapsed_ms)
            if tput is not None:
                extra["throughput_mibps"] = tput
    if error_class is not None:
        extra["error_class"] = error_class
    msg = f"storage.{op} {outcome} path={store_path}"
    # Keys are bound into loguru record["extra"] and promoted to OTLP indexed
    # attributes by _build_extra_dict in logger_adaptor (all are in _KNOWN_EXTRA_KEYS).
    logger.log(level, msg, **extra)

    # Mirror the terminal event to metrics so throughput / failure rate are
    # dashboardable + alertable across the fleet (only for actual transfers).
    if op in ("download", "upload"):
        _record_transfer_metric(
            op,
            outcome=outcome,
            elapsed_ms=elapsed_ms,
            size_bytes=size_bytes,
            throughput_mibps=tput,
            error_class=error_class,
        )


def _log_transfer_progress(
    op: str,
    store_path: str,
    *,
    bytes_so_far: int,
    elapsed_ms: float,
    total_bytes: int | None = None,
) -> None:
    """Emit an in-progress heartbeat for a long-running upload / download.

    Answers "is this transfer stuck or just slow?" while it is still running —
    the per-attempt success/failure event only lands at the end. Only keys in
    ``_KNOWN_EXTRA_KEYS`` are placed on ``extra`` (so they promote to OTLP
    attributes); the human-readable total / percentage stays in the message
    text. (BLDX-1513)
    """
    extra: dict[str, object] = {
        "storage_op": op,
        "store_path": store_path,
        "outcome": "in_progress",
        "size_bytes": bytes_so_far,
        "elapsed_ms": round(elapsed_ms, 3),
    }
    tput = _throughput_mbps(bytes_so_far, elapsed_ms)
    if tput is not None:
        extra["throughput_mibps"] = tput
    if total_bytes:
        pct = 100.0 * bytes_so_far / total_bytes
        progress = f"{bytes_so_far}/{total_bytes} bytes ({pct:.0f}%)"
    else:
        progress = f"{bytes_so_far} bytes"
    msg = f"storage.{op} in_progress path={store_path} {progress}"
    logger.log(logging.INFO, msg, **extra)


# ---------------------------------------------------------------------------
# Resumable chunked-download state (BLDX-1523)
#
# A chunked download writes ranges at fixed offsets into a pre-allocated file.
# The sidecar below records which chunk indices have landed on disk, plus the
# identity of the remote object they came from (key / size / chunk size / etag).
# Its *existence* is the "incomplete" marker: it is deleted on success, so a
# data file without a state sidecar is always complete. On retry after a crash
# the download resumes by fetching only the missing chunk indices — valid only
# while the remote object is unchanged, which is what the etag match enforces.
# ---------------------------------------------------------------------------

_TRANSFER_STATE_SUFFIX = ".transfer-state"


def _transfer_state_path(path: Path) -> Path:
    """Sidecar path holding resumable-download state for *path*."""
    return Path(str(path) + _TRANSFER_STATE_SUFFIX)


def _load_transfer_state(state_path: Path) -> dict | None:
    """Load and validate a transfer-state sidecar; ``None`` if absent/corrupt.

    Corrupt or structurally invalid state must never break a download — the
    caller falls back to a fresh full download, which is always correct.
    """
    try:
        raw = state_path.read_bytes()
        state = orjson.loads(raw)
        if not isinstance(state, dict):
            return None
        if not isinstance(state.get("key"), str):
            return None
        if not isinstance(state.get("file_size"), int):
            return None
        if not isinstance(state.get("chunk_size"), int):
            return None
        if not isinstance(state.get("done"), list) or not all(
            isinstance(i, int) for i in state["done"]
        ):
            return None
        return state
    # conformance: ignore[E004] absent/corrupt checkpoint is an expected condition; falling back to a fresh download is always correct
    except Exception:
        return None


def _save_transfer_state(state_path: Path, state: dict) -> None:
    """Atomically persist transfer state (write temp + rename, fsync'd).

    fsync on the temp file is enough for the failure mode resume targets:
    process death (OOMKill / activity retry) preserves the kernel page cache,
    so data-file writes that precede this checkpoint are visible on retry.
    Node death loses the local volume entirely — resume is moot there and the
    etag check makes the resulting fresh download safe.
    """
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, orjson.dumps(state))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, state_path)


def _discard_transfer_state(path: Path) -> None:
    """Remove the data file and its state sidecar (best-effort)."""
    path.unlink(missing_ok=True)
    _transfer_state_path(path).unlink(missing_ok=True)


async def _list_items(
    store: ObjectStore,
    prefix: str | None,
    *,
    include_markers: bool = False,
) -> list[tuple[str, int, str | None]]:
    """Collect listing results under *prefix*, optionally filtering GCS directory markers.

    Makes a single listing operation (``obstore.list`` returns a native async
    ``ListStream`` that pages internally — no thread wrapping needed).  When *include_markers* is
    ``False``, two additional in-memory passes are applied: one to build the set of
    ancestor path segments, and one to filter out zero-byte objects whose path is one
    of those ancestors (the structural signature of a GCS-console "folder" marker).

    A zero-byte object is excluded when its path is a strict path-prefix of at least
    one other listed key — i.e. it acts as a parent directory for real files.

    Args:
        store: An obstore-compatible store instance.
        prefix: Key prefix, or ``None`` to list everything.
        include_markers: When ``True``, skip the directory-marker filter and return
            every object including zero-byte markers.  Use this when the caller must
            operate on *all* objects (e.g. ``delete_prefix``) so that no orphan
            objects are left behind on any store backend.

    Returns:
        ``(path, size, e_tag)`` tuples in listing order (``e_tag`` may be
        ``None`` on stores that don't provide one).  Directory markers are
        excluded unless *include_markers* is ``True``.
    """
    all_items: list[tuple[str, int, str | None]] = []
    async for batch in obstore.list(store, prefix=prefix):  # native async ListStream
        for item in batch:
            all_items.append((str(item["path"]), int(item["size"]), item.get("e_tag")))

    if include_markers:
        return all_items

    parent_dirs: set[str] = set()
    for path, _, _ in all_items:
        parts = path.split("/")
        for i in range(1, len(parts)):
            parent_dirs.add("/".join(parts[:i]))

    return [
        (path, size, etag)
        for path, size, etag in all_items
        if not (size == 0 and path in parent_dirs)
    ]


def _compute_part_size(file_size: int, chunk_size: int) -> int:
    """Compute effective upload part size to stay under S3's 10,000-part limit.

    Args:
        file_size: Total file size in bytes.
        chunk_size: Desired chunk size in bytes.

    Returns:
        Effective part size — at least *chunk_size* but never so small that
        more than 9,900 parts would be needed (safety margin below 10,000).
    """
    return max(chunk_size, math.ceil(file_size / 9900))


async def upload_file(
    key: str,
    local_path: str | Path,
    store: BoundStore | ObjectStore | None = None,
    *,
    chunk_size: int = 8 * 1024 * 1024,
    normalize: bool = True,
    retain_local_copy: bool = True,
    compute_hash: bool = True,
) -> str | None:
    """Stream-upload a local file to *key* in the store.

    Uses obstore's multipart writer so arbitrarily large files are uploaded
    without materialising the whole content in memory.  The part size is
    adapted automatically to stay under S3's 10,000-part limit.

    A single pass over the file simultaneously feeds each chunk to the
    SHA-256 hasher and the store writer.

    Args:
        key: Destination object key.  Normalised by default.
        local_path: Path to the local file to upload.
        store: Target store, or ``None`` to use the infrastructure store.
        chunk_size: Desired chunk / part size in bytes (default 8 MiB).
            Increased automatically if the file is large enough to exceed
            the 9,900-part safety limit.
        normalize: When ``True`` (default), normalise *key* before use.
        retain_local_copy: When ``True`` (default), keep the local file after
            upload.  When ``False``, delete the local file after a successful
            upload.
        compute_hash: When ``True`` (default), compute a SHA-256 digest of the
            file while streaming and return it as a hex string.  Higher-level
            SDK transfer helpers use this digest to write a ``{key}.sha256``
            integrity record alongside the uploaded object, enabling
            deduplication and corruption detection on subsequent downloads.
            Pass ``False`` for external stores (e.g. ``CloudStore``) that do
            not participate in the SDK integrity protocol.

    Returns:
        Hex-encoded SHA-256 digest of the uploaded file if *compute_hash* is
        ``True``, else ``None``.

    Raises:
        StorageError: If the upload fails.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.

    Note:
        Zero-byte uploads are allowed but emit a warning — some S3-style backends
        may not persist an empty object.  GCS and local stores handle them correctly.
    """
    resolved = _resolve_store(store)
    put_attributes = _resolve_put_attributes(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    file_size = path.stat().st_size
    effective_chunk = _compute_part_size(file_size, chunk_size)

    if file_size == 0:
        logger.warning(
            "Uploading zero-byte file to key '%s' — "
            "some S3-style backends silently drop empty objects; "
            "verify the object exists after upload if your store requires it.",
            key,
        )

    h = hashlib.sha256() if compute_hash else None
    started = time.monotonic()
    from application_sdk.constants import (  # noqa: PLC0415
        STORAGE_PROGRESS_LOG_INTERVAL_SECONDS as _progress_interval,
    )

    last_progress = started
    bytes_sent = 0
    try:
        async with obstore.open_writer_async(
            resolved, key, buffer_size=effective_chunk, attributes=put_attributes
        ) as writer:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(effective_chunk)
                    if not chunk:
                        break
                    if h is not None:
                        h.update(chunk)
                    await writer.write(chunk)
                    bytes_sent += len(chunk)
                    if _progress_interval > 0:
                        now = time.monotonic()
                        if now - last_progress >= _progress_interval:
                            _log_transfer_progress(
                                "upload",
                                key,
                                bytes_so_far=bytes_sent,
                                elapsed_ms=(now - started) * 1000.0,
                                total_bytes=file_size,
                            )
                            last_progress = now
    # conformance: ignore[E004] upload error handler; _log_storage_event records error_class and exception is re-raised via StorageError chain
    except BaseException as exc:
        # BaseException is the umbrella for Exception and its siblings
        # (CancelledError, KeyboardInterrupt, SystemExit). Catching it here
        # ensures cancellation mid-writer-close is logged rather than silently
        # discarding the buffer and leaving no object in the store.
        elapsed_ms = (time.monotonic() - started) * 1000.0
        _log_storage_event(
            logging.WARNING,
            "upload",
            key,
            outcome="failure",
            elapsed_ms=elapsed_ms,
            size_bytes=file_size,
            error_class=_exc_class_name(exc),
        )
        if isinstance(exc, Exception):
            from application_sdk.storage.errors import (  # noqa: PLC0415
                StorageConfigError,
                StorageError,
            )

            if _is_azure_container_not_found(exc):
                raise StorageConfigError(
                    _azure_container_not_found_message(key)
                ) from exc
            raise StorageError(
                f"Failed to upload file to key '{key}'", key=key, cause=exc
            ) from exc
        raise  # re-raise CancelledError / KeyboardInterrupt bare after logging

    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log_storage_event(
        logging.DEBUG,
        "upload",
        key,
        outcome="success",
        elapsed_ms=elapsed_ms,
        size_bytes=file_size,
    )
    digest = h.hexdigest() if h is not None else None

    if not retain_local_copy:
        from application_sdk.constants import TEMPORARY_PATH  # noqa: PLC0415

        resolved_path = path.resolve()
        staging_root = Path(TEMPORARY_PATH).resolve()
        # Only delete files within the staging directory to prevent path traversal
        if resolved_path.is_relative_to(staging_root):
            try:
                resolved_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug(
                    "Failed to delete local file (cleanup): %s", type(exc).__name__
                )

    return digest


async def download_file(
    key: str,
    local_path: str | Path,
    store: BoundStore | ObjectStore | None = None,
    *,
    compute_hash: bool = False,
    min_chunk_size: int = 10 * 1024 * 1024,
    normalize: bool = True,
) -> str | None:
    """Stream-download *key* from the store to a local file.

    Uses obstore's streaming GET so arbitrarily large files are written to
    disk without materialising the whole content in memory.

    Args:
        key: Source object key.  Normalised by default.
        local_path: Destination path (file will be created or overwritten).
            Parent directories are created automatically.
        store: Source store, or ``None`` to use the infrastructure store.
        compute_hash: When ``True``, compute and return the SHA-256 digest
            while streaming.  When ``False`` (default), returns ``None``.
        min_chunk_size: Minimum chunk size hint passed to the stream iterator
            (default 10 MiB).
        normalize: When ``True`` (default), normalise *key* before use.

    Returns:
        Hex-encoded SHA-256 digest if *compute_hash* is ``True``, else ``None``.

    Raises:
        StorageNotFoundError: If *key* does not exist in the store.
        StorageError: If the download or write fails.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    h = hashlib.sha256() if compute_hash else None
    started = time.monotonic()

    try:
        result = await obstore.get_async(resolved, key)
    # conformance: ignore[E004] download error handler; _log_storage_event records error_class and exception is re-raised via StorageError chain
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if _is_not_found(exc):
            _log_storage_event(
                logging.WARNING,
                "download",
                key,
                outcome="failure",
                elapsed_ms=elapsed_ms,
                error_class="StorageNotFoundError",
            )
            from application_sdk.storage.errors import (  # noqa: PLC0415
                StorageNotFoundError,
            )

            raise StorageNotFoundError(
                f"Key not found in store: {key}", key=key
            ) from exc
        _log_storage_event(
            logging.WARNING,
            "download",
            key,
            outcome="failure",
            elapsed_ms=elapsed_ms,
            error_class=_exc_class_name(exc),
        )
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(
            f"Failed to download key '{key}'", key=key, cause=exc
        ) from exc

    bytes_written = 0
    try:
        # 0o600 on creation: owner-only — downloaded artifacts can contain
        # extracted customer metadata; don't rely on the process umask to keep
        # them private. Mirrors the chunked pre-allocation path. (Mode applies
        # only when the file is newly created; pre-existing perms are untouched.)
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        from application_sdk.constants import (  # noqa: PLC0415
            STORAGE_PROGRESS_LOG_INTERVAL_SECONDS as _progress_interval,
        )

        last_progress = started
        with os.fdopen(fd, "wb") as fh:
            async for chunk in result.stream(min_chunk_size=min_chunk_size):
                raw = bytes(chunk)
                fh.write(raw)
                bytes_written += len(raw)
                if h is not None:
                    h.update(raw)
                if _progress_interval > 0:
                    now = time.monotonic()
                    if now - last_progress >= _progress_interval:
                        _log_transfer_progress(
                            "download",
                            key,
                            bytes_so_far=bytes_written,
                            elapsed_ms=(now - started) * 1000.0,
                        )
                        last_progress = now
    # conformance: ignore[E004] file-write error handler; _log_storage_event records error_class and exception is re-raised via StorageError chain
    except Exception as exc:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        _log_storage_event(
            logging.WARNING,
            "download",
            key,
            outcome="failure",
            elapsed_ms=elapsed_ms,
            size_bytes=bytes_written,
            error_class=_exc_class_name(exc),
        )
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(
            f"Failed to write downloaded file to '{local_path}'", key=key, cause=exc
        ) from exc

    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log_storage_event(
        logging.DEBUG,
        "download",
        key,
        outcome="success",
        elapsed_ms=elapsed_ms,
        size_bytes=bytes_written,
    )
    return h.hexdigest() if h is not None else None


async def get_file_size(
    key: str,
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> int | None:
    """Return the byte size of *key* via a HEAD request, or ``None`` if not found.

    Uses a lightweight metadata-only request; the object body is never
    transferred.  Raises :class:`~application_sdk.storage.errors.StorageError`
    for non-404 errors (permission denied, I/O error, etc.).

    Args:
        key: Object key / path.  Normalised by default.
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.

    Returns:
        File size in bytes, or ``None`` if the key does not exist.

    Raises:
        StorageError: For non-404 errors.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        meta = await obstore.head_async(resolved, key)
        return int(meta["size"])
    # conformance: ignore[E004] not-found returns None as documented API contract; other exceptions re-raised via StorageError chain
    except Exception as exc:
        if _is_not_found(exc):
            return None
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(f"Failed to head key '{key}'", key=key, cause=exc) from exc


async def get_file_meta(
    key: str,
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> tuple[int, str | None] | None:
    """Return ``(size_bytes, e_tag)`` for *key*, or ``None`` if not found.

    Same single HEAD request as :func:`get_file_size`, but also surfaces the
    object's etag so callers can version-pin subsequent range GETs
    (:func:`download_file_chunked` ``etag=``) without a second HEAD.

    Raises:
        StorageError: For non-404 errors.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        meta = await obstore.head_async(resolved, key)
        return int(meta["size"]), meta.get("e_tag")
    # conformance: ignore[E004] not-found returns None as documented API contract; other exceptions re-raised via StorageError chain
    except Exception as exc:
        if _is_not_found(exc):
            return None
        from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
            StorageError,
        )

        raise StorageError(f"Failed to head key '{key}'", key=key, cause=exc) from exc


async def download_file_chunked(
    key: str,
    local_path: str | Path,
    store: BoundStore | ObjectStore | None = None,
    *,
    chunk_size_bytes: int = 16 * 1024 * 1024,
    max_concurrent_chunks: int = 4,
    compute_hash: bool = True,
    normalize: bool = True,
    file_size: int | None = None,
    etag: str | None = None,
    resume: bool | None = None,
) -> str | None:
    """Download *key* using parallel range GETs, writing chunks at fixed offsets.

    For files larger than *chunk_size_bytes*, issues multiple independent
    range requests (up to *max_concurrent_chunks* in flight at once) and
    writes each chunk to the correct file offset via ``os.lseek`` +
    ``os.write`` (``os.pwrite`` is unavailable on Windows).
    Each chunk gets its own obstore retry budget, so a mid-stream stall only
    retries the affected chunk — not the entire file.

    Falls through to :func:`download_file` (single streaming GET) when the
    remote object is smaller than *chunk_size_bytes*.

    **Version pinning (BLDX-1523):** when *etag* is known (supplied by the
    caller from a listing, or captured by the internal HEAD), every range GET
    carries ``If-Match: etag``. If the remote object is rewritten mid-download
    the store answers 412 instead of serving bytes from the new version, so
    chunks can never mix two object generations. On a 412 the partial file is
    discarded and the download restarts fresh **once** against the new
    generation; a second 412 raises.

    **Resume (BLDX-1523):** when *resume* is enabled, completed chunk indices
    are checkpointed to a ``{local_path}.transfer-state`` sidecar after every
    chunk write, and an interrupted download leaves the partial file + sidecar
    on disk instead of deleting them. A retry that resolves the same object
    generation (key / size / chunk size / etag all match) re-fetches only the
    missing chunks. The sidecar is deleted on success, so a data file without
    one is always complete. Resume requires the retry to see the same local
    filesystem (same pod or persistent volume); after node loss the download
    simply starts fresh.

    Args:
        key: Source object key.  Normalised by default.
        local_path: Destination path (created / overwritten).
        store: Source store, or ``None`` to use the infrastructure store.
        chunk_size_bytes: Size of each range-GET chunk (default 16 MiB).
        max_concurrent_chunks: Maximum number of in-flight chunk requests
            (default 4).
        compute_hash: When ``True`` (default), compute and return a SHA-256
            digest over the completed file.
        normalize: When ``True`` (default), normalise *key* before use.
        file_size: Pre-known object size in bytes. When supplied (e.g. from a
            prior listing that already carried sizes), the internal HEAD is
            skipped — this avoids a per-file HEAD when fanning out over a prefix
            whose sizes are already known. When ``None`` (default) a HEAD is
            issued, which also serves as the existence check.
        etag: Pre-known object etag (pairs with *file_size* from the same
            listing). When ``None`` and a HEAD is issued anyway, the etag is
            captured from the HEAD. When ``None`` and *file_size* is supplied,
            range GETs are unpinned — same semantics as before BLDX-1523.
        resume: Resume an interrupted download from its checkpoint sidecar.
            ``None`` (default) follows ``ATLAN_STORAGE_RESUME_DOWNLOADS``
            (enabled unless set to ``"false"``).

    Returns:
        Hex-encoded SHA-256 digest if *compute_hash* is ``True``, else ``None``.

    Raises:
        StorageNotFoundError: If *key* does not exist.
        StorageError: If a chunk download or the disk write fails, or the
            object was rewritten during both the original and restarted attempt.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """

    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    if resume is None:
        from application_sdk.constants import STORAGE_RESUME_DOWNLOADS  # noqa: PLC0415

        resume = STORAGE_RESUME_DOWNLOADS

    from application_sdk.constants import (  # noqa: PLC0415
        STORAGE_PROGRESS_LOG_INTERVAL_SECONDS as _progress_interval,
    )

    attempt = 0
    while True:
        attempt += 1

        # HEAD to get exact size before allocating; also serves as the existence
        # check. Skipped when the caller already knows the size (file_size), e.g.
        # a prefix download whose listing carried per-object sizes (+ etags).
        if file_size is None:
            try:
                meta = await obstore.head_async(resolved, key)
                file_size = int(meta["size"])
                if etag is None:
                    etag = meta.get("e_tag")
            # conformance: ignore[E004] not-found and errors both re-raised via StorageError chain; no silent swallow
            except Exception as exc:
                if _is_not_found(exc):
                    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                        StorageNotFoundError,
                    )

                    raise StorageNotFoundError(
                        f"Key not found in store: {key}", key=key
                    ) from exc
                from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                    StorageError,
                )

                raise StorageError(
                    f"Failed to head key '{key}'", key=key, cause=exc
                ) from exc

        # Pin the resolved size for this attempt: `file_size` stays optional
        # across restarts (reset to None to force a re-HEAD), `size` is the
        # narrowed int used everywhere below, including the chunk closure.
        assert file_size is not None
        size: int = file_size
        state_path = _transfer_state_path(path)

        # Small files: delegate to the single-stream path so they still use the
        # streaming GET (avoids materialising the whole body via range GETs).
        # Drop any stale checkpoint from an earlier, larger object generation.
        if size <= chunk_size_bytes:
            state_path.unlink(missing_ok=True)
            return await download_file(
                key, local_path, resolved, compute_hash=compute_hash, normalize=False
            )

        # ── Resume check ────────────────────────────────────────────────────
        # A checkpoint is only honoured when it describes exactly this object
        # generation AND the partial data file is still the pre-allocated size.
        done: set[int] = set()
        resuming = False
        if resume:
            st = _load_transfer_state(state_path)
            if (
                st is not None
                and st.get("key") == key
                and st.get("file_size") == size
                and st.get("chunk_size") == chunk_size_bytes
                and st.get("etag") == etag
                and path.is_file()
                and path.stat().st_size == size
            ):
                done = set(st["done"])
                resuming = bool(done)
            else:
                state_path.unlink(missing_ok=True)

        # Pre-allocate the file at the target size so lseek can address any
        # offset. On resume, open WITHOUT O_TRUNC so completed chunks survive.
        # 0o600: owner-only — downloaded artifacts can contain extracted customer
        # metadata; don't rely on the process umask to keep them private.
        flags = os.O_WRONLY | os.O_CREAT | (0 if resuming else os.O_TRUNC)
        fd = os.open(str(path), flags, 0o600)
        try:
            os.ftruncate(fd, size)
        # conformance: ignore[E004] cleanup-on-error guard; closes fd then re-raises immediately with no swallow
        except Exception:
            os.close(fd)
            raise

        offsets = list(range(0, size, chunk_size_bytes))
        pending = [(i, off) for i, off in enumerate(offsets) if i not in done]
        state_base = {
            "key": key,
            "file_size": size,
            "chunk_size": chunk_size_bytes,
            "etag": etag,
        }

        sem = asyncio.Semaphore(max_concurrent_chunks)
        started = time.monotonic()
        last_progress = started
        # Bytes already on disk from a previous attempt count toward progress
        # so the % is truthful; fetched_bytes is what THIS attempt transferred
        # and is what the terminal event / metrics report.
        completed_bytes = sum(min(chunk_size_bytes, size - offsets[i]) for i in done)
        fetched_bytes = 0

        if resuming:
            _log_storage_event(
                logging.INFO,
                "download",
                key,
                outcome="resume",
                size_bytes=completed_bytes,
            )

        async def _fetch_chunk(idx: int, offset: int) -> None:
            nonlocal completed_bytes, fetched_bytes, last_progress
            length = min(chunk_size_bytes, size - offset)
            async with sem:
                if etag is not None:
                    # Version-pinned range GET: 412 (PreconditionError) if the
                    # object was rewritten — never mixes two generations.
                    result = await obstore.get_async(
                        resolved,
                        key,
                        options={
                            "range": (offset, offset + length),
                            "if_match": etag,
                        },
                    )
                    raw = bytes(await result.bytes_async())
                else:
                    raw = bytes(
                        await obstore.get_range_async(
                            resolved, key, start=offset, length=length
                        )
                    )
                # lseek+write instead of pwrite (Windows lacks pwrite). Safe only
                # because asyncio is single-threaded: no await between the two
                # calls means no other coroutine can interleave on the fd position.
                # WARNING: if _fetch_chunk is ever moved into a thread (e.g. via
                # asyncio.to_thread), lseek+write becomes a data race — two threads
                # could interleave their seeks and corrupt each other's writes.
                # Use os.pwrite (or a per-thread fd) instead if that happens.
                os.lseek(fd, offset, os.SEEK_SET)
                os.write(fd, raw)
                # Checkpoint + progress: read-modify-write of the shared
                # counters is race-free because asyncio is single-threaded and
                # there is no await between here and the next chunk's update.
                done.add(idx)
                if resume:
                    _save_transfer_state(
                        state_path, {**state_base, "done": sorted(done)}
                    )
                completed_bytes += len(raw)
                fetched_bytes += len(raw)
                if _progress_interval > 0:
                    now = time.monotonic()
                    if now - last_progress >= _progress_interval:
                        _log_transfer_progress(
                            "download",
                            key,
                            bytes_so_far=completed_bytes,
                            elapsed_ms=(now - started) * 1000.0,
                            total_bytes=size,
                        )
                        last_progress = now

        chunk_tasks = [
            asyncio.ensure_future(_fetch_chunk(i, off)) for i, off in pending
        ]
        try:
            await asyncio.gather(*chunk_tasks)
        # conformance: ignore[E004] chunked-download error handler; cancels siblings, closes fd, checkpoints or cleans up, emits the terminal event/metric, then re-raises via StorageError chain
        except Exception as exc:
            # gather() does NOT cancel sibling tasks on first failure — without
            # this drain, orphaned chunk coroutines would keep running and write
            # into the fd after it is closed below (and, if the fd number were
            # reused, into an unrelated file). Cancel and await them all before
            # touching the fd. (BLDX-1523; latent since the original BLDX-1155
            # implementation.)
            for _t in chunk_tasks:
                _t.cancel()
            await asyncio.gather(*chunk_tasks, return_exceptions=True)
            os.close(fd)
            _log_storage_event(
                logging.WARNING,
                "download",
                key,
                outcome="failure",
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                size_bytes=fetched_bytes,
                error_class=_exc_class_name(exc),
            )
            if _is_precondition(exc):
                # Object rewritten mid-download: the partial file mixes
                # generations — discard it and restart fresh ONCE against
                # whatever is now in the store.
                _discard_transfer_state(path)
                if attempt == 1:
                    logger.warning(
                        "Object changed during chunked download; restarting fresh: %s",
                        key,
                        exc_info=True,
                    )
                    file_size = None
                    etag = None
                    continue
                from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                    StorageError,
                )

                raise StorageError(
                    f"Object at '{key}' kept changing during chunked download "
                    f"(etag precondition failed twice)",
                    key=key,
                    cause=exc,
                ) from exc
            if _is_not_found(exc):
                _discard_transfer_state(path)
                from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                    StorageNotFoundError,
                )

                raise StorageNotFoundError(
                    f"Key not found during chunked download: {key}", key=key
                ) from exc
            if not resume:
                # Legacy behaviour: no checkpoint, so a partial file is garbage.
                path.unlink(missing_ok=True)
            # With resume enabled the partial file + sidecar stay on disk —
            # the next attempt (Temporal retry, same pod) fetches only the
            # missing ranges recorded in the checkpoint.
            from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
                StorageError,
            )

            raise StorageError(
                f"Chunked download failed for '{key}'", key=key, cause=exc
            ) from exc

        os.close(fd)
        # Success: the checkpoint's existence means "incomplete" — remove it so
        # the file is observably complete.
        state_path.unlink(missing_ok=True)
        _log_storage_event(
            logging.DEBUG,
            "download",
            key,
            outcome="success",
            elapsed_ms=(time.monotonic() - started) * 1000.0,
            size_bytes=fetched_bytes,
        )

        if not compute_hash:
            return None

        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()


async def _get_bytes(
    key: str,
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> bytes | None:
    """Fetch the bytes stored at *key*, or ``None`` if the key does not exist.

    **Internal use only** — intended for small payloads (sidecars, metadata,
    JSON configs).  For large files use :func:`download_file` instead.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.
            Pass ``False`` to use *key* exactly as supplied.

    Returns:
        Raw bytes, or ``None`` if the key was not found.

    Raises:
        StorageError: For non-404 errors (permission denied, I/O error, etc.).
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        result = await obstore.get_async(resolved, key)
        # GetResult.bytes() is the sync accessor; bytes(result) iterates
        # over Rust Bytes chunks and yields bytes objects, not ints.
        raw = result.bytes()
        return bytes(raw)
    # conformance: ignore[E004] not-found returns None as documented API contract; other exceptions re-raised via StorageError chain
    except Exception as exc:
        if _is_not_found(exc):
            return None
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(f"Failed to get key '{key}'", key=key, cause=exc) from exc


async def _put(
    key: str,
    data: bytes,
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> None:
    """Write *data* to *key* in the store (creates or overwrites).

    **Internal use only** — intended for small payloads (sidecars, metadata,
    JSON configs).  For large files use :func:`upload_file` instead.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        data: Raw bytes to write.
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.
            Pass ``False`` to use *key* exactly as supplied.

    Raises:
        StorageError: If the write fails.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    put_attributes = _resolve_put_attributes(store)
    if normalize:
        key = normalize_key(key)
    try:
        await obstore.put_async(resolved, key, data, attributes=put_attributes)
    # conformance: ignore[E004] put error handler; all exceptions re-raised via StorageConfigError or StorageError chain
    except Exception as exc:
        from application_sdk.storage.errors import (  # noqa: PLC0415
            StorageConfigError,
            StorageError,
        )

        if _is_azure_container_not_found(exc):
            raise StorageConfigError(_azure_container_not_found_message(key)) from exc
        raise StorageError(f"Failed to put key '{key}'", key=key, cause=exc) from exc


async def put_json(
    key: str,
    obj: JsonValue,
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> None:
    """Serialise *obj* to JSON and write to *key*.

    Convenience wrapper around :func:`_put` for small JSON payloads such as
    workflow configs and sidecar metadata.  For large files use
    :func:`upload_file` instead.

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        obj: A JSON-serialisable value (dict, list, str, int, float, bool, or None).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.

    Raises:
        StorageError: If the write fails.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    await _put(key, orjson.dumps(obj), store, normalize=normalize)


async def delete(
    key: str,
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> bool:
    """Delete the object at *key*.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.
            Pass ``False`` to use *key* exactly as supplied.

    Returns:
        ``True`` if deleted, ``False`` if the key did not exist.

    Raises:
        StorageError: For non-404 errors.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        await obstore.delete_async(resolved, key)
        return True
    # conformance: ignore[E004] not-found returns False as documented API contract; other exceptions re-raised via StorageError chain
    except Exception as exc:
        if _is_not_found(exc):
            return False
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(f"Failed to delete key '{key}'", key=key, cause=exc) from exc


async def exists(
    key: str,
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> bool:
    """Return ``True`` if *key* exists in the store.

    Uses a HEAD request (metadata only) — the object content is never
    downloaded, so this is safe to call on arbitrarily large objects.

    When *store* is omitted the store is resolved from the current
    infrastructure context (see :func:`_resolve_store`).

    Args:
        key: Object key / path.  Normalised by default (see :func:`normalize_key`).
        store: An obstore-compatible store instance, or ``None`` to use the
            store from the current infrastructure context.
        normalize: When ``True`` (default), normalise *key* before use.

    Returns:
        ``True`` if the object exists, ``False`` otherwise.

    Raises:
        StorageError: For non-404 errors (permission denied, I/O error, etc.).
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)
    try:
        await obstore.head_async(resolved, key)
        return True
    # conformance: ignore[E004] not-found returns False as documented API contract; other exceptions re-raised via StorageError chain
    except Exception as exc:
        if _is_not_found(exc):
            return False
        from application_sdk.storage.errors import StorageError  # noqa: PLC0415

        raise StorageError(
            f"Failed to check existence of key '{key}'", key=key, cause=exc
        ) from exc
