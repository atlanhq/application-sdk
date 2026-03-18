"""Unit tests for transparent FileReference persist / materialise."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from application_sdk.contracts.base import Input, Output
from application_sdk.contracts.types import FileReference
from application_sdk.storage.factory import create_memory_store
from application_sdk.storage.file_ref_sync import (
    has_refs_to_materialize,
    has_refs_to_persist,
    materialize_file_refs,
    persist_file_refs,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_local_file(content: bytes = b"hello") -> str:
    """Write *content* to a temp file and return the path."""
    fd, path = tempfile.mkstemp()
    try:
        os.write(fd, content)
    finally:
        os.close(fd)
    return path


@dataclass
class _TaskOutput(Output, allow_unbounded_fields=True):
    name: str = ""
    ref: FileReference = FileReference()


@dataclass
class _TaskInput(Input, allow_unbounded_fields=True):
    ref: FileReference = FileReference()


# ---------------------------------------------------------------------------
# has_refs_to_persist
# ---------------------------------------------------------------------------


class TestHasRefsToPersist:
    def test_returns_false_for_no_refs(self) -> None:
        output = _TaskOutput(name="test")
        assert has_refs_to_persist(output) is False

    def test_returns_true_for_ephemeral_ref(self) -> None:
        path = _make_local_file()
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            assert has_refs_to_persist(output) is True
        finally:
            os.unlink(path)

    def test_returns_false_for_already_durable(self) -> None:
        output = _TaskOutput(
            ref=FileReference(
                local_path="/tmp/x", is_durable=True, storage_path="file_refs/abc"
            )
        )
        assert has_refs_to_persist(output) is False

    def test_returns_false_for_ref_with_no_local_path(self) -> None:
        output = _TaskOutput(ref=FileReference(is_durable=False, local_path=None))
        assert has_refs_to_persist(output) is False


# ---------------------------------------------------------------------------
# has_refs_to_materialize
# ---------------------------------------------------------------------------


class TestHasRefsToMaterialize:
    def test_returns_false_for_no_refs(self) -> None:
        inp = _TaskInput()
        assert has_refs_to_materialize(inp) is False

    def test_returns_true_for_durable_ref_without_local_path(self) -> None:
        inp = _TaskInput(
            ref=FileReference(
                is_durable=True, storage_path="file_refs/abc", local_path=None
            )
        )
        assert has_refs_to_materialize(inp) is True

    def test_returns_false_for_durable_ref_with_local_path(self) -> None:
        inp = _TaskInput(
            ref=FileReference(
                is_durable=True,
                storage_path="file_refs/abc",
                local_path="/tmp/existing",
            )
        )
        assert has_refs_to_materialize(inp) is False


# ---------------------------------------------------------------------------
# persist_file_refs round-trip
# ---------------------------------------------------------------------------


class TestPersistAndMaterialize:
    async def test_persist_uploads_file(self) -> None:
        store = create_memory_store()
        content = b"payload data"
        path = _make_local_file(content)
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            persisted = await persist_file_refs(store, output)
            assert isinstance(persisted, _TaskOutput)
            assert persisted.ref.is_durable is True
            assert persisted.ref.storage_path is not None
            assert persisted.ref.local_path == path  # local_path preserved
        finally:
            os.unlink(path)

    async def test_materialize_downloads_file(self) -> None:
        store = create_memory_store()
        content = b"round-trip content"
        path = _make_local_file(content)
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            persisted = await persist_file_refs(store, output)
            storage_path = persisted.ref.storage_path
            assert storage_path is not None
        finally:
            os.unlink(path)

        # Simulate a remote worker that has no local file
        remote_input = _TaskInput(
            ref=FileReference(
                is_durable=True, storage_path=storage_path, local_path=None
            )
        )
        materialised = await materialize_file_refs(store, remote_input)
        assert isinstance(materialised, _TaskInput)
        assert materialised.ref.local_path is not None
        assert os.path.exists(materialised.ref.local_path)
        assert open(materialised.ref.local_path, "rb").read() == content
        os.unlink(materialised.ref.local_path)

    async def test_persist_noop_for_durable_ref(self) -> None:
        store = create_memory_store()
        ref = FileReference(is_durable=True, storage_path="file_refs/existing")
        output = _TaskOutput(ref=ref)
        result = await persist_file_refs(store, output)
        assert result.ref.storage_path == "file_refs/existing"

    async def test_materialize_noop_for_non_durable(self) -> None:
        store = create_memory_store()
        inp = _TaskInput(ref=FileReference(is_durable=False, local_path="/tmp/local"))
        result = await materialize_file_refs(store, inp)
        assert result.ref.local_path == "/tmp/local"
