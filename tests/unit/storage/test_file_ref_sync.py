"""Unit tests for transparent FileReference persist / materialise."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

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

    def test_returns_true_for_durable_ref_with_missing_local_file(self) -> None:
        # local_path is set but the file doesn't exist on disk → needs materialize
        inp = _TaskInput(
            ref=FileReference(
                is_durable=True,
                storage_path="file_refs/abc",
                local_path="/tmp/nonexistent_file_that_does_not_exist",
            )
        )
        assert has_refs_to_materialize(inp) is True

    def test_returns_false_for_durable_ref_with_valid_sidecar(self) -> None:
        import hashlib

        content = b"verified content"
        fd, path = tempfile.mkstemp()
        try:
            os.write(fd, content)
            os.close(fd)
            digest = hashlib.sha256(content).hexdigest()
            Path(path + ".sha256").write_text(digest)
            inp = _TaskInput(
                ref=FileReference(
                    is_durable=True, storage_path="file_refs/abc", local_path=path
                )
            )
            assert has_refs_to_materialize(inp) is False
        finally:
            os.unlink(path)
            try:
                os.unlink(path + ".sha256")
            except FileNotFoundError:
                pass


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

    async def test_materialize_fast_path_skips_download(self) -> None:
        """Local file intact + sidecar match → no re-download."""
        store = create_memory_store()
        content = b"fast path data"
        path = _make_local_file(content)
        try:
            output = _TaskOutput(ref=FileReference(local_path=path, is_durable=False))
            persisted = await persist_file_refs(store, output)

            # Materialize onto the same worker (local file still present).
            inp = _TaskInput(ref=persisted.ref)
            materialised = await materialize_file_refs(store, inp)
            assert materialised.ref.local_path == path
            assert open(path, "rb").read() == content
        finally:
            os.unlink(path)


class TestDirectoryPersistMaterialize:
    async def test_persist_directory_sets_file_count(self, tmp_path) -> None:
        store = create_memory_store()
        (tmp_path / "a.txt").write_bytes(b"a")
        (tmp_path / "b.txt").write_bytes(b"b")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "c.txt").write_bytes(b"c")

        ref = FileReference(local_path=str(tmp_path), is_durable=False)
        output = _TaskOutput(ref=ref)
        persisted = await persist_file_refs(store, output)

        assert persisted.ref.is_durable is True
        assert persisted.ref.file_count == 3
        assert persisted.ref.storage_path is not None
        assert persisted.ref.storage_path.endswith("/")

    async def test_persist_directory_roundtrip(self, tmp_path) -> None:
        """Persist a directory then materialize on a 'different worker'."""
        store = create_memory_store()
        src = tmp_path / "src"
        src.mkdir()
        (src / "x.txt").write_bytes(b"x content")
        (src / "y.txt").write_bytes(b"y content")

        ref = FileReference(local_path=str(src), is_durable=False)
        output = _TaskOutput(ref=ref)
        persisted = await persist_file_refs(store, output)
        storage_path = persisted.ref.storage_path

        # Simulate a different worker — no local_path.
        remote_input = _TaskInput(
            ref=FileReference(
                is_durable=True, storage_path=storage_path, local_path=None
            )
        )
        materialised = await materialize_file_refs(store, remote_input)

        assert materialised.ref.local_path is not None
        local_dir = materialised.ref.local_path
        assert os.path.isdir(local_dir)
        assert (tmp_path / local_dir.lstrip("/") / "x.txt").exists() or open(
            os.path.join(local_dir, "x.txt"), "rb"
        ).read() == b"x content"
        assert materialised.ref.file_count == 2

    async def test_local_sidecar_ok_returns_false_for_directory(self, tmp_path) -> None:
        from application_sdk.storage.file_ref_sync import _local_sidecar_ok

        assert _local_sidecar_ok(str(tmp_path)) is False

    async def test_needs_materialize_true_for_dir_ref(self, tmp_path) -> None:
        from application_sdk.storage.file_ref_sync import _needs_materialize

        ref = FileReference(
            is_durable=True,
            storage_path="file_refs/abc/",
            local_path=str(tmp_path),
        )
        # Directory always needs materialize (no fast path).
        assert _needs_materialize(ref) is True
