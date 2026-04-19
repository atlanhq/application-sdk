"""Store factory helpers.

Convenience functions for creating common obstore store types without
needing to import from obstore directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from obstore.store import LocalStore, MemoryStore


def create_local_store(root_path: str | Path) -> "LocalStore":
    """Create a ``LocalStore`` rooted at *root_path*.

    Creates the directory (and any parents) if it does not already exist.

    Args:
        root_path: Filesystem path for the store root.

    Returns:
        A configured ``LocalStore`` instance.
    """
    from obstore.store import LocalStore

    path = Path(root_path)
    path.mkdir(parents=True, exist_ok=True)
    return LocalStore(prefix=str(path.resolve()))


def create_memory_store() -> "MemoryStore":
    """Create an in-memory store suitable for unit tests.

    Returns:
        A configured ``MemoryStore`` instance.
    """
    from obstore.store import MemoryStore

    return MemoryStore()
