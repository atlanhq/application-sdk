"""RocksDB utilities for disk-backed state storage.

Provides factory functions for creating and cleaning up RocksDB (Rdict) instances
used by TableScope for storing incremental states.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

try:
    from rocksdict import Rdict
except ImportError:  # conformance: ignore[E008,E009] optional dep rocksdict not installed; sentinel fallback
    Rdict = None  # type: ignore[misc, assignment]


class StatesStore(Protocol):
    """Table qualified name -> incremental state mapping, as ``TableScope`` uses it.

    Structural, so anything with these two methods fits: the RocksDB
    :class:`RocksStatesStore` in production, a plain ``dict[str, str]`` in
    tests.
    """

    def __setitem__(self, key: str, value: str, /) -> None: ...

    def get(self, key: str, /) -> str | None: ...


@runtime_checkable
class ProbeableStatesStore(StatesStore, Protocol):
    """A :class:`StatesStore` with a Bloom-filter probe for fast negative lookups.

    Runtime checkable so a lookup can use the probe when the store has one.
    """

    # A bare bool without ``fetch``; Rdict's own signature widens it for
    # ``fetch=True``, which the incremental code never passes.
    def key_may_exist(self, key: str, /) -> bool | tuple[bool, object]: ...


@runtime_checkable
class RocksStatesStore(ProbeableStatesStore, Protocol):
    """The disk-backed :class:`StatesStore` that :func:`create_states_db` returns.

    The subset of ``rocksdict.Rdict`` the incremental code calls. Runtime
    checkable so :func:`close_states_db` can tell it from a plain mapping.
    """

    def path(self) -> str: ...

    def close(self) -> None: ...


def create_states_db() -> RocksStatesStore:
    """Create a temporary RocksDB for table states.

    Creates an Rdict instance backed by a unique temporary directory.
    The directory is automatically named with a UUID to prevent conflicts.

    Returns:
        Rdict instance for storing table qualified name -> state mappings
    """
    if Rdict is None:
        raise ImportError("rocksdict is required for create_states_db")

    path = Path(tempfile.gettempdir()).joinpath(f"table_states_{uuid.uuid4().hex}")
    return Rdict(str(path))


def close_states_db(db: StatesStore | None) -> None:
    """Close RocksDB and cleanup its temporary directory.

    A store that is not a :class:`RocksStatesStore` (a ``dict`` in tests) holds
    nothing to close, so it is left alone.

    Args:
        db: The Rdict instance to close, or None
    """
    if db is None or not isinstance(db, RocksStatesStore):
        return

    # Get path before close (close may invalidate it)
    db_path = None
    try:
        db_path = db.path()
    except Exception:
        logger.warning("Failed to get RocksDB path", exc_info=True)

    # Close db (may fail, but we still want to cleanup)
    try:
        db.close()
    except Exception:
        logger.warning("Failed to close RocksDB", exc_info=True)

    # Always cleanup temp directory
    if db_path:
        shutil.rmtree(db_path, ignore_errors=True)
