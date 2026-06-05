"""Disk-backed dictionary backed by RocksDB.

Provides a dict-like interface (implements ``collections.abc.MutableMapping``)
for storing key-value pairs on disk, optimized for write-heavy connector
workloads (entity location maps, lineage processing, metadata verification,
etc.).

Replaces the historical `sqlitedict` pattern that several connectors adopted
locally. `sqlitedict` is unmaintained and has no upstream fix for
CVE-2024-35515 (pickle-based arbitrary code execution), which made it a
chronic allowlist entry across our scan pipeline.

Requires the ``storage`` extra (which ``incremental`` also pulls in)::

    pip install atlan-application-sdk[storage]

Related: ``application_sdk.common.incremental.storage.rocksdb_utils``
provides a lower-level Rdict factory (``create_states_db`` /
``close_states_db``) used by TableScope for incremental-extract state. The
two utilities coexist deliberately — TableScope wants raw Rdict, callers
that want dict-like semantics want this class.
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
import weakref
from collections.abc import Iterator, MutableMapping
from typing import TYPE_CHECKING, Any, Self, TypeAlias

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

# Optional dependency. For static type checking we always reference the real
# rocksdict symbols so pyright sees the proper types throughout the file. At
# runtime we fall back to None and raise a clear ImportError in __init__ if
# the [storage] extra isn't installed.
if TYPE_CHECKING:
    from rocksdict import BlockBasedOptions, Options, Rdict
else:
    try:
        from rocksdict import BlockBasedOptions, Options, Rdict
    except ImportError:
        Rdict = None
        Options = None
        BlockBasedOptions = None

# The key types rocksdict itself accepts. Anything outside this union is
# rejected internally by ``key_may_exist`` / ``__getitem__`` with an
# unhelpful TypeError; SpillableDict's defensive guards filter on this
# set so callers see either standard dict-style misses (reads) or a
# clear typed error (writes).
#
# ``SpillableKey`` is exported as the call-side type hint so static
# checkers surface bad-key usage at the caller (e.g. ``d[None] = v``
# becomes a type error against this union); ``ROCKSDB_KEY_TYPES`` is
# the runtime tuple used for ``isinstance`` guards in the methods
# below, since unions aren't valid second arguments to ``isinstance``.
SpillableKey: TypeAlias = str | int | float | bool | bytes
ROCKSDB_KEY_TYPES: tuple[type, ...] = (str, int, float, bool, bytes)


class SpillableDict(MutableMapping):  # type: ignore[type-arg]
    """A disk-backed dictionary using RocksDB.

    Implements ``collections.abc.MutableMapping`` — ``pop``, ``update``,
    ``setdefault``, ``keys()``, ``values()``, ``clear()``, etc. are all
    available from the ABC, built on the five abstract methods this class
    overrides (``__setitem__``, ``__getitem__``, ``__delitem__``,
    ``__iter__``, ``__len__``).

    Backed by RocksDB, optimized for write-heavy workloads via LSM-tree
    storage. RocksDB handles write buffering internally via MemTable, so
    callers don't need to manage explicit commits.

    The backing store is a temporary directory created on construction and
    removed on ``close()`` / context-manager exit. Use only for ephemeral
    per-run state — there is no API for persistent reuse across runs.

    **Supported key types**: ``str``, ``int``, ``float``, ``bool``, ``bytes``
    (whatever RocksDB itself accepts via rocksdict). These are exposed as
    the ``SpillableKey`` typealias for static checkers.

    **Reads of unsupported / typo'd keys** are handled defensively to
    match the standard ``dict`` contract that ``sqlitedict`` migrations
    expect — so callers can probe optimistically without wrapping every
    access:

    - ``d.get(bad_key, default)`` returns ``default`` (instead of raising
      from rocksdict's ``key_may_exist`` internals).
    - ``bad_key in d`` returns ``False``.
    - ``d[bad_key]`` raises ``KeyError`` (matches dict for missing keys).
    - ``del d[bad_key]`` raises ``KeyError``.

    **Writes of unsupported keys raise ``TypeError`` loudly**, including
    ``d[None] = v``. Silently dropping writes hides caller bugs and turns
    into data loss; if a caller has a "skip when key is None" semantic,
    they should express it explicitly at the call site:

        if key is not None:
            d[key] = value

    **Iteration order is RocksDB key order (lexicographic), not insertion
    order.** Callers that need insertion order must track it themselves —
    iterating a ``SpillableDict`` is not equivalent to iterating a Python
    ``dict``.

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

    **Cleanup safety net.** Each instance registers an ``atexit`` cleanup so
    the temp directory doesn't leak if the caller skips both the context
    manager and an explicit ``close()``. Prefer the context manager —
    ``atexit`` only fires at interpreter shutdown.

    Example::

        with SpillableDict() as d:
            d["k"] = [1, 2, 3]
            d.append_to_key("k", 4)
            assert d["k"] == [1, 2, 3, 4]
            for k, v in d.items():
                ...

    Notes:
        - ``__len__`` is exact (O(n)) — honors MutableMapping's exact-len
          contract. Use ``approximate_size()`` for a constant-time hint.
        - Iteration is in RocksDB's internal key order, not insertion order.
    """

    def __init__(self) -> None:
        if Rdict is None:
            # rocksdict ships Rdict/Options/BlockBasedOptions as a unit;
            # checking one is sufficient.
            raise ImportError(
                "rocksdict is required for SpillableDict — install with "
                "`pip install atlan-application-sdk[storage]` or add "
                "`rocksdict>=0.3.0` to your project deps."
            )

        self._temp_dir = tempfile.mkdtemp(prefix="atlan_spillable_dict_")
        # Wrap the rest of init: if Options()/BlockBasedOptions()/Rdict()
        # raises (or KeyboardInterrupt fires mid-construction), the
        # tempfile.mkdtemp above has already created the directory but
        # cleanup paths (atexit / close / __exit__) aren't reachable yet.
        # Catch BaseException so SIGINT-during-init still cleans up.
        try:
            options = Options()
            # Smaller MemTable than RocksDB's 64MB default — connectors often
            # spin up many of these in parallel under tight memory limits.
            options.set_write_buffer_size(16 * 1024 * 1024)  # 16MB
            # Bloom filter for fast negative lookups in get() / __contains__.
            # 10 bits/key gives ~1% false-positive rate; we don't subscribe
            # to a hot read path that needs more.
            bbo = BlockBasedOptions()
            bbo.set_bloom_filter(bits_per_key=10, block_based=False)
            options.set_block_based_table_factory(bbo)
            self._db = Rdict(self._temp_dir, options=options)
        except BaseException:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            raise

        self._closed = False

        # Safety net: register an atexit cleanup keyed by weakref so the
        # instance can still be GC'd. Keep the handle so _cleanup() can
        # unregister it — otherwise atexit's registry grows by one slot
        # per SpillableDict() until process exit even after close().
        self_ref = weakref.ref(self)

        def _atexit_cleanup() -> None:
            inst = self_ref()
            if inst is not None:
                inst._cleanup()

        self._atexit_handle = _atexit_cleanup
        atexit.register(self._atexit_handle)

    def __setitem__(self, key: SpillableKey, value: Any) -> None:
        # Reminder: value is pickled. See class docstring's "Caveat on
        # untrusted data" — don't store attacker-controllable bytes here.
        if not isinstance(key, ROCKSDB_KEY_TYPES):
            raise TypeError(
                f"SpillableDict keys must be one of "
                f"(str, int, float, bool, bytes); "
                f"got {type(key).__name__}"
            )
        self._db[key] = value

    def __getitem__(self, key: SpillableKey) -> Any:
        # Reads of an unsupported key type are a guaranteed miss
        # (couldn't have been stored, since __setitem__ rejects them).
        # Match standard dict semantics for misses: KeyError.
        if not isinstance(key, ROCKSDB_KEY_TYPES):
            raise KeyError(key)
        return self._db[key]

    def __delitem__(self, key: SpillableKey) -> None:
        # rocksdict's __delitem__ maps to RocksDB Delete, which is a no-op
        # on missing keys (writes a tombstone unconditionally). We want
        # standard dict semantics — match what sqlitedict callers expect.
        # Bloom filter short-circuits the negative case; the explicit
        # `in self._db` check handles bloom-filter false positives.
        # The isinstance guard prevents key_may_exist from raising on
        # unsupported types — those just KeyError, like dict.
        if not isinstance(key, ROCKSDB_KEY_TYPES):
            raise KeyError(key)
        if not self._db.key_may_exist(key) or key not in self._db:
            raise KeyError(key)
        del self._db[key]

    def get(self, key: SpillableKey, default: Any = None) -> Any:
        # Reads with an unsupported key type can never hit anything
        # (writes reject them), so return the default rather than
        # forwarding to ``key_may_exist`` (which would raise).
        if not isinstance(key, ROCKSDB_KEY_TYPES):
            return default
        # Bloom filter short-circuits the disk read when RocksDB can prove
        # the key is absent.
        if not self._db.key_may_exist(key):
            return default
        return self._db.get(key, default)

    # items() is NOT overridden — MutableMapping's default ItemsView
    # (backed by __iter__ + __getitem__) preserves the ABC contract
    # (supports len(d.items()), repeated iteration, containment checks).
    # An override that returns a generator would silently violate that
    # contract for marginal perf gain.

    def __iter__(self) -> Iterator[SpillableKey]:
        # Rdict.keys() walks SST keys without ever reading + unpickling
        # values — both faster and narrower than .items() since `for k in d:`
        # has no reason to touch the pickle surface.
        # rocksdict's keys() returns Iterator[str|int|float|bytes|bool] (the
        # supported key types); annotation matches the union directly.
        return iter(self._db.keys())  # type: ignore[return-value]

    def __contains__(self, key: object) -> bool:
        # Protocol signature uses `object`. rocksdict raises on unsupported
        # key types (and on None) — match dict's behavior of returning
        # False for anything we couldn't possibly have stored.
        if not isinstance(key, ROCKSDB_KEY_TYPES):
            return False
        return key in self._db

    def __len__(self) -> int:
        """Exact key count.

        Iterates RocksDB's keyspace once (O(n)) to honor MutableMapping's
        exact-len contract — `bool(d)` / `len(d) == 0` must answer correctly,
        and RocksDB's internal estimate can be wrong on either side.

        Use ``approximate_size()`` if you need a constant-time hint on a
        large store.
        """
        # rocksdict's Rdict.__iter__ has different semantics than .keys()
        # and triggers KeyError mid-iteration. Use .keys() explicitly so
        # this delegates to RdictKeysIter — not the default __iter__.
        return sum(1 for _ in self._db.keys())  # noqa: SIM118

    def approximate_size(self) -> int:
        """RocksDB's estimated key count — O(1), may be inaccurate.

        Fast alternative to ``len()`` when an exact count isn't needed
        (progress logging, sizing heuristics, etc.). Returns RocksDB's
        ``rocksdb.estimate-num-keys`` property, which counts insertions
        without subtracting deletes — so it can over-count, and on a
        freshly-flushed table it can briefly under-count.
        """
        return int(self._db.property_value("rocksdb.estimate-num-keys") or "0")

    def append_to_key(self, key: SpillableKey, value: Any) -> None:
        """Append a value to the list stored at ``key``.

        Read-modify-write list append. **Not atomic, not thread-safe.**
        Two threads or coroutines racing on the same key will lose updates;
        serialize access externally if that's possible in your call site.

        **Performance — O(N²) for repeated appends to the same key.** Each
        call unpickles the whole list, appends, and repickles. Building a
        K-element list this way costs O(K²) bytes shuffled through pickle.
        For known-large lists, prefer a flattened layout where each list
        element gets its own key (e.g. ``"prefix/0"``, ``"prefix/1"``, ...
        plus a separate ``"prefix/count"``) so each append is O(1).

        **Note on untrusted data:** the value gets pickled on write and
        unpickled on subsequent reads (including the read inside this method).
        Don't pass attacker-controllable bytes here — see the class-level
        "Caveat on untrusted data" section.

        Args:
            key: Dictionary key (must be a supported primitive type). Creates a
                new list if absent. Raises ``TypeError`` for unsupported types
                (including ``None``) — same contract as ``__setitem__``.
            value: Value to append to the list at ``key``.
        """
        if not isinstance(key, ROCKSDB_KEY_TYPES):
            raise TypeError(
                f"SpillableDict keys must be one of "
                f"(str, int, float, bool, bytes); "
                f"got {type(key).__name__}"
            )
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
        # Unregister the atexit hook to keep the registry bounded across
        # many short-lived instances. atexit.unregister silently no-ops
        # if the handle was already removed (e.g. atexit already fired
        # during interpreter shutdown).
        atexit.unregister(self._atexit_handle)
        try:
            self._db.close()
        except Exception:
            logger.warning("Failed to close RocksDB cleanly", exc_info=True)
        shutil.rmtree(self._temp_dir, ignore_errors=True)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._cleanup()
