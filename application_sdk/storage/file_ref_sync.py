"""Transparent FileReference persist / materialise for Temporal activities.

These helpers are called automatically by ``create_activity_from_task()``
to handle large-payload cross-task data transfer:

* Before a task runs, ``materialize_file_refs`` downloads any durable
  ``FileReference`` fields in the input to local temp files.
* After a task completes, ``persist_file_refs`` uploads any ephemeral
  ``FileReference`` fields in the output to the store and marks them
  durable.

This makes ``FileReference`` round-trips transparent to task authors.

SHA-256 sidecar verification
-----------------------------
After every persist, a ``{storage_path}.sha256`` sidecar is written to the
store and a ``{local_path}.sha256`` sidecar is written locally.  Before a
re-use of an already-materialised file (``local_path`` is set), the local
sidecar is checked:

* **Missing sidecar** — conservative default: assume the file must be
  re-verified.  ``materialize_file_reference`` will fetch the stored sidecar
  and, if the hashes agree, simply stamp the local sidecar without re-downloading.
* **Stat fingerprint matches** — the sidecar records the file's
  ``(size, mtime_ns, inode)`` next to its digest; while they still match, the
  file is reused without being read (FND-4081). This is the same-pod hand-off,
  and a multi-GB re-hash here used to hold the event loop for seconds.
* **Stat moved, or a digest-only sidecar** — the file is re-hashed in a worker
  thread. A match re-stamps the sidecar; a mismatch means the file is corrupt
  or partially written, so it is re-downloaded.

This ensures that:

1. A Temporal retry on a different worker (stale ``local_path``) always
   triggers a fresh download.
2. A crash mid-download (partial file, no sidecar) is detected and recovered.
3. Once a sidecar exists and matches, re-downloads are skipped entirely.
4. Concurrent activities sharing one durable ref (``local_path`` is a
   deterministic function of run, stage and entity) are safe: downloads
   stream into a temp file and land via ``os.replace``, and the
   materialise-and-verify step holds a per-path lock, so a second activity
   waits and reuses the file instead of re-downloading underneath the first
   (CONNECT-1126).
"""

from __future__ import annotations

import asyncio
import os

try:
    import fcntl
except ImportError:  # pragma: no cover — no flock on Windows; the sweep is then a no-op
    fcntl = None  # type: ignore[assignment]
import queue
import shutil
import threading
import uuid
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from application_sdk._runtime.offload import (
    drain_offloads,
    run_in_thread,
    tracking_offloads,
)
from application_sdk._runtime.progress import current_progress_tracker
from application_sdk.contracts.types import FileReference, Lazy, StoreTarget
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage._local_marker import (
    file_fingerprint,
    hash_fingerprinted,
    read_local_marker,
    trusted_digest,
)

if TYPE_CHECKING:
    from obstore.store import ObjectStore

logger = get_logger(__name__)


@dataclass(frozen=True)
class NamedFileRef:
    """One ``FileReference`` found in a contract tree, with where it was found.

    The *where* is what artifact validation needs and the bare ref cannot
    supply. Declarations are keyed by ``(entrypoint, contract field name)``, and
    whether a hand-off sits on an app's public boundary is a fact about the class
    that owns the field — neither is recoverable from a ``FileReference``, which
    carries only paths.
    """

    ref: FileReference
    """The reference itself."""

    field: str = ""
    """Name of the contract field it was reached through, or "" for a bare ref
    handed in at the top level."""

    owner: type | None = None
    """The model class declaring :attr:`field`, or ``None`` for a bare ref.

    For a ref inside a container (``list[FileReference]``,
    ``dict[str, FileReference]``) this is the model declaring the *container*
    field, so every element shares the one declaration keyed by that field name —
    which is how the declaration is keyed.
    """


