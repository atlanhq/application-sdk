"""RocksDB utilities for disk-backed state storage.

Provides factory functions for creating and cleaning up RocksDB (Rdict) instances
used by TableScope for storing incremental states.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from application_sdk.observability.logger_adaptor import get_logger

logger = get_logger(__name__)

try:
    from rocksdict import Rdict
except ImportError:
    Rdict = None  # type: ignore[misc, assignment]


def create_states_db() -> Any:
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


def close_states_db(db: Any) -> None:
    """Close RocksDB and cleanup its temporary directory.

    Args:
        db: The Rdict instance to close, or None
    """
    if db is None:
        return

    # Get path before close (close may invalidate it)
    db_path = None
    try:
        db_path = db.path()
    except Exception as e:
        logger.warning("Failed to get RocksDB path: %s", e)

    # Close db (may fail, but we still want to cleanup)
    try:
        db.close()
    except Exception as e:
        logger.warning("Failed to close RocksDB: %s", e)

    # Always cleanup temp directory
    if db_path:
        shutil.rmtree(db_path, ignore_errors=True)
