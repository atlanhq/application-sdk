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

import contextlib
import hashlib
import logging
import math
import os
import re
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, TypedDict

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

from application_sdk._runtime.offload import run_in_thread
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.common._listing import PARTIAL_DIRNAME
from application_sdk.observability.logger_adaptor import get_logger

# Transfer integrity validation (FND-306). ``integrity`` holds no top-level
# storage-sibling imports — it reaches back into ``ops`` lazily — so this
# top-level import is acyclic regardless of module load order.
from application_sdk.storage import integrity
from application_sdk.storage._telemetry import (
    _log_transfer_progress,
    _record_transfer_metric,
    _throughput_mbps,
)

# Back-compat re-export: the chunked/resumable download path lives in
# storage/chunked.py (BLDX-1513 follow-up split); existing
# `from application_sdk.storage.ops import download_file_chunked` callers
# keep working. chunked.py imports ops only lazily, so this is cycle-free.
from application_sdk.storage.chunked import (  # noqa: F401 — re-export
    download_file_chunked,
)

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


def _is_local_dir_collision(exc: BaseException, store: ObjectStore, key: str) -> bool:
    """Return True when *key* names a directory in a local store, not an object.

    A :class:`~obstore.store.LocalStore` maps an object key onto a filesystem
    path, so a probe of a key that names a directory (e.g. the bare root marker
    ``"t"`` when ``t/`` holds children) does not surface as "not found": on
    Windows the stat raises ``GenericError("… Unable to open file …: Access is
    denied. (os error 5)")``, and a failed open can surface as "… Is a
    directory".  A key that resolves to a directory can never also exist as an
    object, so this is a directory collision — not a permission or I/O failure
    — and the caller contract here ("skip when the object is not there") is
    served by treating it as missing.

    Deliberately not a message match on its own; message text alone would
    reclassify a genuine failure that happens to be worded the same way (a
    non-local backend, or a local *file* that is unreadable).  Three gates, in
    order of authority:

    1. Not a ``LocalStore`` → never a directory collision.  Cloud backends have
       no directories; a 403 wording similar to Windows' stays fatal.
    2. A ``LocalStore`` with a prefix (what :func:`storage.factory.create_local_store`
       builds) → resolve the key and ask the filesystem.  ``is_dir()`` is the
       authoritative answer and needs no message parsing: it is ``False`` for an
       unreadable *file*, so a real permission failure still raises.
    3. A rootless ``LocalStore`` (keys are not resolvable to a path here) → fall
       back to the conjunctive message shape: an "unable to open file" prefix
       *and* a directory-stat suffix.  A plain ``ERROR_ACCESS_DENIED`` or a
       cloud 403 containing "access is denied" does not match.

    Permission failures must stay fatal in every case — see the ``StorageError``
    contract on :func:`delete` / :func:`exists`, and ``StoragePermissionError``
    in ``storage.preflight``.
    """
    from obstore.store import LocalStore  # noqa: PLC0415 — defensive: keep inline

    if not isinstance(store, LocalStore):
        return False

    root = store.prefix
    if root is not None:
        # PurePosixPath: obstore keys are always '/'-separated, whatever the OS.
        return Path(root, *PurePosixPath(key).parts).is_dir()

    msg = str(exc).lower()
    return "unable to open file" in msg and (
        "access is denied" in msg or "is a directory" in msg
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


# ``object_store`` formats every backend HTTP failure as
#   "Generic {store} error: Error performing {METHOD} {url} in {elapsed} -
#    Server returned non-2xx status code: {status} {reason}: {body}"
# and GCS, S3 and Azure Blob all put an XML
# ``<Error><Code>...</Code><Message>...</Message></Error>`` in ``{body}``.
# Parsing the two routing-relevant tokens out is what lets a consumer branch on
# a field: before FND-957 the status existed only inside the free-text cause,
# which the envelope's length cap usually truncated away before it arrived.
# Cap for the logged copy of a driver error. Larger than the wire envelope's
# cap on purpose — the point of the log copy is to outlive the truncation — but
# still bounded, because ``error_message`` promotes to an indexed OTLP
# attribute. Matches ``safe_traceback``'s ceiling.
_LOG_CAUSE_MAX_LEN = 8000

_OBSTORE_HTTP_STATUS_RE = re.compile(r"status code:\s*(\d{3})")
_PROVIDER_CODE_RE = re.compile(r"<Code>([^<>]{1,64})</Code>")


class _StorageEvidence(TypedDict):
    """The kwargs every branch of :func:`_storage_error_for` splats into a leaf.

    Typed rather than ``dict[str, Any]`` so pyright checks the splat against
    each ``__init__`` it reaches. Under ``Any`` that check is silently skipped,
    and the failure mode is the one this module exists to prevent: add a field
    here, miss one leaf's ``__init__``, and the ``TypeError`` surfaces at raise
    time — on the error path, under load, replacing the failure it was meant to
    describe.
    """

    key: str
    cause: Exception
    http_status: int | None
    provider_code: str | None
    target: str | None


def _obstore_http_evidence(exc: BaseException) -> tuple[int | None, str | None]:
    """Return ``(http_status, provider_code)`` parsed from *exc*, best-effort.

    Both are ``None`` when *exc* is not a backend HTTP failure -- a local store,
    a request timeout, a credential decode error. Never raises: a parse miss
    degrades the evidence attached to the failure, it must never replace the
    failure itself.
    """
    text = str(exc)
    status = _OBSTORE_HTTP_STATUS_RE.search(text)
    provider = _PROVIDER_CODE_RE.search(text)
    return (
        int(status.group(1)) if status else None,
        provider.group(1) if provider else None,
    )


# Scheme per obstore store class, for the ``target`` evidence field.
_STORE_SCHEMES: dict[str, str] = {
    "GCSStore": "gs",
    "S3Store": "s3",
    "AzureStore": "abfs",
    "LocalStore": "file",
}
# Identity keys that are safe to read off a store's ``config``. That dict also
# carries live credentials -- an ``S3Store`` exposes ``secret_access_key`` and
# an ``AzureStore`` exposes ``account_key``, both in plaintext -- so this is an
# allowlist and must stay one. Never put ``store.config`` itself on an error.
_STORE_IDENTITY_KEYS: tuple[str, ...] = ("bucket", "container_name")


def _store_target(store: ObjectStore, key: str) -> str | None:
    """Return a ``scheme://container/path`` identity for the failing request.

    This is the ``target`` evidence field -- "what were we talking to". It is
    assembled from the store's class and one allowlisted identity key, never
    from the request URL and never from ``store.config`` wholesale: a signed
    URL carries ``X-Goog-Signature``, which ``redact_secrets`` does not strip,
    and the config dict holds credentials outright.

    Best-effort by design. Returns ``None`` rather than raising, because this
    only decorates a failure and must never be able to replace it. (FND-957)
    """
    try:
        scheme = _STORE_SCHEMES.get(type(store).__name__, "objectstore")
        container: str | None = None
        config = getattr(store, "config", None)
        if config is not None and hasattr(config, "get"):
            for candidate in _STORE_IDENTITY_KEYS:
                value = config.get(candidate)
                if isinstance(value, str) and value:
                    container = value
                    break
        prefix = getattr(store, "prefix", None)
        path = f"{prefix}/{key}" if isinstance(prefix, str) and prefix else key
        return f"{scheme}://{container}/{path}" if container else f"{scheme}:///{path}"
    except Exception:  # pragma: no cover — identity is decoration, never fatal
        return None


def _storage_error_for(
    exc: Exception,
    key: str,
    message: str,
    store: ObjectStore | None = None,
) -> Exception:
    """Build the typed storage error for a failed write.

    Only two conditions reclassify: a missing Azure container, and a bucket
    mid-relocation. Everything else — including a bare ``PreconditionFailed``
    — is a retryable ``StorageError``. The parsed ``http_status`` and
    ``provider_code`` are *evidence, not routing*: they ride on the envelope so
    a consumer holding context the SDK lacks can branch on them, and so a
    future case can argue for its own leaf from data rather than inference.

    Order is load-bearing, but not for the retry verdict — both reclassifying
    branches are retryable. It decides which ``code`` and which
    ``suggested_action`` an operator sees, and those are wrong if a broader
    rule matches first. Do not reorder without re-reading the notes at each
    branch.

    Every branch carries the same evidence, so a consumer reads ``http_status``,
    ``provider_code`` and ``target`` off the envelope regardless of which leaf
    was chosen. (FND-957)
    """
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        StorageBucketRelocationError,
        StorageConfigError,
        StorageError,
    )

    http_status, provider_code = _obstore_http_evidence(exc)
    evidence: _StorageEvidence = {
        "key": key,
        "cause": exc,
        "http_status": http_status,
        "provider_code": provider_code,
        "target": _store_target(store, key) if store is not None else None,
    }

    # Checked before the status routing because its remediation is a config
    # change rather than a retry, and Azure reports it as a plain 404 that
    # would otherwise be indistinguishable from a missing blob. It carries the
    # same evidence as every other branch: the container name is in ``target``
    # and the 404/ContainerNotFound pair is what tells an operator the
    # container, not the object, is what is absent.
    if _is_azure_container_not_found(exc):
        return StorageConfigError(_azure_container_not_found_message(key), **evidence)

    # Relocation is checked BEFORE the generic fallthrough below, and the order
    # is load-bearing rather than stylistic — though not for the reason it might
    # look. Both outcomes are retryable, so nothing here changes a retry
    # decision. What the order buys is the *specific* code
    # (DEPENDENCY_UNAVAILABLE_STORAGE_RELOCATION, which the preflight gate
    # stamps for the same condition) and the remediation hint telling an
    # operator to wait the move out. Fall through to the generic StorageError
    # first and both are silently lost: the failure still retries, but nobody
    # can tell a relocation from any other storage blip.
    #
    # The detection is deliberately the preflight gate's, not a second one: its
    # rule requires BOTH the "precondition" and "relocation" tokens, so a plain
    # etag/if-match 412 can never be misread as a relocation and pick up a
    # "wait for the move to finish" hint for a move that is not happening.
    from application_sdk.storage.preflight import (  # noqa: PLC0415 — error path only; avoids a module-load cycle
        RELOCATION_BUCKET,
        _classify_access_error,
    )

    bucket, hint = _classify_access_error(exc)
    if bucket == RELOCATION_BUCKET:
        return StorageBucketRelocationError(
            f"{message}: the destination bucket is being relocated",
            suggested_action=hint,
            **evidence,
        )
    # Nothing else reclassifies. A 403 or 404 is genuinely ambiguous here:
    # ``upload_file`` accepts an explicit store, so the same status arrives both
    # from the deployment's own artifact store (audience APP_OWNER) and from a
    # caller-supplied store pointing at a customer bucket (audience USER).
    # Routing audience off the status would put customer-facing attribution on
    # our own infrastructure half the time.
    #
    # A bare ``PreconditionFailed`` that is *not* a relocation deliberately
    # stays a retryable ``StorageError`` too. The one real instance we have of
    # that status turned out to be a relocation — temporary and self-healing —
    # so the prior on "unclassified precondition means permanent" is weak, and
    # a wrongly-permanent verdict fails a run that would have recovered. The
    # status and provider code still reach ``evidence``, which is what a future
    # non-relocation case would need to make the argument for its own leaf.
    return StorageError(message, **evidence)