def iter_named_file_refs(
    data: Any, *, field: str = "", owner: type | None = None
) -> Iterator[NamedFileRef]:
    """Walk a contract tree yielding every ``FileReference`` with its field name.

    The same traversal :func:`_find_file_refs` does — that function is written in
    terms of this one, so the two cannot drift into disagreeing about which refs
    a tree contains. Containers pass the enclosing ``(owner, field)`` down
    unchanged; a nested model re-keys to its own fields, so a ref on an inner
    model reports that inner model as its owner and is therefore never counted as
    a boundary hand-off.

    Args:
        data: Model, container or scalar to walk.
        field: Field name the recursion arrived through — internal.
        owner: Model class declaring ``field`` — internal.

    Yields:
        One :class:`NamedFileRef` per reference, in the tree's own field order.
    """
    if isinstance(data, FileReference):
        yield NamedFileRef(ref=data, field=field, owner=owner)
    elif isinstance(data, BaseModel):
        for name in type(data).model_fields:
            yield from iter_named_file_refs(
                getattr(data, name), field=name, owner=type(data)
            )
    elif isinstance(data, (list, tuple)):
        for item in data:
            yield from iter_named_file_refs(item, field=field, owner=owner)
    elif isinstance(data, dict):
        for value in data.values():
            yield from iter_named_file_refs(value, field=field, owner=owner)


def _find_file_refs(data: Any) -> list[FileReference]:
    """Recursively find all FileReference instances in a BaseModel/dataclass tree."""
    return [named.ref for named in iter_named_file_refs(data)]


def _is_lazy(node: BaseModel, name: str) -> bool:
    """Return True if field *name* on *node* carries the ``Lazy`` marker."""
    return any(isinstance(m, Lazy) for m in type(node).model_fields[name].metadata)


def _materialize_candidates(
    data: Any, *, log_lazy: bool = False
) -> list[FileReference]:
    """The refs ``materialize_file_refs`` considers in *data*, in tree order.

    Skips refs with ``auto_materialize=False`` (the app owns them) and refs on a
    ``Lazy`` field (the task fetches them itself).
    """
    found: list[FileReference] = []

    def _walk(node: Any) -> None:
        if isinstance(node, FileReference):
            if node.auto_materialize:
                found.append(node)
            return
        if isinstance(node, BaseModel):
            for name in type(node).model_fields:
                value = getattr(node, name)
                if isinstance(value, FileReference) and _is_lazy(node, name):
                    if log_lazy:
                        # conformance: ignore[L018] storage_path is in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes it to an indexed OTLP attribute — %-style would lose the promotion
                        logger.debug(
                            "file_ref.materialize.lazy_skipped",
                            storage_path=value.storage_path,
                        )
                    continue
                _walk(value)
        elif isinstance(node, (list, tuple)):
            for item in node:
                _walk(item)
        elif isinstance(node, dict):
            for v in node.values():
                _walk(v)

    _walk(data)
    return found


class _LocalCopy(Enum):
    """What a stat says about a durable ref's local copy, without reading it."""

    CURRENT = "current"
    """Nothing to do: not a durable ref, or its marker's stat still matches."""
    STALE = "stale"
    """Must materialise: no local file, no marker, a directory, or the size
    disagrees with the ref's ``size_bytes``."""
    UNVERIFIED = "unverified"
    """A file and a marker exist but the marker cannot be trusted on stat —
    its fingerprint moved, or it predates fingerprints. Only a hash decides."""


def _local_copy(ref: FileReference) -> _LocalCopy:
    """Classify *ref*'s local copy from a stat and its marker. Never hashes.

    Conservative: any I/O error reads as ``STALE`` so the caller triggers a
    fresh materialise. Directories are always ``STALE`` — they go through the
    full materialise check (list + per-file markers).
    """
    if not ref.is_durable or ref.storage_path is None:
        return _LocalCopy.CURRENT
    if ref.local_path is None:
        return _LocalCopy.STALE
    try:
        if trusted_digest(ref.local_path, size_bytes=ref.size_bytes) is not None:
            return _LocalCopy.CURRENT
        current = file_fingerprint(ref.local_path)
        if current is not None:
            size = current[0]
        elif os.path.isfile(ref.local_path):
            # A symlink to a regular file: never trusted on stat, but its
            # target's size is still checked against the producer's record.
            size = os.path.getsize(ref.local_path)
        else:
            # Missing or a directory: only a full materialise can produce
            # something to hand the task.
            return _LocalCopy.STALE
        if ref.size_bytes is not None and size != ref.size_bytes:
            return _LocalCopy.STALE
        if read_local_marker(ref.local_path) is None:
            return _LocalCopy.STALE
        return _LocalCopy.UNVERIFIED
    except Exception:
        logger.warning("Local sidecar check failed", exc_info=True)
        return _LocalCopy.STALE


