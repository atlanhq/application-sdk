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

Related: ``application_sdk.common.incremental.storage.rocksdb_utils``
provides a lower-level Rdict factory (``create_states_db`` /
``close_states_db``) used by TableScope for incremental-extract state. The
two utilities coexist deliberately — TableScope wants raw Rdict, callers
that want dict-like semantics want this class.
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

try:
    from rocksdict import BlockBasedOptions, Options, Rdict
except ImportError:
    Rdict = None  # type: ignore[misc, assignment]
    Options = None  # type: ignore[misc, assignment]
    BlockBasedOptions = None  # type: ignore[misc, assignment]


class DiskBackedDict:
    """A disk-backed dictionary using RocksDB.

    Dict-like interface backed by RocksDB, optimized for write-heavy workloads
    via LSM-tree storage. RocksDB handles write buffering internally via MemTable,
    so callers don't need to manage explicit commits.

    The backing store is a temporary directory created on construction and
    removed on ``close()`` / context-manager exit. Use only for ephemeral
    per-run state — there is no API for persistent reuse across runs.

    **Keys must be strings.** Values may be any picklable Python object.

    **Not thread-safe.** Serialize access externally if multiple threads or
    coroutines may touch the same key.

    **Caveat on untrusted data.** Values are pickled by RocksDict on write and
    unpickled on read. Do not store data whose bytes are attacker-controllable
    (raw payloads from untrusted HTTP, arbitrary user-provided blobs, etc.) —
    the unpickle-on-read is the same risk surface that CVE-2024-35515 named
    for sqlitedict. This class is appropriate for caching trusted-source
    metadata (rows from authenticated data sources, lineage graphs, location
    maps); it is not a hardened deserializer. Auditors of the migration
    follow-up PRs should verify what the caller actually stores here.

    Example::

        with DiskBackedDict() as d:
            d["k"] = [1, 2, 3]
            d.append_to_key("k", 4)
            assert d["k"] == [1, 2, 3, 4]
            for k, v in d.items():
                ...

    Notes:
        - ``__len__`` returns RocksDB's estimate, not a true count.
        - Iteration is in RocksDB's internal key order, not insertion order.
    """

    def __init__(self) -> None:
        if Rdict is None:
            # rocksdict ships Rdict/Options/BlockBasedOptions as a unit;
            # checking one is sufficient.
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
        # Bloom filter for fast negative lookups in get() / __contains__.
        # 10 bits/key gives ~1% false-positive rate; we don't subscribe to
        # a hot read path that needs more.
        bbo = BlockBasedOptions()
        bbo.set_bloom_filter(bits_per_key=10, block_based=False)
        options.set_block_based_table_factory(bbo)
        self._db = Rdict(self._temp_dir, options=options)
        self._closed = False

    def __setitem__(self, key: str, value: Any) -> None:
        # Reminder for callers: value is pickled. See class docstring's
        # "Caveat on untrusted data" — don't store attacker-controllable
        # bytes here.
        self._db[key] = value

    def __getitem__(self, key: str) -> Any:
        return self._db[key]

    def __delitem__(self, key: str) -> None:
        # rocksdict's __delitem__ maps to RocksDB Delete, which is a no-op
        # on missing keys (writes a tombstone unconditionally). We want
        # standard dict semantics — match what sqlitedict callers expect.
        # Bloom filter short-circuits the negative case; the explicit
        # `in self._db` check handles bloom-filter false positives.
        if not self._db.key_may_exist(key) or key not in self._db:
            raise KeyError(key)
        del self._db[key]

    def get(self, key: str, default: Any = None) -> Any:
        # Bloom filter short-circuits the disk read when RocksDB can prove
        # the key is absent.
        if not self._db.key_may_exist(key):
            return default
        return self._db.get(key, default)

    def items(self) -> Iterator[tuple[Any, Any]]:
        for key, value in self._db.items():
            yield key, value

    def __iter__(self) -> Iterator[str]:
        # Rdict.keys() walks SST keys without ever reading + unpickling
        # values — both faster and narrower than .items() since `for k in d:`
        # has no reason to touch the pickle surface.
        yield from self._db.keys()

    def __contains__(self, key: str) -> bool:
        return key in self._db

    def __len__(self) -> int:
        """Estimated key count.

        RocksDB exposes an estimate (sum of inserts/updates seen by the LSM),
        not an exact count. Use this for rough sizing, not invariants.
        """
        # property_value can in principle return an empty string; coerce safely.
        return int(self._db.property_value("rocksdb.estimate-num-keys") or "0")

    def append_to_key(self, key: str, value: Any) -> None:
        """Append a value to the list stored at ``key``.

        Read-modify-write list append. **Not atomic, not thread-safe.**
        Two threads or coroutines racing on the same key will lose updates;
        serialize access externally if that's possible in your call site.

        Args:
            key: Dictionary key. Creates a new list if absent.
            value: Value to append to the list at ``key``.
        """
        current = self.get(key, [])
        current.append(value)
        self[key] = current

    def close(self) -> None:
        """Close the database and remove its temporary directory.

        Idempotent: safe to call multiple times.
        """
        self._cleanup()

    def _cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._db.close()
        except Exception:
            logger.warning("Failed to close RocksDB cleanly", exc_info=True)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def __enter__(self) -> "DiskBackedDict":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self._cleanup()
