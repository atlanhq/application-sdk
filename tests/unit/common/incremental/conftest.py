"""Shared fixtures for incremental-extraction unit tests."""

from collections.abc import Iterator

import pytest
from obstore.store import MemoryStore

from application_sdk.infrastructure.context import (
    InfrastructureContext,
    clear_infrastructure,
    set_infrastructure,
)
from application_sdk.storage.factory import create_memory_store


@pytest.fixture
def memory_store() -> Iterator[MemoryStore]:
    """An in-memory object store bound as the infrastructure store.

    Lets the incremental download/upload helpers — which resolve their store
    from the infrastructure context — run against real storage, so a test can
    assert the resulting on-disk *layout* instead of mocking the transfer away
    (FND-340).
    """
    store = create_memory_store()
    set_infrastructure(InfrastructureContext(storage=store))
    try:
        yield store
    finally:
        clear_infrastructure()