async def _rehash_matches_marker(local_path: str) -> bool:
    """Hash *local_path* off the event loop; True if it matches its marker.

    A match re-stamps the marker with the file's current stat, so the next
    hand-off on this pod trusts it without hashing — this is also how a
    digest-only marker from an older SDK gets upgraded.
    """
    from application_sdk.storage.reference import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _write_local_sidecar,
    )

    try:
        marker = await run_in_thread(read_local_marker, local_path)
        if marker is None:
            return False
        hashed = await hash_fingerprinted(local_path)
    except Exception:
        logger.warning("sha256 verification failed for local sidecar", exc_info=True)
        return False
    # A file that changed while it was being hashed: the digest describes
    # bytes it may no longer hold, so it proves nothing.
    if not hashed.stable or hashed.value != marker.sha256:
        return False
    # A symlink re-stamps digest-only (fingerprint None) and is re-hashed
    # next time, as before FND-4081.
    await run_in_thread(
        _write_local_sidecar, local_path, hashed.value, hashed.fingerprint
    )
    return True


async def _needs_materialize(
    ref: FileReference, rehashes: dict[str, asyncio.Task[bool]] | None = None
) -> bool:
    """Return True if the durable ref needs to be downloaded (or re-verified).

    A durable ref needs materializing when any of the following is true:

    * ``local_path`` is ``None`` — file has never been downloaded.
    * ``local_path`` is set but the file no longer exists on disk
      (e.g. the activity retried on a different worker pod).
    * The local ``.sha256`` sidecar is missing — conservative default;
      ``materialize_file_reference`` will validate against the stored sidecar
      before deciding whether to skip the actual download.
    * The file's size differs from the ref's ``size_bytes``.
    * The sidecar's stat fingerprint no longer matches and the file's sha256,
      computed in a worker thread, does not match the sidecar either — the
      file is corrupt or was only partially written.

    Args:
        rehashes: Shared across the refs of one tree so refs naming the same
            ``local_path`` hash it once between them.
    """
    state = await run_in_thread(_local_copy, ref)
    if state is _LocalCopy.UNVERIFIED:
        path = ref.local_path
        assert path is not None  # UNVERIFIED implies a local file
        if rehashes is None:
            return not await _rehash_matches_marker(path)
        if path not in rehashes:
            rehashes[path] = asyncio.ensure_future(_rehash_matches_marker(path))
        return not await rehashes[path]
    return state is _LocalCopy.STALE


def has_refs_to_persist(data: Any) -> bool:
    """Return True if *data* contains ephemeral FileReferences (local but not durable)."""
    return any(
        ref.local_path is not None and not ref.is_durable
        for ref in _find_file_refs(data)
    )


def has_refs_to_materialize(data: Any) -> bool:
    """Return True if *data* may contain durable FileReferences to download.

    A cheap pre-check that only stats files and reads their markers, so it is
    safe on the event loop. Missing local file, stale ``local_path``
    (different worker), missing sidecar, or a size that disagrees with the
    ref's ``size_bytes`` all return True — and so does a local copy whose
    sidecar cannot be trusted on stat alone. ``materialize_file_refs`` hashes
    those in a worker thread and downloads only the ones that do not match.
    """
    return any(
        _local_copy(ref) is not _LocalCopy.CURRENT for ref in _find_file_refs(data)
    )


def _resolve_ref_store(
    ref: FileReference, store: ObjectStore, upstream_store: ObjectStore | None
) -> ObjectStore:
    """Return the store that holds *ref*'s bytes.

    ``UPSTREAM`` refs read *upstream_store*; with no upstream bound they fall
    back to *store*, mirroring ``App.download``'s own routing.

    The tag is trusted here because a payload from outside the run cannot
    carry it: the workflow boundary (``_validate_workflow_input``) and the
    ``@update`` relay in ``app/base.py`` demote ``UPSTREAM`` to ``DEPLOYMENT``
    on every ref they admit. Inside a run, only ``App.download`` /
    ``App.upload`` set it, and app code that builds a ref by hand is
    trusted code, like a hand-written ``storage_path``.
    """
    if ref.store is StoreTarget.UPSTREAM and upstream_store is not None:
        return upstream_store
    return store


def _materialize_key(ref: FileReference) -> str:
    """Dedup key for materialise: one download per (store, storage_path).

    The same key in two stores is two different objects, so the store is part
    of the key.
    """
    if ref.storage_path is None:
        return str(id(ref))
    return f"{ref.store.value}:{ref.storage_path}"


