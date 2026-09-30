"""The committed current-state snapshot of one connection.

``CurrentStateStore`` owns the object-store side of incremental state: what is
the last committed snapshot (``probe``), put it on local disk
(``materialize``), and replace it with a new one (``commit``). The public
layout is unchanged — every snapshot still lives at::

    persistent-artifacts/apps/{app}/connection/{connection_id}/current-state/{entity}/*.json

because Argo publish reads that prefix directly, and so do connector apps that
carry state forward on their own. Two things are new underneath it.

**A manifest, written last.** ``current-state/.sdk-manifest`` names every key
of the committed snapshot. ``commit`` uploads the snapshot, then writes the
manifest, then prunes every key the manifest does not name. A reader trusts
the manifest over the listing, so a commit that died part-way — before its
manifest — is invisible: the next run still reads the previous snapshot
whole. The name is dot-prefixed and has no ``.json`` suffix because the
publish converter globs the prefix root with ``**/*.json``; a root
``_manifest.json`` would be parsed as asset records.

**Run-stamped file names.** An upload that overwrote the previous snapshot's
keys in place could not be undone by any manifest: the bytes would already be
gone. So each committed file name carries a token derived from the committing
run (``{token}--chunk-0.json``), and a new commit never writes a key the
previous manifest names. A same-run retry derives the same token, so it
overwrites its own partial upload and nothing else. Readers glob
``{entity}/*.json`` and are indifferent to the name.

A snapshot written before the manifest existed has no manifest; its listing
is trusted, minus any run-stamped key — those only ever come from a commit
that did not reach its manifest.

Local directories are the caller's: ``materialize`` mirrors the snapshot into
whatever directory it is given (the template uses the run-scoped
``{output_path}/incremental/previous-state``), under a per-directory lock, with
``download_prefix``'s *sync* semantics — a same-run retry resumes instead of
starting over, and a partial tree from a killed attempt is completed and
pruned rather than trusted.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import orjson

from application_sdk._runtime.offload import run_in_thread
from application_sdk.common._listing import prune_internal_dirs
from application_sdk.common.incremental.helpers import get_persistent_s3_prefix
from application_sdk.common.incremental.incremental_errors import (
    CurrentStateManifestError,
)
from application_sdk.observability.logger_adaptor import get_logger
from application_sdk.storage._concurrency import _run_drained
from application_sdk.storage._locks import PathLockRegistry
from application_sdk.storage.batch import (
    DataObject,
    _delete_paths_individually,
    _download_listed,
    list_data_objects,
    list_keys,
    upload_prefix,
)
from application_sdk.storage.integrity import sidecar_key
from application_sdk.storage.ops import _get_bytes, _put, _resolve_store, normalize_key

if TYPE_CHECKING:
    from obstore.store import ObjectStore

logger = get_logger(__name__)

#: Object name of the manifest, directly under the current-state prefix.
MANIFEST_NAME = ".sdk-manifest"

_MANIFEST_VERSION = 1

#: ``{12 hex}--`` — the run stamp ``commit`` puts in front of a file name.
_STAMP_RE = re.compile(r"^[0-9a-f]{12}--")

_MATERIALIZE_LOCKS = PathLockRegistry("incremental.current_state.materialize.lock_wait")


def _run_stamp(run_id: str) -> str:
    """The file-name stamp for *run_id*; stable across a run's retries."""
    return hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:12] + "--"


def _is_stamped(relative_key: str) -> bool:
    return bool(_STAMP_RE.match(relative_key.rsplit("/", 1)[-1]))


@dataclass(frozen=True)
class CurrentStateSnapshot:
    """One committed current-state snapshot, as found by :meth:`CurrentStateStore.probe`.

    Attributes:
        s3_prefix: The current-state prefix the snapshot lives under.
        exists: Whether the snapshot holds any JSON entity file.
        json_count: Number of ``.json`` keys in the snapshot.
        total_bytes: Combined size of the snapshot's keys.
        committed_run_id: The run that committed it, or ``None`` for a snapshot
            written before manifests existed (or no snapshot at all).
        keys: Every object key of the snapshot, excluding the manifest and
            integrity sidecars.
    """

    s3_prefix: str
    exists: bool
    json_count: int
    total_bytes: int
    committed_run_id: str | None
    keys: tuple[str, ...]
    #: The listing entries behind ``keys`` (size, etag, sidecar flag), carried
    #: so ``materialize`` does not list the prefix a second time.
    _objects: tuple[DataObject, ...] = field(default=(), repr=False, compare=False)


