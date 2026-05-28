"""Disk-backed dictionary backed by RocksDB.

Provides a dict-like interface for storing key-value pairs on disk, optimized
for write-heavy connector workloads (entity location maps, lineage processing,
metadata verification, etc.).

Replaces the historical `sqlitedict` pattern that several connectors adopted
locally. `sqlitedict` is unmaintained and has no upstream fix for
CVE-2024-35515 (pickle-based arbitrary code execution), which made it a
chronic allowlist entry across our scan pipeline.

Requires the ``incremental`` extra (or ``rocksdict`` directly)::

    pip install atlan-application-sdk[incremental]
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

try:
    from rocksdict import Options, Rdict
except ImportError:
    Rdict = None  # type: ignore[misc, assignment]
    Options = None  # type: ignore[misc, assignment]


class DiskBackedDict:
    """A disk-backed dictionary using RocksDB.

    Dict-like interface backed by RocksDB, optimized for write-heavy workloads
    via LSM-tree storage. RocksDB handles write buffering internally via MemTable,
    so callers don't need to manage explicit commits.

    The backing store is a temporary directory created on construction and
    removed on ``close()`` / context-manager exit. Use only for ephemeral
    per-run state — there is no API for persistent reuse across runs.

    Example::

        with DiskBackedDict() as d:
            d["k"] = [1, 2, 3]
            d.append_to_key("k", 4)
            assert d["k"] == [1, 2, 3, 4]
            for k, v in d.items():
                ...

    Notes:
        - Values are pickled by RocksDict on write. Don't put untrusted
          payloads in here — the unpickle on read is the same risk surface
          sqlitedict had (CVE-2024-35515).
        - ``__len__`` returns RocksDB's estimate, not a true count.
    """

    def __init__(self) -> None:
        if Rdict is None:
            raise ImportError(
                "rocksdict is required for DiskBackedDict — install with "
                "`pip install atlan-application-sdk[incremental]` or add "
                "`rocksdict>=0.3.0` to your project deps."
            )

        self._temp_dir = tempfile.mkdtemp(prefix="atlan_disk_backed_dict_")
        options = Options()
        # Smaller MemTable than RocksDB's 64MB default — connectors often
        # spin up many of these in parallel under tight memory limits.
        options.set_write_buffer_size(16 * 1024 * 1024)  # 16MB
        self._db = Rdict(self._temp_dir, options=options)

    def __setitem__(self, key: str, value: Any) -> None:
        self._db[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._db[key]

    def get(self, key: str, default: Any = None) -> Any:
        # Bloom filter check first — fast in-memory negative lookup.
        if not self._db.key_may_exist(key):
            return default
        return self._db.get(key, default)

    def items(self) -> Iterator[tuple[Any, Any]]:
        for key, value in self._db.items():
            yield key, value

    def __contains__(self, key: str) -> bool:
        return key in self._db

    def __len__(self) -> int:
        """Estimated key count.

        RocksDB exposes an estimate (sum of inserts/updates seen by the LSM),
        not an exact count. Use this for rough sizing, not invariants.
        """
        return int(self._db.property_value("rocksdb.estimate-num-keys") or 0)

    def append_to_key(self, key: str, value: Any) -> None:
        """Append a value to the list stored at ``key``.

        Handles the common read-modify-write pattern atomically from the
        caller's perspective (no concurrent-writer safety — RocksDB is
        single-process here).

        Args:
            key: Dictionary key. Creates a new list if absent.
            value: Value to append to the list at ``key``.
        """
        current = self.get(key, [])
        current.append(value)
        self[key] = current

    def close(self) -> None:
        """Close the database and remove its temporary directory."""
        self._cleanup()

    def _cleanup(self) -> None:
        try:
            self._db.close()
        except Exception:
            logger.warning("Failed to close RocksDB cleanly", exc_info=True)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def __enter__(self) -> "DiskBackedDict":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._cleanup()