def _current_directories(dirs: dict[str, int]) -> set[str]:
    """The directories already complete on this worker, used where they are.

    Current means: the directory holds exactly the ref's ``file_count`` files
    (the set a directory upload sends) and every one has a marker that can be
    trusted on stat. That is what a producer's own directory looks like after
    it was persisted on this worker.
    """
    from application_sdk.storage.errors import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        UnsafeUploadPathError,
    )
    from application_sdk.storage.reference import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        _list_upload_files,
    )

    current: set[str] = set()
    for path, file_count in dirs.items():
        try:
            if not os.path.isdir(path) or os.path.islink(path):
                continue
            files = _list_upload_files(Path(path))
            if len(files) == file_count and all(
                trusted_digest(str(f)) is not None for f in files
            ):
                current.add(path)
        except (OSError, UnsafeUploadPathError):
            logger.debug("Directory not reusable in place: %s", path, exc_info=True)
    return current


def _empty_directories(paths: set[str]) -> set[str]:
    empty: set[str] = set()
    for path in paths:
        try:
            with os.scandir(path) as it:
                if next(it, None) is None:
                    empty.add(path)
        except OSError:
            continue
    return empty


async def _replace_refs(
    data: Any,
    store: ObjectStore,
    mode: str,
    output_path: str | None = None,
    *,
    upstream_store: ObjectStore | None = None,
    download_dir: str | None = None,
) -> Any:
    """Replace FileReference instances in a data tree using concurrent execution.

    Uses a three-pass approach:

    - **Pass 1 (collect)**: walk the tree, bucket unique operations by
      ``storage_path`` (materialize) or ``id(ref)`` (persist).  Refs sharing
      the same ``storage_path`` are coalesced — only the first-encountered ref
      triggers an actual download. Whether a ref needs materialising is
      decided once here, off the event loop, and reused by pass 3.
    - **Pass 2 (execute)**: run all unique coroutines concurrently under a
      bounded ``asyncio.Semaphore(MAX_CONCURRENT_STORAGE_TRANSFERS)``.
    - **Pass 3 (scatter)**: rebuild the tree synchronously via ``model_copy``
      for frozen ``BaseModel`` parents, writing results from the dict.
      Subsequent refs sharing a ``storage_path`` adopt the winner's
      ``local_path`` and emit a ``file_ref.materialize.dedup_hit`` debug event.

    Args:
        data: Input dataclass, list, tuple, dict, or scalar.
        store: obstore store used for upload/download.
        mode: ``"persist"`` or ``"materialize"``.
        output_path: Run-scoped base prefix passed through to
            ``persist_file_reference`` for ``RETAINED``-tier refs.
        upstream_store: Store a ref with ``store=UPSTREAM`` materialises
            from. ``None`` falls back to *store*.
        download_dir: When set, every ref that needs a download is
            downloaded to ``<download_dir>/<storage_path>`` instead of its own
            ``local_path`` or a system temp name, so the caller deletes every
            copy, finished or not, by deleting one folder. A ref whose local
            copy is current is used where it is and never moved.

    Returns:
        A new object tree with replaced FileReference instances (original
        objects are never mutated).

    If one operation fails, the others are cancelled, and they and the
    threads they started have finished before the error propagates, so
    nothing keeps writing after the caller has moved on.
    """
    from application_sdk.constants import (  # noqa: PLC0415 — circular: constants imported transitively across the SDK
        MAX_CONCURRENT_STORAGE_TRANSFERS,
    )
    from application_sdk.storage.reference import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        materialize_file_reference,
        persist_file_reference,
    )

    # ── Pass 1: collect unique operations ─────────────────────────────────────
    # materialize: dedup by storage_path — same remote file → one download.
    # persist:     dedup by id(ref)      — same Python object → one upload.
    _factories: dict[str, Callable[[], Coroutine[Any, Any, FileReference]]] = {}
    _winners: dict[str, FileReference] = {}
    # materialize: refs to check, in tree order. Whether each needs a download
    # is decided once, below, and read back by the scatter pass — the check
    # may hash a multi-GB file, so it must not run per pass (FND-4081).
    _candidates: list[FileReference] = []
    _needs: dict[int, bool] = {}
    _empty_dirs: set[str] = set()

    def _collect(node: Any) -> None:
        if isinstance(node, FileReference):
            if not node.auto_materialize:
                return  # app owns the lifecycle for this ref — skip
            if node.local_path is not None and not node.is_durable:
                key = str(id(node))
                if key not in _factories:
                    _ref = node
                    _factories[key] = lambda r=_ref: persist_file_reference(
                        store, r, output_path=output_path
                    )
                    _winners[key] = node
            return
        if isinstance(node, BaseModel):
            for name in type(node).model_fields:
                _collect(getattr(node, name))
        elif isinstance(node, (list, tuple)):
            for item in node:
                _collect(item)
        elif isinstance(node, dict):
            for v in node.values():
                _collect(v)

    if mode == "materialize":
        _candidates = _materialize_candidates(data, log_lazy=True)
    else:
        _collect(data)

    if _candidates:
        check_sem = asyncio.Semaphore(MAX_CONCURRENT_STORAGE_TRANSFERS)
        rehashes: dict[str, asyncio.Task[bool]] = {}

        async def _check(ref: FileReference) -> bool:
            async with check_sem:
                return await _needs_materialize(ref, rehashes)

        unique = list({id(ref): ref for ref in _candidates}.values())
        verdicts = await asyncio.gather(*[_check(ref) for ref in unique])
        _needs = {id(ref): needs for ref, needs in zip(unique, verdicts)}
        if download_dir is not None:
            dirs = {
                node.local_path: node.file_count
                for node in _candidates
                if _needs[id(node)]
                and node.file_count > 0
                and node.local_path is not None
            }
            if dirs:
                current = await run_in_thread(_current_directories, dirs)
                for node in _candidates:
                    if node.local_path in current:
                        _needs[id(node)] = False
            probe = {
                node.local_path
                for node in _candidates
                if _needs[id(node)]
                and node.file_count == 0
                and node.local_path is not None
            }
            if probe:
                _empty_dirs = await run_in_thread(_empty_directories, probe)
        for node in _candidates:
            if not _needs[id(node)]:
                continue
            key = _materialize_key(node)
            if key not in _factories:
                keep_empty_dir = node.local_path in _empty_dirs
                if download_dir is not None and not keep_empty_dir:
                    target = _download_target(download_dir, node, len(_factories))
                    _factories[key] = lambda r=node, t=target: (
                        materialize_file_reference(
                            _resolve_ref_store(r, store, upstream_store),
                            r.model_copy(update={"local_path": t}),
                        )
                    )
                else:
                    _factories[key] = lambda r=node: materialize_file_reference(
                        _resolve_ref_store(r, store, upstream_store), r
                    )
                _winners[key] = node

    if not _factories:
        return data  # nothing to do — skip gather entirely

    # ── Pass 2: execute concurrently under bounded semaphore ──────────────────
    sem = asyncio.Semaphore(MAX_CONCURRENT_STORAGE_TRANSFERS)

    async def _run(
        key: str, factory: Callable[[], Coroutine[Any, Any, FileReference]]
    ) -> tuple[str, FileReference]:
        async with sem:
            result = await factory()
        # One ref persisted or materialised is one unit of work (ADR-0018).
        # The per-file and per-part marks inside transfer/ops already keep a
        # single large ref visible; this coarser mark exists for the label,
        # which tells an operator the interceptor was moving typed I/O rather
        # than the app being mid-transfer on one file.
        current_progress_tracker().mark_progress(f"file_ref.{mode}")
        return key, result

    with tracking_offloads() as offloads:
        runs = [asyncio.ensure_future(_run(k, f)) for k, f in _factories.items()]
        try:
            pairs = await asyncio.gather(*runs)
        except BaseException:
            for run in runs:
                run.cancel()
            try:
                await asyncio.gather(*runs, return_exceptions=True)
            finally:
                await drain_offloads(offloads)
            raise
    _results: dict[str, FileReference] = dict(pairs)

    # ── Pass 3: scatter results back through the tree ─────────────────────────
    def _scatter(node: Any) -> Any:
        if isinstance(node, FileReference):
            if (
                mode == "persist"
                and node.local_path is not None
                and not node.is_durable
            ):
                return _results.get(str(id(node)), node)
            if mode == "materialize" and _needs.get(id(node), False):
                key = _materialize_key(node)
                result = _results.get(key, node)
                if node is not _winners.get(key):
                    # conformance: ignore[L018] storage_path/dedup_key/reused_local_path/local_path are in _KNOWN_EXTRA_KEYS; _build_extra_dict promotes them to indexed OTLP attributes — %-style would lose the promotion
                    logger.debug(
                        "file_ref.materialize.dedup_hit",
                        storage_path=node.storage_path,
                        dedup_key=key,
                        reused_local_path=result.local_path,
                        local_path=node.local_path,
                    )
                return result
            return node

        if isinstance(node, BaseModel):
            changes: dict[str, Any] = {}
            for name in type(node).model_fields:
                old_val = getattr(node, name)
                if (
                    mode == "materialize"
                    and isinstance(old_val, FileReference)
                    and _is_lazy(node, name)
                ):
                    continue  # leave lazy field as the original durable ref
                new_val = _scatter(old_val)
                if new_val is not old_val:
                    changes[name] = new_val
            return node.model_copy(update=changes) if changes else node

        if isinstance(node, list):
            new_list = [_scatter(item) for item in node]
            return new_list if any(n is not o for n, o in zip(new_list, node)) else node

        if isinstance(node, tuple):
            new_tuple = tuple(_scatter(item) for item in node)
            return new_tuple if new_tuple != node else node

        if isinstance(node, dict):
            new_dict = {k: _scatter(v) for k, v in node.items()}
            return new_dict if any(new_dict[k] is not node[k] for k in node) else node

        return node

    return _scatter(data)