@dataclass(frozen=True)
class RunStateDirs:
    """The run-scoped local directories of one incremental run.

    Everything lives under ``{output_path}/incremental/``, so two runs of one
    connection never share a directory, and a same-run retry finds its own.
    """

    root: Path

    @classmethod
    def for_output_path(cls, output_path: str | Path) -> RunStateDirs:
        return cls(Path(output_path) / "incremental")

    @property
    def previous_state(self) -> Path:
        """Where the previous committed snapshot is materialized."""
        return self.root / "previous-state"

    @property
    def current_state(self) -> Path:
        """Where this run's snapshot is built before its commit."""
        return self.root / "current-state"

    @property
    def diff(self) -> Path:
        """Where this run's incremental diff is built before its upload."""
        return self.root / "diff"


class CurrentStateStore:
    """Probe, materialize and commit one connection's current-state snapshot.

    Args:
        s3_prefix: The current-state prefix itself
            (``persistent-artifacts/apps/{app}/connection/{id}/current-state``).
        store: Object store, or ``None`` to use the infrastructure store.
    """

    def __init__(self, s3_prefix: str, store: ObjectStore | None = None) -> None:
        self.s3_prefix = s3_prefix.rstrip("/")
        self._store = store
        self._key_prefix = normalize_key(self.s3_prefix).rstrip("/") + "/"
        self.manifest_key = self._key_prefix + MANIFEST_NAME

    @classmethod
    def for_connection(
        cls,
        connection_qualified_name: str,
        application_name: str = "",
        store: ObjectStore | None = None,
    ) -> CurrentStateStore:
        """The store for a connection's ``current-state/`` prefix."""
        prefix = get_persistent_s3_prefix(connection_qualified_name, application_name)
        return cls(f"{prefix}/current-state", store)

    # ------------------------------------------------------------------
    # probe
    # ------------------------------------------------------------------

    async def probe(self) -> CurrentStateSnapshot:
        """Find the committed snapshot with one listing (plus the manifest read).

        Downloads nothing but the manifest. With a manifest, exactly its keys
        are the snapshot; without one, the listing is — minus run-stamped keys,
        which only a commit that never reached its manifest leaves behind.

        Raises:
            CurrentStateManifestError: If the manifest is unreadable or names a
                key the listing does not hold.
            StorageError: If the listing or the manifest read fails.
        """
        listing = await list_data_objects(self._key_prefix, self._store)
        has_manifest = any(o.key == self.manifest_key for o in listing)
        raw = await _get_bytes(self.manifest_key, self._store) if has_manifest else None
        # Offloaded: at 100k keys the decode and the reconcile below are one
        # pass each over the whole snapshot, with no await in between.
        return await run_in_thread(self._reconcile, listing, raw)

    def _reconcile(
        self, listing: list[DataObject], raw_manifest: bytes | None
    ) -> CurrentStateSnapshot:
        data = [o for o in listing if o.key != self.manifest_key]
        committed_run_id: str | None = None
        if raw_manifest is None:
            chosen = [
                o for o in data if not _is_stamped(o.key[len(self._key_prefix) :])
            ]
            skipped = len(data) - len(chosen)
            if skipped:
                logger.warning(
                    "Current-state at %s has no manifest; ignoring %d key(s) left "
                    "by a commit that did not complete",
                    self.s3_prefix,
                    skipped,
                )
        else:
            committed_run_id, named = self._parse_manifest(raw_manifest)
            by_key = {o.key: o for o in data}
            missing = [k for k in named if self._key_prefix + k not in by_key]
            if missing:
                raise CurrentStateManifestError(
                    message=(
                        f"Current-state manifest names {len(missing)} key(s) the "
                        f"store does not hold (first: {missing[0]!r})"
                    ),
                    manifest_key=self.manifest_key,
                )
            chosen = [by_key[self._key_prefix + k] for k in named]
        chosen.sort(key=lambda o: o.key)
        json_count = sum(1 for o in chosen if o.key.endswith(".json"))
        return CurrentStateSnapshot(
            s3_prefix=self.s3_prefix,
            exists=json_count > 0,
            json_count=json_count,
            total_bytes=sum(o.size for o in chosen),
            committed_run_id=committed_run_id,
            keys=tuple(o.key for o in chosen),
            _objects=tuple(chosen),
        )

    def _parse_manifest(self, raw: bytes) -> tuple[str | None, list[str]]:
        try:
            doc = orjson.loads(raw)
            if not isinstance(doc, dict) or doc.get("version") != _MANIFEST_VERSION:
                raise ValueError(f"unsupported manifest version: {doc!r:.80}")
            keys = doc["keys"]
            run_id = doc.get("run_id")
            if not isinstance(keys, dict) or not all(isinstance(k, str) for k in keys):
                raise ValueError("manifest 'keys' is not an object of names")
        except (ValueError, KeyError, TypeError) as exc:
            raise CurrentStateManifestError(
                message=f"Current-state manifest is unreadable: {exc}",
                manifest_key=self.manifest_key,
            ) from exc
        return (run_id if isinstance(run_id, str) else None), list(keys)

    # ------------------------------------------------------------------
    # materialize
    # ------------------------------------------------------------------

    async def materialize(
        self,
        snapshot: CurrentStateSnapshot,
        dest: Path,
        *,
        max_concurrency: int = 4,
    ) -> Path:
        """Mirror *snapshot* into *dest*; return *dest*.

        *dest* ends up holding exactly the snapshot's files, laid out as under
        the prefix (``dest/table/...``). Files already current from an earlier
        materialize into the same directory are not downloaded again, and
        anything else in it — a killed attempt's partial tree included — is
        deleted. Serialised per directory, so two same-worker callers of one
        destination cannot interleave.

        Raises:
            CurrentStateManifestError: If *snapshot* names keys the store no
                longer holds (only when the snapshot was built by hand).
            StorageError: If a download fails.
            StorageIntegrityError: If an object does not match its sidecar.
        """
        objects = list(snapshot._objects)
        if len(objects) != len(snapshot.keys):
            listing = {
                o.key: o for o in await list_data_objects(self._key_prefix, self._store)
            }
            missing = [k for k in snapshot.keys if k not in listing]
            if missing:
                raise CurrentStateManifestError(
                    message=(
                        f"Snapshot names {len(missing)} key(s) the store does not "
                        f"hold (first: {missing[0]!r})"
                    ),
                    manifest_key=self.manifest_key,
                )
            objects = [listing[k] for k in snapshot.keys]

        async with _MATERIALIZE_LOCKS.guard(str(dest)):
            dest.mkdir(parents=True, exist_ok=True)
            await _download_listed(
                objects,
                self._key_prefix,
                dest,
                self._store,
                suffix="",
                normalize=False,
                strip_prefix=True,
                max_concurrency=max_concurrency,
                sync=True,
            )
        logger.info(
            "Current-state materialized: %d key(s) from %s into %s",
            len(objects),
            self.s3_prefix,
            dest,
        )
        return dest

    # ------------------------------------------------------------------
    # commit
    # ------------------------------------------------------------------

    async def commit(
        self,
        local_dir: Path,
        run_id: str,
        *,
        max_concurrency: int = 4,
    ) -> CurrentStateSnapshot:
        """Replace the committed snapshot with the tree under *local_dir*.

        1. Stamp every file name with *run_id*'s token (renamed in place, so
           *local_dir* keeps mirroring what is committed).
        2. Upload the tree.
        3. Write the manifest — the commit point.
        4. Delete every key under the prefix the manifest does not name.

        A failure before step 3 leaves the previous snapshot committed and
        intact; a failure in step 4 leaves stale keys that the next commit
        prunes and that every reader already ignores.

        Raises:
            StorageError: If an upload, the manifest write, or the prune fails.
            OSError: If *local_dir* cannot be walked or renamed.
        """
        stamp = _run_stamp(run_id)
        sizes = await _run_drained(run_in_thread(_stamp_tree, local_dir, stamp))
        uploaded = await upload_prefix(
            local_dir=str(local_dir),
            prefix=self._key_prefix.rstrip("/"),
            store=self._store,
            normalize=False,
            max_concurrency=max_concurrency,
        )
        relative = sorted(k[len(self._key_prefix) :] for k in uploaded)
        manifest = {
            "version": _MANIFEST_VERSION,
            "run_id": run_id,
            "committed_at": datetime.now(UTC).isoformat(),
            "keys": {k: sizes.get(k, 0) for k in relative},
        }
        await _put(self.manifest_key, orjson.dumps(manifest), self._store)
        logger.info(
            "Current-state committed: %d key(s) under %s (run %s)",
            len(relative),
            self.s3_prefix,
            run_id,
        )
        await self._prune(set(uploaded), run_id)

        json_count = sum(1 for k in relative if k.endswith(".json"))
        return CurrentStateSnapshot(
            s3_prefix=self.s3_prefix,
            exists=json_count > 0,
            json_count=json_count,
            total_bytes=sum(manifest["keys"].values()),
            committed_run_id=run_id,
            keys=tuple(sorted(uploaded)),
        )

    async def _prune(self, committed: set[str], run_id: str) -> None:
        # Re-read the manifest first: if a concurrent run of this connection
        # committed after us, its keys are the live snapshot and pruning to
        # ours would delete them. That run prunes on its own commit.
        raw = await _get_bytes(self.manifest_key, self._store)
        live_run = self._parse_manifest(raw)[0] if raw is not None else None
        if live_run != run_id:
            logger.warning(
                "Current-state at %s was re-committed by another run during this "
                "commit; leaving the prune to it",
                self.s3_prefix,
            )
            return
        keep = committed | {sidecar_key(k) for k in committed} | {self.manifest_key}
        existing = await list_keys(
            self._key_prefix, self._store, normalize=False, include_markers=True
        )
        stale = [k for k in existing if k not in keep]
        if stale:
            await _delete_paths_individually(_resolve_store(self._store), stale)
            logger.info(
                "Pruned %d key(s) outside the committed current-state manifest",
                len(stale),
            )


