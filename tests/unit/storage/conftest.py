"""Fixtures shared by the storage unit tests."""

from __future__ import annotations

import pytest
from obstore.store import MemoryStore

from application_sdk.storage.factory import create_memory_store


@pytest.fixture
def store() -> MemoryStore:
    """A fresh in-memory object store."""
    return create_memory_store()