async def persist_file_refs(
    store: ObjectStore, data: Any, output_path: str | None = None
) -> Any:
    """Upload all ephemeral FileReferences in *data* to the store.

    Args:
        store: Destination obstore store.
        data: Dataclass tree potentially containing ephemeral
            ``FileReference`` objects.
        output_path: Run-scoped base prefix (e.g.
            ``artifacts/apps/{app}/workflows/{wf_id}/{run_id}``).  Required
            for any ``RETAINED``-tier ``FileReference`` in *data*.

    Returns:
        New object tree with all ephemeral FileReferences replaced by
        durable ones (with sha256 sidecars written).
    """
    return await _replace_refs(data, store, "persist", output_path=output_path)


async def materialize_file_refs(
    store: ObjectStore,
    data: Any,
    *,
    upstream_store: ObjectStore | None = None,
    download_dir: str | None = None,
) -> Any:
    """Download all durable FileReferences in *data* to local temp files.

    Each ref is read from the store it names: *upstream_store* for
    ``store=UPSTREAM`` (what ``App.download`` / ``App.upload`` return in a
    two-store deployment), *store* for everything else.

    Handles:

    * Normal case: ``local_path`` is ``None`` → download + write sidecar.
    * Retry on different worker: ``local_path`` set but file gone → download.
    * Missing sidecar: file exists but no ``.sha256`` sidecar → validate
      against stored sidecar; skip download if hashes agree.
    * Corrupt file: sidecar mismatch → re-download.

    Returns:
        New object tree with all durable FileReferences replaced by local
        ones (``local_path`` set, ``.sha256`` sidecar written).
    """
    return await _replace_refs(
        data,
        store,
        "materialize",
        upstream_store=upstream_store,
        download_dir=download_dir,
    )