def _log_storage_event(
    level: int,
    op: str,
    store_path: str,
    *,
    outcome: str,
    elapsed_ms: float | None = None,
    size_bytes: int | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
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
    if error_message is not None:
        # The wire envelope length-caps ``cause_repr``, so without this the
        # provider's own explanation had nowhere to live: this event previously
        # recorded only ``error_class`` ("GenericError"), with no ``str(exc)``,
        # no ``exc_info`` and no ``safe_traceback``. A copy here gives a
        # truncated envelope a higher-fidelity counterpart in the logs.
        # ``error_message`` is already in ``_KNOWN_EXTRA_KEYS``, so it promotes
        # to an indexed OTLP attribute — hence the cap, for the same reason
        # ``safe_traceback`` has one. Callers pass secret-redacted text.
        # (FND-957)
        extra["error_message"] = error_message[:_LOG_CAUSE_MAX_LEN]
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


async def _finalize_upload_integrity(
    key: str,
    path: Path,
    store: BoundStore | ObjectStore | None,
    resolved: ObjectStore,
    *,
    declared_size: int,
    bytes_sent: int,
    digest: str | None,
    verify: bool | None,
    write_sidecar: bool | None,
    defer_remote_verify: bool = False,
) -> None:
    """Validate a completed upload, then record its digest (FND-306).

    Kept out of :func:`upload_file`'s body: the transport there (part sizing,
    the streaming loop, error translation, cleanup) and this validation
    sequence change for unrelated reasons, and the next edit to either should
    review on its own.

    The order is load-bearing. The local-truncation check comes first because
    it invalidates the object itself; the readback next; and the sidecar only
    after both pass — a sidecar must never advertise a digest for an object
    this same upload just rejected, or a downstream reader would "verify" a
    corrupt file successfully.

    Args:
        key: Normalised destination key, as written.
        path: Local source file.
        store: Caller's store argument — passed to the sidecar write so a
            ``BoundStore``'s put attributes still apply.
        resolved: Underlying store, for the readback HEAD.
        declared_size: ``st_size`` observed before the read began.
        bytes_sent: Bytes actually streamed to the store.
        digest: Streaming SHA-256, or ``None`` when hashing was off.
        verify: Per-call verification flag (``None`` → env default).
        write_sidecar: Per-call sidecar flag (``None`` → env default).
        defer_remote_verify: When ``True``, run the local-truncation check but
            skip the readback HEAD — the caller has taken over the readback
            (a directory upload verifies every object against one listing of
            its prefix instead of one HEAD each, FND-1339). Such a caller must
            also hold the sidecar back (``write_sidecar=False``) until its own
            readback has passed, or the ordering guarantee above is lost.

    Raises:
        StorageIntegrityError: If the local file shrank while being read.
        StorageError: If the object is absent afterwards, or is not the size
            that was sent.
    """
    if integrity.verification_enabled(verify):
        integrity.check_local_file_stable(
            key, path, declared_size=declared_size, bytes_read=bytes_sent
        )
        if not defer_remote_verify:
            remote_meta = await get_file_meta(key, resolved, normalize=False)
            if remote_meta is None:
                # The writer reported success but the object is not there. The
                # zero-byte case is the known one (some S3-style backends drop
                # empty objects), and a size comparison would pass it — 0 sent,
                # 0 found — so absence is checked separately from length.
                from application_sdk.storage.errors import StorageError  # noqa: PLC0415

                raise StorageError(
                    f"Upload of '{key}' reported success but the object is "
                    f"absent from the store afterwards ({bytes_sent} bytes sent "
                    f"from {path}). Some S3-style backends silently drop empty "
                    f"objects; others need read-your-writes consistency that "
                    f"this store does not offer.",
                    key=key,
                )
            integrity.check_transfer_size(
                "upload",
                key,
                expected=bytes_sent,
                actual=remote_meta[0],
                local_path=path,
            )

    if digest is not None and integrity.sidecar_writes_enabled(write_sidecar):
        await integrity.write_digest_sidecar(store, key, digest)


async def upload_file(
    key: str,
    local_path: str | Path,
    store: BoundStore | ObjectStore | None = None,
    *,
    chunk_size: int | None = None,
    normalize: bool = True,
    retain_local_copy: bool = True,
    compute_hash: bool = True,
    verify: bool | None = None,
    write_sidecar: bool | None = None,
    defer_remote_verify: bool = False,
) -> str | None:
    """Stream-upload a local file to *key* in the store.

    Uses obstore's multipart writer so arbitrarily large files are uploaded
    without materialising the whole content in memory.  The part size is
    adapted automatically to stay under S3's 10,000-part limit.

    A single pass over the file simultaneously feeds each chunk to the
    SHA-256 hasher and the store writer.

    **Integrity (FND-306).** This is the single upstream convergence point for
    every upload in the SDK, so the write-side validations live here rather
    than at each call site.  After the writer closes, three things happen (all
    governed by ``ATLAN_STORAGE_VERIFY_TRANSFERS``):

    1. the local file is confirmed not to have shrunk while it was being read
       — a truncation under the reader would have put a partial artifact in the
       store (:class:`~application_sdk.storage.errors.StorageIntegrityError`);
    2. a HEAD confirms the store recorded exactly the bytes we sent — this is
       what catches the S3-style backends that silently drop an object
       (retryable ``StorageError``);
    3. the streamed SHA-256 is written to a ``{key}.sha256`` sidecar so a
       *downstream* app — a different process, on a different pod, days later
       — can detect an artifact whose producer died mid-write.

    Args:
        key: Destination object key.  Normalised by default.
        local_path: Path to the local file to upload.
        store: Target store, or ``None`` to use the infrastructure store.
        chunk_size: Desired chunk / part size in bytes, honoured only when
            ``ATLAN_STORAGE_UPLOAD_PART_SIZE_BYTES`` is unset — a deployment
            that sets it overrides the caller, since the workable part size
            depends on the destination the deployment writes to rather than on
            the app.  Defaults to 8 MiB when neither is given.  Increased
            automatically if the file is large enough to exceed the 9,900-part
            safety limit.  See the constant's docstring for the
            S3-proxy-over-GCS case that makes part *count* what matters.
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
            not participate in the SDK integrity protocol.  Implies
            ``write_sidecar=False``.
        verify: Run the post-upload integrity validations described above.
            ``None`` (default) follows ``ATLAN_STORAGE_VERIFY_TRANSFERS``.
        write_sidecar: Write the ``{key}.sha256`` sidecar.  ``None`` (default)
            follows ``ATLAN_STORAGE_WRITE_SIDECARS``.  Ignored when
            *compute_hash* is ``False`` (there is no digest to record).
        defer_remote_verify: When ``True`` and verification is on, skip this
            call's readback HEAD; the local-truncation check still runs.  For
            callers that verify a whole batch against one listing of the
            target prefix instead of one HEAD per object — the shape
            :func:`application_sdk.storage.transfer.upload` uses for
            directories (FND-1339).  Pair it with ``write_sidecar=False`` and
            write the sidecars once that readback has passed.

    Returns:
        Hex-encoded SHA-256 digest of the uploaded file if *compute_hash* is
        ``True``, else ``None``.

    Raises:
        StorageError: If the upload fails, or the object the store reports
            after the write is not the size that was sent.
        StorageIntegrityError: If the local file shrank while it was being
            read, so the uploaded object is a truncated prefix of it.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.

    Note:
        Zero-byte uploads are allowed but emit a warning — some S3-style backends
        may not persist an empty object.  GCS and local stores handle them correctly.
        With verification on, a backend that drops the object is no longer silent:
        the post-upload HEAD turns it into a ``StorageError``.
    """
    resolved = _resolve_store(store)
    put_attributes = _resolve_put_attributes(store)
    if normalize:
        key = normalize_key(key)

    from application_sdk.constants import (  # noqa: PLC0415
        STORAGE_UPLOAD_MAX_CONCURRENCY,
        STORAGE_UPLOAD_PART_SIZE_BYTES,
        STORAGE_UPLOAD_PART_SIZE_OVERRIDDEN,
    )

    # A deployment that sets the part size overrides the caller. The size that
    # keeps a completion inside the destination's idle timeout depends on where
    # the deployment writes; an app cannot know which tenant it lands on, so it
    # must not be able to pin a value the operator then cannot correct.
    if (
        STORAGE_UPLOAD_PART_SIZE_OVERRIDDEN
        and chunk_size is not None
        and chunk_size != STORAGE_UPLOAD_PART_SIZE_BYTES
    ):
        logger.debug(
            "Using deployment part size %d instead of the requested %d " "for key '%s'",
            STORAGE_UPLOAD_PART_SIZE_BYTES,
            chunk_size,
            key,
        )
    requested_chunk = (
        STORAGE_UPLOAD_PART_SIZE_BYTES
        if (STORAGE_UPLOAD_PART_SIZE_OVERRIDDEN or chunk_size is None)
        else chunk_size
    )

    path = Path(local_path)
    file_size = path.stat().st_size
    effective_chunk = _compute_part_size(file_size, requested_chunk)

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
            resolved,
            key,
            buffer_size=effective_chunk,
            max_concurrency=STORAGE_UPLOAD_MAX_CONCURRENCY,
            attributes=put_attributes,
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
                    # One multipart part on its way to the store is one
                    # observable unit (ADR-0018). Marking per part rather than
                    # per file is what keeps a single multi-GB upload — the
                    # common shape for a large connector's parquet output —
                    # from looking like one long quiet window to the stall
                    # watchdog. Unconditional, unlike the progress *log* below:
                    # a mark is two stores under an uncontended lock, so it
                    # needs no interval gate.
                    current_progress_tracker().mark_progress("storage.upload_part")
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
        from application_sdk.errors.base import (  # noqa: PLC0415 — lazy: avoid import-time cycle errors<->storage
            redact_secrets,
        )

        _log_storage_event(
            logging.WARNING,
            "upload",
            key,
            outcome="failure",
            elapsed_ms=elapsed_ms,
            size_bytes=file_size,
            error_class=_exc_class_name(exc),
            error_message=redact_secrets(str(exc)),
        )
        if isinstance(exc, Exception):
            raise _storage_error_for(
                exc, key, f"Failed to upload file to key '{key}'", resolved
            ) from exc
        raise  # re-raise CancelledError / KeyboardInterrupt bare after logging

    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log_storage_event(
        logging.DEBUG,
        "upload",
        key,
        outcome="success",
        elapsed_ms=elapsed_ms,
        size_bytes=bytes_sent,
    )
    digest = h.hexdigest() if h is not None else None

    await _finalize_upload_integrity(
        key,
        path,
        store,
        resolved,
        declared_size=file_size,
        bytes_sent=bytes_sent,
        digest=digest,
        verify=verify,
        write_sidecar=write_sidecar,
        defer_remote_verify=defer_remote_verify,
    )

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
    verify: bool | None = None,
    expected_sha256: str | None = None,
    sidecar_present: bool | None = None,
) -> str | None:
    """Stream-download *key* from the store to a local file.

    Uses obstore's streaming GET so arbitrarily large files are written to
    disk without materialising the whole content in memory.

    **Integrity (FND-306).** This is the downstream convergence point for every
    small-file download in the SDK (``download_file_chunked`` delegates here
    below its chunking threshold), so the read-side validations live here.
    With verification on, the bytes written to disk are compared against the
    size the store declared for the object, and — when the producer left a
    ``{key}.sha256`` sidecar — the content is hashed and compared against it.
    A digest mismatch means the stored object is corrupt at source and raises
    a non-retryable
    :class:`~application_sdk.storage.errors.StorageIntegrityError` naming the
    key and both digests, instead of letting a truncated file reach a parser
    and surface as an unattributable ``Malformed JSON`` deep inside DuckDB.

    **Atomic publish (CONNECT-1126).** The bytes stream into a uniquely-named
    temp file inside the ``.sdk-partial`` staging directory beside the
    destination (created 0o600 by ``mkstemp``: owner-only, since downloaded
    artifacts can contain extracted customer metadata; staged there so no SDK
    tree walk can adopt it) and land at *local_path* via ``os.replace``. The
    destination therefore never holds a partial file — a concurrent reader,
    or a second download of the same shared ``local_path``, sees only the
    previous complete content or the new complete content. On Windows the
    publish additionally requires that no other handle holds the destination
    open: ``os.replace`` raises ``PermissionError`` rather than succeeding,
    so a concurrent reader turns a corrupt read into a failed write.

    Args:
        key: Source object key.  Normalised by default.
        local_path: Destination path (file will be created or overwritten).
            Parent directories are created automatically.
        store: Source store, or ``None`` to use the infrastructure store.
        compute_hash: When ``True``, compute and return the SHA-256 digest
            while streaming.  When ``False`` (default), returns ``None``.
            Verification may enable hashing internally; the digest is still
            only *returned* when this is ``True``.
        min_chunk_size: Minimum chunk size hint passed to the stream iterator
            (default 10 MiB).
        normalize: When ``True`` (default), normalise *key* before use.
        verify: Run the integrity validations described above.  ``None``
            (default) follows ``ATLAN_STORAGE_VERIFY_TRANSFERS``.
        expected_sha256: Digest to verify the content against.  Pass it when
            the caller already holds the producer's digest — the sidecar
            lookup is then skipped, saving a round trip.  ``None`` (default)
            fetches the sidecar when verification is on.
        sidecar_present: Whether ``{key}.sha256`` exists, when a prior listing
            already established it.  Saves the existence probe on prefix
            downloads, where the listing has already enumerated every sidecar.

    Returns:
        Hex-encoded SHA-256 digest if *compute_hash* is ``True``, else ``None``.

    Raises:
        StorageNotFoundError: If *key* does not exist in the store.
        StorageError: If the download or write fails, or fewer bytes reached
            disk than the store declared for the object.
        StorageIntegrityError: If the downloaded content does not match the
            digest its producer recorded.
        ObjectStoreNotProvidedError: If *store* is ``None`` and no infrastructure store is set.
    """
    resolved = _resolve_store(store)
    if normalize:
        key = normalize_key(key)

    path = Path(local_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    verifying = integrity.verification_enabled(verify)
    # Hash while streaming whenever verification is on: the bytes are already
    # in hand, so this costs CPU only and no re-read. The sidecar lookup that
    # decides whether the digest is *compared* runs after the transfer — doing
    # it first would let a store-wide failure surface as a sidecar error and
    # mask the real download failure (and its telemetry) behind it.
    h = hashlib.sha256() if (compute_hash or verifying) else None
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
    tmp_name: str | None = None
    published = False
    try:
        try:
            staging_dir = path.parent / PARTIAL_DIRNAME
            # mode hardens only the first creation (ignored when the directory
            # exists) — it keeps a staging dir under a shared temp root private.
            os.makedirs(staging_dir, mode=0o700, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                dir=str(staging_dir), prefix=path.name + "."
            )
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
                    # One streamed chunk landed on disk — see the matching mark in
                    # upload_file for why this is per chunk and ungated.
                    current_progress_tracker().mark_progress("storage.download_chunk")
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
                # fsync before the publish: on a delayed-allocation filesystem
                # ENOSPC can surface only at writeback, and without this a
                # short file would be published as complete (the FND-318
                # argument). Offloaded so a large flush does not hold the
                # event loop and the activity heartbeat with it.
                fh.flush()
                await run_in_thread(os.fsync, fh.fileno())
            os.replace(tmp_name, path)
            published = True
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
    finally:
        # BaseException-safe: a Temporal cancel or worker shutdown must not
        # strand a uniquely-named staging file per attempt.
        #
        # Suppressed, like common.atomic._discard and the chunked path's
        # _discard_transfer_state: `missing_ok` covers only FileNotFoundError,
        # and a raise out of this `finally` would replace whatever the caller
        # was about to receive — a completed download, or the typed
        # StorageError above — with a bare OSError about the temp file.
        if not published and tmp_name is not None:
            with contextlib.suppress(OSError):
                Path(tmp_name).unlink(missing_ok=True)

    elapsed_ms = (time.monotonic() - started) * 1000.0
    _log_storage_event(
        logging.DEBUG,
        "download",
        key,
        outcome="success",
        elapsed_ms=elapsed_ms,
        size_bytes=bytes_written,
    )

    digest = h.hexdigest() if h is not None else None

    # ── Integrity: validate before the caller can parse it (FND-306) ─────────
    if verifying:
        # result.meta came back with the GET — the declared length costs no
        # extra round trip. A stream that ended early is a truncated download,
        # distinct from an object that is corrupt in the store.
        integrity.check_transfer_size(
            "download",
            key,
            expected=int(result.meta["size"]),
            actual=bytes_written,
            local_path=path,
        )
        if expected_sha256 is None:
            expected_sha256 = await integrity.read_expected_digest(
                resolved, key, sidecar_present=sidecar_present
            )
        if expected_sha256 is not None and digest is not None:
            # The corrupt file is deliberately left on disk: byte-stability
            # across fresh downloads is what distinguishes "corrupt at source"
            # from "flaky transfer", and an operator cannot check that against
            # a file we deleted. A retry atomically publishes a fresh download
            # over it.
            integrity.check_transfer_digest(
                "download",
                key,
                expected=expected_sha256,
                actual=digest,
                local_path=path,
            )

    return digest if compute_hash else None


async def get_file_size(
    key: str,
    store: BoundStore | ObjectStore | None = None,
    *,
    normalize: bool = True,
) -> int | None:
    """Return the byte size of *key* via a HEAD request, or ``None`` if not found.

    Thin wrapper over :func:`get_file_meta` (same single HEAD request) that
    discards the etag. Prefer :func:`get_file_meta` when you also need the
    etag — e.g. to version-pin a subsequent chunked download.

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
    meta = await get_file_meta(key, store, normalize=normalize)
    return None if meta is None else meta[0]


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
        raise _storage_error_for(
            exc, key, f"Failed to put key '{key}'", resolved
        ) from exc


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