def _stamp_tree(root: Path, stamp: str) -> dict[str, int]:
    """Rename every file under *root* to carry *stamp*; return ``{relpath: size}``.

    An existing stamp (a file carried forward from a materialized snapshot) is
    replaced rather than stacked, so names do not grow run over run. Symlinks
    and SDK working directories are skipped, matching ``upload_prefix``.
    Blocking: callers offload it.
    """
    sizes: dict[str, int] = {}
    if not root.exists():
        return sizes
    for dirpath, dirs, filenames in os.walk(root, followlinks=False):
        prune_internal_dirs(dirs)
        # Unstamped names first: they claim the plain ``{stamp}{name}``, and a
        # carried-forward ``{old}--{name}`` beside one falls back to keeping
        # its old stamp rather than overwriting the fresh file.
        for name in sorted(filenames, key=lambda n: (bool(_STAMP_RE.match(n)), n)):
            path = Path(dirpath) / name
            if path.is_symlink():
                continue
            if not name.startswith(stamp):
                candidates = [stamp + _STAMP_RE.sub("", name, count=1)]
                if _STAMP_RE.match(name):
                    candidates.append(stamp + name)
                target = next(
                    (
                        path.with_name(c)
                        for c in candidates
                        if not path.with_name(c).exists()
                    ),
                    None,
                )
                if target is None:
                    raise FileExistsError(
                        f"cannot stamp {path}: {candidates[-1]} already exists"
                    )
                os.replace(path, target)
                path = target
            sizes[path.relative_to(root).as_posix()] = path.stat().st_size
    return sizes