_LOCK_NAME = ".owner.lock"
_CREATING_SUFFIX = ".creating"

_pending_removals: set[asyncio.Future[None]] = set()
_held_lock_fds: set[int] = set()
_held_lock_fds_guard = threading.Lock()
_removal_queue: queue.SimpleQueue[
    tuple[str, int, asyncio.AbstractEventLoop, asyncio.Future[None]]
] = queue.SimpleQueue()
_removal_thread: threading.Thread | None = None
_removal_thread_guard = threading.Lock()


@dataclass(frozen=True)
class TaskInputDir:
    """One task's download folder and the lock that marks its owner alive."""

    path: str
    lock_fd: int


def _task_inputs_root() -> str:
    from application_sdk.constants import (  # noqa: PLC0415 — circular: constants imported transitively across the SDK
        TEMPORARY_PATH,
    )
    from application_sdk.storage.ops import (  # noqa: PLC0415 — circular: storage/__init__.py loads sibling modules
        TASK_INPUTS_DIRNAME,
    )

    return os.path.abspath(os.path.join(TEMPORARY_PATH, TASK_INPUTS_DIRNAME))


def _close_lock(fd: int) -> None:
    with _held_lock_fds_guard:
        _held_lock_fds.discard(fd)
    try:
        os.close(fd)
    except OSError:
        pass


def _close_held_locks_in_child() -> None:
    _held_lock_fds_guard.release()
    for fd in list(_held_lock_fds):
        try:
            os.close(fd)
        except OSError:
            pass
    _held_lock_fds.clear()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(
        before=_held_lock_fds_guard.acquire,
        after_in_parent=_held_lock_fds_guard.release,
        after_in_child=_close_held_locks_in_child,
    )


def _create_owned_dir(path: str) -> int:
    """Create *path* with its owner lock already held, then make it visible.

    The folder is built under a hidden ``.<id>.creating`` name, which the
    sweep never touches, and renamed into place only once its lock is held:
    a visible folder therefore always has a held lock until its owner
    deletes it.
    """
    parent, name = os.path.split(path)
    staging = os.path.join(parent, f".{name}{_CREATING_SUFFIX}")
    os.makedirs(staging)
    fd: int | None = None
    try:
        with _held_lock_fds_guard:
            fd = os.open(
                os.path.join(staging, _LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o600
            )
            _held_lock_fds.add(fd)
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.rename(staging, path)
    except BaseException:
        if fd is not None:
            _close_lock(fd)
        _remove_tree(staging)
        raise
    return fd


async def open_task_input_dir() -> TaskInputDir:
    """Create one task's download folder and hold its owner lock (FND-4310).

    Lives under ``TEMPORARY_PATH``, the SDK's working volume, so downloads land
    where they always did. ``materialize_file_refs(..., download_dir=...)``
    downloads into it; :func:`remove_task_input_dir` deletes it when the task
    ends. The exclusive ``flock`` on its lock file lasts as long as the task,
    so :func:`sweep_dead_task_inputs` can tell a live folder from a dead one.
    A cancel while the folder is being created still deletes it once the
    creation finishes.
    """
    path = os.path.join(_task_inputs_root(), uuid.uuid4().hex)
    creating = asyncio.ensure_future(run_in_thread(_create_owned_dir, path))
    try:
        fd = await asyncio.shield(creating)
    except asyncio.CancelledError:

        def _discard(done: asyncio.Future[int]) -> None:
            if not done.cancelled() and done.exception() is None:
                remove_task_input_dir(TaskInputDir(path=path, lock_fd=done.result()))

        creating.add_done_callback(_discard)
        raise
    return TaskInputDir(path=path, lock_fd=fd)


def _download_target(download_dir: str, ref: FileReference, slot: int) -> str:
    """``<download_dir>/<slot>/<storage_path>``, or a fresh name in the slot.

    One slot per distinct object (store + key), so the same key in two stores,
    or keys that nest (``a`` and ``a/b``), never share a path. Keeping the
    store key as the relative path keeps the layout readers rely on (a
    ``transformed`` segment, sibling files). A key that would leave the slot
    gets a fresh name with the key's suffix.
    """
    base = os.path.join(download_dir, str(slot))
    key = ref.storage_path or ""
    target = os.path.normpath(os.path.join(base, key.lstrip("/")))
    try:
        inside = target != base and os.path.commonpath([base, target]) == base
    except ValueError:  # conformance: ignore[E009] os.path.commonpath raises on mixed Windows drives; such a key leaves the folder
        inside = False
    if inside:
        return target
    return os.path.join(base, uuid.uuid4().hex + PurePosixPath(key).suffix)


def _ignore_missing(func: Any, path: str, exc_info: Any) -> None:
    if not isinstance(exc_info[1], FileNotFoundError):
        raise exc_info[1]


def _remove_tree(path: str) -> bool:
    try:
        shutil.rmtree(path, onerror=_ignore_missing)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not delete a task's downloaded inputs", exc_info=True)
        return False
    return True


def _remove_owned(path: str, lock_fd: int | None) -> bool:
    """Delete *path*'s content first and its lock file last, then release the lock.

    If the content cannot all be deleted, the lock file stays: once the lock
    is released, the next sweep finds the folder dead and retries.
    """
    try:
        try:
            entries = list(os.scandir(path))
        except FileNotFoundError:
            return True
        except OSError:
            logger.warning("Could not delete a task's downloaded inputs", exc_info=True)
            return False
        complete = True
        for entry in entries:
            if entry.name == _LOCK_NAME:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path, onerror=_ignore_missing)
                else:
                    os.remove(entry.path)
            except FileNotFoundError:
                pass
            except OSError:
                complete = False
                logger.warning(
                    "Could not delete a task's downloaded inputs", exc_info=True
                )
        if complete:
            complete = _remove_tree(path)
        return complete
    finally:
        if lock_fd is not None:
            _close_lock(lock_fd)


def _removal_worker() -> None:
    while True:
        path, lock_fd, loop, done = _removal_queue.get()
        try:
            if not _remove_owned(path, lock_fd):
                _sweep_dead_task_inputs()
        except Exception:
            logger.warning("Could not delete a task's downloaded inputs", exc_info=True)
        finally:
            try:
                loop.call_soon_threadsafe(_finish_removal, done)
            except RuntimeError:
                pass


def _finish_removal(done: asyncio.Future[None]) -> None:
    if not done.done():
        done.set_result(None)


def _start_removal_thread() -> None:
    global _removal_thread
    with _removal_thread_guard:
        if _removal_thread is None or not _removal_thread.is_alive():
            _removal_thread = threading.Thread(
                target=_removal_worker, name="sdk-task-input-removal", daemon=True
            )
            _removal_thread.start()


def remove_task_input_dir(owned: TaskInputDir) -> None:
    """Queue a task's download folder for deletion. Never raises.

    One daemon thread deletes queued folders: never the loop's default
    executor (Temporal uses it) nor the SDK's blocking pool (downloads need
    it, and its threads are joined at exit, so a long delete would hold a
    stopping pod up). A delete cut short by process exit is finished by the
    next worker's sweep. Not awaited, so the wrapper's ``finally`` has no
    cancellation point here and never replaces the error the task ends with.
    The owner lock is released after the folder is gone; if the folder cannot
    all be deleted, a sweep retries at once. If the delete cannot be queued,
    the lock is released, so the next sweep deletes the folder.
    """
    try:
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()
        _start_removal_thread()
        _removal_queue.put((owned.path, owned.lock_fd, loop, done))
    except Exception:
        _close_lock(owned.lock_fd)
        logger.warning("Could not start deleting a task's downloaded inputs", exc_info=True)
        return
    _pending_removals.add(done)
    done.add_done_callback(_pending_removals.discard)


async def wait_for_task_input_removals(timeout: float) -> None:
    """Wait up to *timeout* seconds for queued folder deletions. Never raises.

    For worker shutdown: an unfinished deletion is logged and left to the next
    worker's sweep.
    """
    pending = list(_pending_removals)
    if not pending or timeout <= 0:
        if pending:
            logger.warning(
                "Shutdown left %d task download folders to the next sweep",
                len(pending),
            )
        return
    _, unfinished = await asyncio.wait(pending, timeout=timeout)
    if unfinished:
        logger.warning(
            "Shutdown left %d task download folders to the next sweep",
            len(unfinished),
        )


def _sweep_dead_task_inputs() -> None:
    if fcntl is None:
        return
    root = _task_inputs_root()
    try:
        entries = list(os.scandir(root))
    except FileNotFoundError:
        return
    except OSError:
        logger.warning("Could not list leftover task downloads", exc_info=True)
        return
    for entry in entries:
        if entry.name.startswith("."):
            continue
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
            fd = os.open(os.path.join(entry.path, _LOCK_NAME), os.O_RDWR)
        except FileNotFoundError:
            _remove_owned(entry.path, None)
            continue
        except OSError:
            continue
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            continue
        _remove_owned(entry.path, fd)


async def sweep_dead_task_inputs() -> None:
    """Delete task download folders whose owner is dead. Never raises.

    A worker killed mid-task (OOM, SIGKILL) never reaches its ``finally``; the
    kernel drops its ``flock`` when the process dies. A visible folder whose
    lock this sweep can take, or that has no lock file at all, has no live
    owner in any process on the node, so it goes; a folder whose lock is held
    belongs to a running task and is skipped. Folders still being created
    (``.<id>.creating``) are never touched.
    """
    await run_in_thread(_sweep_dead_task_inputs)


def needs_task_input_dir(data: Any) -> bool:
    """True when materialise may download something for *data*.

    Same walk as materialise (``Lazy`` and ``auto_materialize=False`` refs are
    never downloaded), so a task with nothing to download gets no folder.
    """
    return any(
        _local_copy(ref) is not _LocalCopy.CURRENT
        for ref in _materialize_candidates(data)
    )
